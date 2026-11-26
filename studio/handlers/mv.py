"""MV(ミュージックビデオ。歌わせるリップシンクと、歌わせないイメージ MV)のハンドラ: mv.analyze / mv.listen / mv.script / mv.scene / mv.assemble。

どのジョブをいつ入れるか(順番・一時停止・やり直し)は mv.Director が決める。ここは 1 つのジョブの仕事だけをして、結果を台帳の mv_projects に書く
(シーンを「生成中」にするのと、失敗・中止の記録は Director の仕事)。ジョブの params は {"project_id", ("scene")} だけで、中身はその都度台帳から読む。
  analyze  : CPU。音声を 44.1kHz にそろえ → Demucs でボーカルを分け → 曲をシーンに分ける(道具用の Python の子プロセス)
  listen   : CPU(管理役の LLM)。曲の 30 秒を最大 2 か所聞いて(Gemma の音声入力)言語・ジャンル・雰囲気・楽器・ボーカル・テンポを読み取り、曲の内容・世界観の案を書く。
             曲の台帳に情報が無く、「曲の内容」が空の制作が、分析の後に通る(BPM は分析のもの)。GPU も ACE-Step も使わない
  script   : CPU(管理役の LLM)。人物の説明が空なら、まず画像を見て作る。そのうえで、シーンごとの演出を、H3 公式の Ref2VA 6 セクション形式で書く
  scene    : H3。そのシーンのボーカルを切り出し、キャラクター画像と一緒に Ref2VA + Vocal Lock で生成 → ちょうどの尺のクリップに整える。
             歌わせない制作(params.lipsync = False、イメージ MV)は音声を渡さず、画像だけを参照に seconds(シーンの尺)と mute で頼む
             (口を動かすのは音声の参照そのもので、音声が無ければプロンプトの閉口が効く。mv_studio_V3 の 2026-08-26 の実機プローブ)
  assemble : CPU。クリップをつなぎ、元の曲に差し替えた MP4 を作ってギャラリーに載せる
作業のファイルは outputs/mv/<制作の ID>/ に置く(song.wav・vocals.wav・scene_NNN.mp4 など)。
"""
import asyncio
import hashlib
import logging
import re
import time
from pathlib import Path

from .. import config, media, mv_audio, mvclip, mvdescribe, mvscript
from ..jobs import Ctx, Handler, Job, JobCancelled, JobFailed
from ..ledger import THUMB_WIDTH
from ..llm import LlmError
from ..mv import make_scenes, scene_clip_name, scene_thumb_name, scene_vocals_name, work_dir
from . import h3
from .common import download

log = logging.getLogger("studio.mv")

H3_AUDIO_RATE, H3_MIN_AUDIO_S = 16000, 5.0       # H3 に渡すボーカル: 16kHz モノラル、最短 5 秒(足りない分は無音で延ばす)
SEPARATE_START, SEPARATE_SPAN = 0.05, 0.85       # 分析ジョブの進みのうち、ボーカルの分離が占める範囲
DEVICES = ("cpu", "cuda", "auto")


NOUNS = {"mv": "MV の", "short": "ショート動画の"}       # ジョブの種類の接頭辞 -> エラー文に出す呼び名(ショート動画のジョブも、ここを通る)


def normalize(kind: str, params: dict, ledger=None) -> dict:
    """ジョブの params を検証する: 制作の ID(と、シーンなら番号)。不正なら ValueError(日本語)。
    制作の種類(台帳の kind)はジョブの種類の接頭辞(mv / short)と合っていなければならない(MV のジョブでショート動画の制作を触らせない)。"""
    prefix, _, stage = kind.partition(".")
    pid = str(params.get("project_id") or "").strip()
    proj = ledger.get_mv_project(pid) if ledger is not None and pid else None
    if not pid or (ledger is not None and (proj is None or proj.get("kind", "mv") != prefix)):
        raise ValueError(f"{NOUNS.get(prefix, 'MV の')}制作が見つかりません(削除されたかもしれません)")
    out = {"project_id": pid}
    if stage == "scene":
        try:
            out["scene"] = int(params.get("scene"))
        except (TypeError, ValueError, OverflowError):
            raise ValueError("シーンの番号を整数で指定してください")
        if proj is not None and not any(s["index"] == out["scene"] for s in proj["scenes"]):
            raise ValueError(f"シーン {out['scene']} はありません")
    return out


