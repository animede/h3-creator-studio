"""MV(リップシンク)のハンドラ: mv.analyze / mv.script / mv.scene / mv.assemble。

どのジョブをいつ入れるか(順番・一時停止・やり直し)は mv.Director が決める。ここは 1 つのジョブの仕事だけをして、結果を台帳の mv_projects に書く
(シーンを「生成中」にするのと、失敗・中止の記録は Director の仕事)。ジョブの params は {"project_id", ("scene")} だけで、中身はその都度台帳から読む。
  analyze  : CPU。音声を 44.1kHz にそろえ → Demucs でボーカルを分け → 曲をシーンに分ける(道具用の Python の子プロセス)
  script   : CPU(管理役の LLM)。シーンごとの演出を、H3 公式の Ref2VA 6 セクション形式で書く
  scene    : H3。そのシーンのボーカルを切り出し、キャラクター画像と一緒に Ref2VA + Vocal Lock で生成 → ちょうどの尺のクリップに整える
  assemble : CPU。クリップをつなぎ、元の曲に差し替えた MP4 を作ってギャラリーに載せる
作業のファイルは outputs/mv/<制作の ID>/ に置く(song.wav・vocals.wav・scene_NNN.mp4 など)。
"""
import asyncio
import hashlib
import logging
import re
import time
from pathlib import Path

from .. import config, media, mv_audio, mvclip, mvscript
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


def normalize(kind: str, params: dict, ledger=None) -> dict:
    """ジョブの params を検証する: 制作の ID(と、シーンなら番号)。不正なら ValueError(日本語)。"""
    pid = str(params.get("project_id") or "").strip()
    proj = ledger.get_mv_project(pid) if ledger is not None and pid else None
    if not pid or (ledger is not None and proj is None):
        raise ValueError("MV の制作が見つかりません(削除されたかもしれません)")
    out = {"project_id": pid}
    if kind == "mv.scene":
        try:
            out["scene"] = int(params.get("scene"))
        except (TypeError, ValueError):
            raise ValueError("シーンの番号を整数で指定してください")
        if proj is not None and not any(s["index"] == out["scene"] for s in proj["scenes"]):
            raise ValueError(f"シーン {out['scene']} はありません")
    return out


def _project(ctx: Ctx, job: Job) -> dict:
    proj = ctx.ledger.get_mv_project(job.params["project_id"])
    if proj is None:
        raise JobFailed("MV の制作が見つかりません(削除されたかもしれません)")
    return proj


def _device() -> str:
    """ボーカルの分離に使う装置([mv] device。空なら CPU)。"""
    value = str(config.settings.mv.get("device") or "cpu").strip().lower()
    return value if value in DEVICES else "cpu"


async def _ffmpeg(build, dst: Path, what: str) -> None:
    """ffmpeg を別スレッドで走らせる。build(exe, tmp) が引数を返し、出力先 tmp(= dst.part)に書かせて、成功したら dst に置き換える。失敗は JobFailed。"""
    try:
        exe = mv_audio._ffmpeg()
        await asyncio.to_thread(mv_audio._run_ffmpeg, build(exe, mv_audio._part(dst)), dst, what)
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


async def cancel_flag(job: Job, ctx: Ctx) -> None:
    """何もしない。実行中の run が、区切りごとに job.cancel_requested を見る(ffmpeg は数秒で終わるので途中では止めない)。"""


# ---- mv.analyze ----

async def run_analyze(job: Job, ctx: Ctx) -> None:
    if job.cancel_requested:
        raise JobCancelled()
    await _cancellable(job, _analyze(job, ctx))