def _project(ctx: Ctx, job: Job) -> dict:
    proj = ctx.ledger.get_mv_project(job.params["project_id"])
    if proj is None:
        raise JobFailed(f"{NOUNS.get(job.kind.partition('.')[0], 'MV の')}制作が見つかりません(削除されたかもしれません)")
    return proj


def _device() -> str:
    """ボーカルの分離に使う装置([mv] device。空なら CPU)。"""
    value = str(config.settings.mv.get("device") or "cpu").strip().lower()
    return value if value in DEVICES else "cpu"


async def _ffmpeg(build, dst: Path, what: str, media_s: float = None) -> None:
    """ffmpeg を別スレッドで走らせる。build(exe, tmp) が引数を返し、出力先 tmp(= dst.part)に書かせて、成功したら dst に置き換える。失敗は JobFailed。
    取り消されたら(ジョブの中止・管理役の停止)ffmpeg も止める。media_s は作る動画の尺(秒。長い動画の作り直しは、待つ最長をそれに合わせて延ばす)。"""
    try:
        exe = mv_audio._ffmpeg()
        await mv_audio.run_ffmpeg(build(exe, mv_audio._part(dst)), dst, what, media_s=media_s)
    except mv_audio.MvAudioError as e:
        raise JobFailed(e.message, e.log_tail) from e


# 走っている作業のタスク(ジョブの ID -> タスク)。CPU の長い仕事(分離・LLM)を、中止で取り消せるようにする
_tasks: dict = {}


async def _cancellable(job: Job, coro):
    """coro を、cancel_task で取り消せるタスクとして走らせる。取り消されたら JobCancelled(管理役の停止による取り消しは、そのまま通す)。"""
    task = asyncio.ensure_future(coro)
    _tasks[job.id] = task
    try:
        return await task
    except asyncio.CancelledError:
        if job.cancel_requested and not asyncio.current_task().cancelling():
            raise JobCancelled() from None
        raise
    finally:
        _tasks.pop(job.id, None)


async def cancel_task(job: Job, ctx: Ctx) -> None:
    """走っている作業を取り消す(分離の子プロセスは mv_audio が止める)。"""
    task = _tasks.get(job.id)
    if task is not None:
        task.cancel()


# ---- mv.analyze ----

async def run_analyze(job: Job, ctx: Ctx) -> None:
    if job.cancel_requested:
        raise JobCancelled()
    await _cancellable(job, _analyze(job, ctx))


async def analyze_song(job: Job, ctx: Ctx, proj: dict, work: Path) -> tuple:
    """曲を 44.1kHz にそろえ、ボーカルを分離し、曲をシーンに分ける(MV の mv.analyze と、ショート動画の曲だけの short.analyze が使う)。
    work に song.wav・vocals.wav・accompaniment.wav を置く(できているものはやり直さない)。(量子化したシーンの並び, 分析の結果の辞書)を返す。"""
    p = proj["params"]
    rec = ctx.ledger.get_output(p["song_id"])
    if rec is None:
        raise JobFailed("曲が見つかりません(ギャラリーから削除されたかもしれません)")
    work.mkdir(parents=True, exist_ok=True)
    song, vocals, accompaniment = work / "song.wav", work / "vocals.wav", work / "accompaniment.wav"

    def on_line(line: str) -> None:
        frac = mv_audio.progress_fraction(line)
        if frac is not None:
            job.progress = SEPARATE_START + SEPARATE_SPAN * frac
            job.message = f"ボーカルを分離しています {round(frac * 100)}%"
    try:
        if not song.is_file():       # 失敗のあとの再開で、できているものはやり直さない(分離は数分〜数十分かかる)
            job.message = "音声を 44.1kHz にそろえています"
            await mv_audio.normalize(ctx.ledger.abs_path(rec["path"]), song)
        if vocals.is_file() and accompaniment.is_file():
            job.message = "分離済みのボーカルを使います"
        else:
            job.progress, job.message = SEPARATE_START, "ボーカルを分離しています(CPU では曲の長さの 0.5〜3 倍ほどかかります)"
            await mv_audio.separate(song, work, device=_device(), on_progress=on_line)
        job.progress, job.message = SEPARATE_START + SEPARATE_SPAN, "曲をシーンに分けています"
        plan = await mv_audio.analyze(song, vocals, p["min_s"], p["max_s"], (p["min_s"] + p["max_s"]) / 2)
    except mv_audio.MvAudioError as e:      # 道具が無い(MvToolsMissing)ときは、その案内がそのまま message になる
        raise JobFailed(e.message, e.log_tail) from e
    scenes = mvclip.quantize_scene_plan(plan["scenes"])
    if not scenes:
        raise JobFailed("曲をシーンに分けられませんでした")
    analysis = {"duration": plan["duration"], "bpm": plan.get("bpm"), "has_vocals": bool(plan.get("has_vocals", True)),
                "scenes": len(scenes)}
    return scenes, analysis


async def _analyze(job: Job, ctx: Ctx) -> None:
    proj = _project(ctx, job)
    pid = proj["id"]
    scenes, analysis = await analyze_song(job, ctx, proj, work_dir(ctx.outputs_dir, pid))
    ctx.ledger.update_mv_project(pid, scenes=make_scenes(scenes), params_patch={"analysis": analysis, "assembled": False})
    job.result = analysis
    log.info("MV %s: 曲 %.1f 秒を %d シーンに分けました(BPM %s、歌%s)", pid, analysis["duration"], len(scenes), analysis["bpm"],
             "あり" if analysis["has_vocals"] else "なし")


# ---- mv.listen ----

async def run_listen(job: Job, ctx: Ctx) -> None:
    if job.cancel_requested:
        raise JobCancelled()
    await _cancellable(job, _listen(job, ctx))


async def _listen(job: Job, ctx: Ctx) -> None:
    proj = _project(ctx, job)
    pid, p = proj["id"], proj["params"]
    rec = ctx.ledger.get_output(p["song_id"])
    if rec is None:
        raise JobFailed("曲が見つかりません(ギャラリーから削除されたかもしれません)")
    if ctx.llm is None:
        raise JobFailed("LLM が設定されていません")
    analysis = p.get("analysis") or {}
    job.message = "曲を聞いています"
    try:
        heard = await mvdescribe.listen_song(ctx.llm, ctx.ledger.abs_path(rec["path"]), analysis.get("duration") or rec.get("duration_s"))
        job.progress, job.message = 0.9, "曲の内容を考えています"
        text = await mvdescribe.propose_concept(ctx.llm, {**heard, "bpm": analysis.get("bpm")}, rec["params"])
    except LlmError as e:
        raise JobFailed(e.message) from e
    except mv_audio.MvAudioError as e:
        raise JobFailed(e.message, e.log_tail) from e
    job.result = {"text": text, **heard}
    # 失敗しても MV は止めない(Director が、曲の内容なしで続ける)。ここに書くのは、聞けて案ができたときだけ
    ctx.ledger.update_mv_project(pid, params_patch={"concept": text, "concept_source": "listen", "listen": "done"})
    log.info("MV %s: 曲を聞きました(%s)→ %s", pid, heard, text)


# ---- mv.script ----

async def run_script(job: Job, ctx: Ctx) -> None:
    if job.cancel_requested:
        raise JobCancelled()
    await _cancellable(job, _script(job, ctx))


async def _describe_image(job: Job, ctx: Ctx, proj: dict, image_id: str = None) -> str:
    """人物の説明が空の制作(台帳に画像のプロンプトが無い、画面で書かなかった)のとき、画像を見て人物の説明を作り、制作に残す(再開でやり直さない)。
    画像を読めなければ、理由を character_note に残して空のまま(演出は中立の文)。image_id は人物の画像(MV は params.image_id、ショート動画は人物の参照画像)。"""
    rec = ctx.ledger.get_output(image_id or proj["params"]["image_id"])
    if rec is None:
        return ""
    job.message = "画像を見て、人物の見た目を調べています"
    res = await mvdescribe.describe_character(ctx.llm, ctx.ledger.abs_path(rec["path"]), str(rec["params"].get("prompt") or ""))
    source = {"vision": "vision", "prompt": "image"}.get(res["source"], "none")        # none: 読めなかった(再開しても、また頼まない)
    ctx.ledger.update_mv_project(proj["id"], params_patch={"character": res["text"], "character_source": source, "character_note": res["note"]})
    log.info("MV %s: 人物の説明を自動で作りました(%s): %s", proj["id"], res["source"], res["text"] or res["note"])
    return res["text"]