async def _analyze(job: Job, ctx: Ctx) -> None:
    proj = _project(ctx, job)
    pid, p = proj["id"], proj["params"]
    rec = ctx.ledger.get_output(p["song_id"])
    if rec is None:
        raise JobFailed("曲が見つかりません(ギャラリーから削除されたかもしれません)")
    work = work_dir(ctx.outputs_dir, pid)
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
    scenes = make_scenes(mvclip.quantize_scene_plan(plan["scenes"]))
    if not scenes:
        raise JobFailed("曲をシーンに分けられませんでした")
    analysis = {"duration": plan["duration"], "bpm": plan.get("bpm"), "has_vocals": bool(plan.get("has_vocals", True)),
                "scenes": len(scenes)}
    ctx.ledger.update_mv_project(pid, scenes=scenes, params_patch={"analysis": analysis, "assembled": False})
    job.result = analysis
    log.info("MV %s: 曲 %.1f 秒を %d シーンに分けました(BPM %s、歌%s)", pid, analysis["duration"], len(scenes), analysis["bpm"],
             "あり" if analysis["has_vocals"] else "なし")


# ---- mv.script ----

async def run_script(job: Job, ctx: Ctx) -> None:
    if job.cancel_requested:
        raise JobCancelled()
    await _cancellable(job, _script(job, ctx))


async def _script(job: Job, ctx: Ctx) -> None:
    proj = _project(ctx, job)
    pid, p, scenes = proj["id"], proj["params"], proj["scenes"]
    if ctx.llm is None:
        raise JobFailed("LLM が設定されていません")
    job.message = "人物の見た目を整理しています"

    def on_progress(done: int, total: int) -> None:
        job.progress, job.message = done / total, f"演出を書いています {done} / {total} シーン"
    try:
        # 人物の特徴は、画面の説明か画像のプロンプト(画像の解析はしない)。場所や光を除いた見た目だけにしてから使う
        character = await mvscript.character_facts(ctx.llm.chat, p.get("character") or "")
        job.message = f"演出を書いています 0 / {len(scenes)} シーン"
        result = await mvscript.generate_scenario(
            ctx.llm.chat, scenes, p.get("concept") or "", character, frame=mvscript.frame_kind(p["width"], p["height"]),
            background_text=p.get("background_text") or "auto", on_progress=on_progress)
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
    vocals, raw, clip, thumb = work / "vocals.wav", work / f"scene_{index:03d}_raw.mp4", work / scene_clip_name(index), work / scene_thumb_name(index)
    if not vocals.is_file():
        raise JobFailed("分離したボーカルが見つかりません(分析からやり直してください)")
    started = time.monotonic()
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
    data = {"prompt": scene["prompt"], "width": str(p["width"]), "height": str(p["height"]), "vocal_lock": "true"}
    if scene.get("seed") is not None:
        data["seed"] = str(scene["seed"])
    try:
        async with h3._client(ctx) as c:
            job.message = f"{tag}: H3 に送っています"
            res = await h3._generate(job, c, h3.ENDPOINTS["ref2va"], data, files, prefix=f"{tag}: ")      # 中止は H3 への interrupt(h3.cancel)
            if not res.get("video_url"):
                raise JobFailed("H3 が動画を返しませんでした")
            job.progress, job.message = 0.99, f"{tag}: 動画を取り込んでいます"
            await download(c, res["video_url"], raw)
        # ここまで来たら、生成済みの動画を捨てない(中止の要求が来ていても、クリップにして残す)
        frames = scene.get("frame_count") or max(1, round(scene["duration"] * mvclip.FPS))
        pad = mvclip.pad_seconds(frames, res.get("duration_s"))
        job.message = f"{tag}: 尺をそろえています"
        await _ffmpeg(lambda exe, tmp: mvclip.finish_args(exe, raw, tmp, frames, p["width"], p["height"], mvclip.FPS, pad), clip,
                      f"{tag} のクリップの整形")
    finally:
        raw.unlink(missing_ok=True)
    has_thumb = await asyncio.to_thread(media.video_thumb, clip, thumb, THUMB_WIDTH)
    elapsed = res.get("total_elapsed_s")
    job.result = {"scene": index, "generation_seconds": elapsed, "peak_vram_gb": res.get("peak_vram_gb"),
                  "steps_used": res.get("num_inference_steps"), "seed": res.get("seed")}
    seed = res.get("seed") if res.get("seed") is not None else scene.get("seed")
    log.info("MV %s: %s ができました(H3 %s 秒、%d コマ、%.1f 秒)", pid, tag, elapsed, frames, time.monotonic() - started)
    ctx.ledger.update_mv_project(pid, scene_patches={index: {
        "status": "done", "clip": clip.name, "thumb": thumb.name if has_thumb else None, "error": None, "seed": seed,
        "elapsed_s": round(elapsed if elapsed is not None else time.monotonic() - started, 1), "peak_vram_gb": res.get("peak_vram_gb")}})


# ---- mv.assemble ----

def final_path(outputs_dir, pid: str) -> Path:
    """最終の MV の置き場所 outputs/video/mv_<ID>.mp4。結合し直すときは、前の MV(ギャラリーに残っている)を上書きしないよう _2、_3… にする。"""
    base = Path(outputs_dir) / "video"
    path, n = base / f"mv_{pid}.mp4", 1
    while path.exists():
        n += 1
        path = base / f"mv_{pid}_{n}.mp4"
    return path


async def run_assemble(job: Job, ctx: Ctx) -> None:
    if job.cancel_requested:
        raise JobCancelled()
    proj = _project(ctx, job)
    pid, p, scenes = proj["id"], proj["params"], sorted(proj["scenes"], key=lambda s: s["index"])
    if not scenes or any(s["status"] != "done" or not s.get("clip") for s in scenes):
        raise JobFailed("まだ生成できていないシーンがあります")
    work = work_dir(ctx.outputs_dir, pid)
    clips = [work / s["clip"] for s in scenes]
    missing = [c.name for c in clips if not c.is_file()]
    if missing:
        raise JobFailed("シーンのクリップが見つかりません: " + "、".join(missing) + "(そのシーンをやり直してください)")
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
        await _ffmpeg(lambda exe, tmp: mvclip.concat_args(exe, listing, tmp), video_only, "シーンの結合")
        if job.cancel_requested:
            raise JobCancelled()
        job.progress, job.message = 0.6, "元の曲を載せています"
        await _ffmpeg(lambda exe, tmp: mvclip.mux_args(exe, video_only, song, tmp, duration), dest, "曲の差し替え")
    finally:
        listing.unlink(missing_ok=True)
        video_only.unlink(missing_ok=True)
    job.progress, job.message = 0.95, "ギャラリーに登録しています"
    meta = {"project_id": pid, "kind": job.kind, "aspect": p.get("aspect"), "quality": p.get("quality"), "scenes": len(scenes),
            "song_id": p.get("song_id"), "image_id": p.get("image_id"), "width": p.get("width"), "height": p.get("height")}
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", str(p.get("title") or "MV"))[:40] + "_mv.mp4"
    rec = await asyncio.to_thread(ctx.ledger.add_output, "video", dest, job.tab, job.id, name, meta,
                                  width=p.get("width"), height=p.get("height"), duration_s=duration)
    job.outputs.append(rec["id"])
    ctx.ledger.update_mv_project(pid, output_id=rec["id"], params_patch={"assembled": True})
    log.info("MV %s: %d シーンを結合しました → %s(%.1f 秒)", pid, len(scenes), dest.name, duration)


HANDLERS = {
    "mv.analyze": Handler(component=None, normalize=normalize, run=run_analyze, cancel=cancel_task, gpu=False),
    "mv.script": Handler(component=None, normalize=normalize, run=run_script, cancel=cancel_task, gpu=False),
    "mv.scene": Handler(component=h3.COMPONENT, normalize=normalize, run=run_scene, cancel=h3.cancel),
    "mv.assemble": Handler(component=None, normalize=normalize, run=run_assemble, cancel=cancel_flag, gpu=False),
}