async def _script(job: Job, ctx: Ctx) -> None:
    proj = _project(ctx, job)
    pid, p, scenes = proj["id"], proj["params"], proj["scenes"]
    if ctx.llm is None:
        raise JobFailed("LLM が設定されていません")

    def on_progress(done: int, total: int) -> None:
        job.progress, job.message = done / total, f"演出を書いています {done} / {total} シーン"
    try:
        description = str(p.get("character") or "").strip()
        if not description and p.get("character_source") != "none":
            description = await _describe_image(job, ctx, proj)
        job.message = "人物の見た目を整理しています"
        # 人物の特徴は、画面の説明か、画像のプロンプトか、画像を見て作った説明。場所や光を除いた見た目だけにしてから使う
        character = await mvscript.character_facts(ctx.llm.chat, description)
        job.message = f"演出を書いています 0 / {len(scenes)} シーン"
        result = await mvscript.generate_scenario(
            ctx.llm.chat, scenes, p.get("concept") or "", character, frame=mvscript.frame_kind(p["width"], p["height"]),
            background_text=p.get("background_text") or "auto", lipsync=bool(p.get("lipsync", True)),
            style=p.get("style") or "none", on_progress=on_progress)
    except LlmError as e:
        raise JobFailed(e.message) from e
    except mvscript.ScenarioError as e:
        raise JobFailed(e.message) from e
    items = {int(it["index"]): it for it in result["scenes"]}
    seed_base = p["seed"] if p.get("seed") is not None else int(hashlib.sha256(pid.encode()).hexdigest()[:8], 16)
    patches = {s["index"]: {"prompt": str(items[s["index"]]["prompt"]).strip(), "emotion": str(items[s["index"]].get("emotion") or ""),
                            "seed": mvclip.scene_seed(seed_base, s["index"])} for s in scenes}
    ctx.ledger.update_mv_project(pid, scene_patches=patches, params_patch={"script": result["overview"], "character_facts": character})
    log.info("MV %s: %d シーンの演出を書きました", pid, len(scenes))


# ---- mv.scene ----

async def run_scene(job: Job, ctx: Ctx) -> None:
    if job.cancel_requested:
        raise JobCancelled()
    proj = _project(ctx, job)
    pid, index, p, scenes = proj["id"], job.params["scene"], proj["params"], proj["scenes"]
    scene = next((s for s in scenes if s["index"] == index), None)
    if scene is None:
        raise JobFailed(f"シーン {index} がありません")
    if not scene.get("prompt"):
        raise JobFailed("このシーンの演出がまだありません(演出の作成からやり直してください)")
    tag = f"シーン {index} / {len(scenes)}"
    work = work_dir(ctx.outputs_dir, pid)
    lipsync = bool(p.get("lipsync", True))
    vocals, raw, clip, thumb = work / "vocals.wav", work / f"scene_{index:03d}_raw.mp4", work / scene_clip_name(index), work / scene_thumb_name(index)
    if lipsync and not vocals.is_file():
        raise JobFailed("分離したボーカルが見つかりません(分析からやり直してください)")
    started = time.monotonic()
    data = {"prompt": scene["prompt"], "width": str(p["width"]), "height": str(p["height"])}
    if lipsync:
        job.progress, job.message = 0.0, f"{tag}: ボーカルを切り出しています"
        try:
            wav = await mv_audio.cut(vocals, scene["start"], scene["end"], work / scene_vocals_name(index), rate=H3_AUDIO_RATE, channels=1,
                                     pad_to=H3_MIN_AUDIO_S)
        except mv_audio.MvAudioError as e:
            raise JobFailed(e.message, e.log_tail) from e
        if job.cancel_requested:
            raise JobCancelled()
        # 参照は、キャラクター画像(<Picture 1>)、そのシーンのボーカル(<Audio 1>)の順。秒数は送らない(= 音声の長さ)
        files = await asyncio.to_thread(lambda: [h3._load_file(ctx, "references", p["image_id"], "キャラクター画像"), h3._file_part("references", wav)])
        data["vocal_lock"] = "true"
    else:
        # 歌わせない: 参照はキャラクター画像だけ。音声が無いと H3 は尺を決められないので、シーンの尺を seconds で送る(H3 が 17n+5 コマに丸めて短く
        # 返した分は、下の tpad で足す)。H3 が作る音声は使わない(結合で曲に差し替える)ので mute
        job.progress, job.message = 0.0, f"{tag}: 準備しています"
        files = await asyncio.to_thread(lambda: [h3._load_file(ctx, "references", p["image_id"], "キャラクター画像")])
        data["seconds"] = f"{max(H3_MIN_AUDIO_S, float(scene['duration'])):g}"
        data["mute"] = "true"
    if scene.get("seed") is not None:
        data["seed"] = str(scene["seed"])
    res, frames = await generate_clip(job, ctx, h3.ENDPOINTS["ref2va"], data, files, tag=tag, scene=scene, p=p, raw=raw, clip=clip)
    await record_scene(job, ctx, "MV", proj["id"], scene, res, clip, thumb, started, frames, tag)


async def generate_clip(job: Job, ctx: Ctx, endpoint: str, data: dict, files: list, *, tag: str, scene: dict, p: dict, raw: Path, clip: Path,
                        keep_audio: bool = False) -> tuple:
    """H3 にシーン 1 つを頼み(進みは job に写す)、できた動画を取り込んで、ちょうどの尺のクリップ clip に整える(MV もショート動画も同じ)。
    (H3 の結果, クリップのコマ数)を返す。keep_audio なら H3 の音を残す(ショート動画の「どちらも無し」)。"""
    try:
        async with h3._client(ctx) as c:
            job.message = f"{tag}: H3 に送っています"
            res = await h3._generate(job, c, endpoint, data, files, prefix=f"{tag}: ")      # 中止は H3 への interrupt(h3.cancel)
            if not res.get("video_url"):
                raise JobFailed("H3 が動画を返しませんでした")
            job.progress, job.message = 0.99, f"{tag}: 動画を取り込んでいます"
            await download(c, res["video_url"], raw)
        # ここまで来たら、生成済みの動画を捨てない(中止の要求が来ていても、クリップにして残す)
        frames = scene.get("frame_count") or max(1, round(scene["duration"] * mvclip.FPS))
        pad = mvclip.pad_seconds(frames, res.get("duration_s"))
        job.message = f"{tag}: 尺をそろえています"
        await _ffmpeg(lambda exe, tmp: mvclip.finish_args(exe, raw, tmp, frames, p["width"], p["height"], mvclip.FPS, pad, keep_audio), clip,
                      f"{tag} のクリップの整形")
    finally:
        raw.unlink(missing_ok=True)
    return res, frames


async def record_scene(job: Job, ctx: Ctx, label: str, pid: str, scene: dict, res: dict, clip: Path, thumb: Path, started: float, frames: int,
                       tag: str) -> None:
    """できたシーンを台帳に書く(サムネイル・実測・seed)。"""
    index = scene["index"]
    has_thumb = await asyncio.to_thread(media.video_thumb, clip, thumb, THUMB_WIDTH)
    elapsed = res.get("total_elapsed_s")
    job.result = {"scene": index, "generation_seconds": elapsed, "peak_vram_gb": res.get("peak_vram_gb"),
                  "steps_used": res.get("num_inference_steps"), "seed": res.get("seed")}
    seed = res.get("seed") if res.get("seed") is not None else scene.get("seed")
    log.info("%s %s: %s ができました(H3 %s 秒、%d コマ、%.1f 秒)", label, pid, tag, elapsed, frames, time.monotonic() - started)
    ctx.ledger.update_mv_project(pid, scene_patches={index: {
        "status": "done", "clip": clip.name, "thumb": thumb.name if has_thumb else None, "error": None, "seed": seed,
        "elapsed_s": round(elapsed if elapsed is not None else time.monotonic() - started, 1), "peak_vram_gb": res.get("peak_vram_gb")}})


# ---- mv.assemble ----

def final_path(outputs_dir, pid: str, prefix: str = "mv") -> Path:
    """最終の動画の置き場所 outputs/video/<prefix>_<ID>.mp4(MV は mv、ショート動画は short)。結合し直すときは、前の動画(ギャラリーに残っている)を
    上書きしないよう _2、_3… にする。"""
    base = Path(outputs_dir) / "video"
    path, n = base / f"{prefix}_{pid}.mp4", 1
    while path.exists():
        n += 1
        path = base / f"{prefix}_{pid}_{n}.mp4"
    return path


def ready_clips(proj: dict, work: Path) -> tuple:
    """結合するクリップ(シーン番号順の scenes と、そのファイルの並び)。まだ無い・消えているときは JobFailed。"""
    scenes = sorted(proj["scenes"], key=lambda s: s["index"])
    if not scenes or any(s["status"] != "done" or not s.get("clip") for s in scenes):
        raise JobFailed("まだ生成できていないシーンがあります")
    clips = [work / s["clip"] for s in scenes]
    missing = [c.name for c in clips if not c.is_file()]
    if missing:
        raise JobFailed("シーンのクリップが見つかりません: " + "、".join(missing) + "(そのシーンをやり直してください)")
    return scenes, clips


async def register_video(job: Job, ctx: Ctx, pid: str, dest: Path, name: str, meta: dict, p: dict, duration: float) -> dict:
    """最終の動画をギャラリー(台帳)に載せ、制作に結びつける。"""
    rec = await asyncio.to_thread(ctx.ledger.add_output, "video", dest, job.tab, job.id, name, meta,
                                  width=p.get("width"), height=p.get("height"), duration_s=duration, fps=mvclip.FPS)
    job.outputs.append(rec["id"])
    ctx.ledger.update_mv_project(pid, output_id=rec["id"], params_patch={"assembled": True})
    return rec


async def run_assemble(job: Job, ctx: Ctx) -> None:
    if job.cancel_requested:
        raise JobCancelled()
    await _cancellable(job, _assemble(job, ctx))        # 中止は、走っている ffmpeg ごと止める(長い MV の結合は数分かかる)


async def _assemble(job: Job, ctx: Ctx) -> None:
    proj = _project(ctx, job)
    pid, p = proj["id"], proj["params"]
    work = work_dir(ctx.outputs_dir, pid)
    scenes, clips = ready_clips(proj, work)
    song = work / "song.wav"
    if not song.is_file():
        rec = ctx.ledger.get_output(p["song_id"])
        song = ctx.ledger.abs_path(rec["path"]) if rec is not None else song
        if not song.is_file():
            raise JobFailed("曲のファイルが見つかりません")
    duration = float((p.get("analysis") or {}).get("duration") or sum(s["duration"] for s in scenes))
    listing, video_only, dest = work / "concat.txt", work / "video_only.mp4", final_path(ctx.outputs_dir, pid)
    try:
        job.progress, job.message = 0.1, "シーンをつないでいます"
        await asyncio.to_thread(listing.write_text, mvclip.concat_list(clips), "utf-8")
        await _ffmpeg(lambda exe, tmp: mvclip.concat_args(exe, listing, tmp), video_only, "シーンの結合", duration)
        if job.cancel_requested:
            raise JobCancelled()
        job.progress, job.message = 0.6, "元の曲を載せています"
        await _ffmpeg(lambda exe, tmp: mvclip.mux_args(exe, video_only, song, tmp, duration), dest, "曲の差し替え", duration)
    finally:
        listing.unlink(missing_ok=True)
        video_only.unlink(missing_ok=True)
    job.progress, job.message = 0.95, "ギャラリーに登録しています"
    meta = {"project_id": pid, "kind": job.kind, "aspect": p.get("aspect"), "quality": p.get("quality"), "scenes": len(scenes),
            "song_id": p.get("song_id"), "image_id": p.get("image_id"), "width": p.get("width"), "height": p.get("height")}
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", str(p.get("title") or "MV"))[:40] + "_mv.mp4"
    await register_video(job, ctx, pid, dest, name, meta, p, duration)
    log.info("MV %s: %d シーンを結合しました → %s(%.1f 秒)", pid, len(scenes), dest.name, duration)


HANDLERS = {
    "mv.analyze": Handler(component=None, normalize=normalize, run=run_analyze, cancel=cancel_task, gpu=False),
    "mv.listen": Handler(component=None, normalize=normalize, run=run_listen, cancel=cancel_task, gpu=False),
    "mv.script": Handler(component=None, normalize=normalize, run=run_script, cancel=cancel_task, gpu=False),
    "mv.scene": Handler(component=h3.COMPONENT, normalize=normalize, run=run_scene, cancel=h3.cancel),
    "mv.assemble": Handler(component=None, normalize=normalize, run=run_assemble, cancel=cancel_task, gpu=False),
}
