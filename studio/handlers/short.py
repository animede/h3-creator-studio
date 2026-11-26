"""ショート動画のハンドラ: short.analyze / short.plan / short.script / short.scene / short.assemble。

どのジョブをいつ入れるか(順番・一時停止・やり直し)は short.ShortDirector(mv.Director と同じ)が決める。ここは 1 つのジョブの仕事だけをして、結果を台帳の
mv_projects(kind = "short")に書く。ジョブの params は {"project_id", ("scene")} だけで、中身はその都度台帳から読む。MV のハンドラ(handlers/mv.py)の
エンジン(曲の分析・クリップの整形・結合・ギャラリーへの登録)をそのまま使う。
  analyze  : CPU。時間軸で違う ― speech: セリフを AivisSpeech で文ごとに合成してつなぎ(narration.wav。文ごとの時刻は sentences.json)、
             song.wav と vocals.wav の両方として置き、文の切れ目で 5〜10 秒のシーンに束ねる(Demucs は使わない)/ music: MV と同じ(Demucs で分離して分ける)/
             free: 長さを置くだけ(シーンは short.plan が作る)
  plan     : CPU(管理役の LLM)。free だけ。筋(人物の見た目の整理 + 物語の筋)を作り、シーンの秒数と場面を決めて、コマの格子に載せる
  script   : CPU(管理役の LLM)。筋(plan で作っていなければここで作る)をもとに、シーンごとの使う参照(refs)と演出(6 セクション形式。参照なしは t2va 用)を書く
  scene    : H3。参照が無ければ /api/t2va、あれば /api/ref2va(画像は refs の順、speech なら最後にそのシーンの読み上げの切り出し)。
             speech: vocal_lock、seconds は送らない / music: seconds + mute / free: seconds だけ(H3 の音を残す)
  assemble : CPU。クリップをつなぎ、最終の音声(final_audio と時間軸)を付けた MP4 を作ってギャラリーに載せる
作業のファイルは outputs/short/<制作の ID>/ に置く(narration.wav・sentences.json・scene_NNN.mp4 など)。
"""
import asyncio
import hashlib
import io
import json
import logging
import re
import shutil
import time
import wave
from pathlib import Path

from .. import config, mv_audio, mvclip, mvscript, mvsub, shortscript
from ..jobs import Ctx, Handler, Job, JobCancelled, JobFailed
from ..llm import LlmError
from ..mv import concept_from_song, scene_clip_name, scene_thumb_name, scene_vocals_name
from ..short import make_scenes, person_index, plan_speech_scenes, sentence_text, split_sentences, work_dir
from ..tts import TtsError
from . import h3
from . import mv as mvh
from .mv import H3_AUDIO_RATE, H3_MIN_AUDIO_S

log = logging.getLogger("studio.short")

normalize = mvh.normalize
SYNTH_SHARE = 0.9                 # 分析ジョブの進みのうち、読み上げの合成が占める割合(残りは音声の整え・シーン分け)
LABEL = "ショート動画"


async def _run(job: Job, coro):
    """ジョブの仕事 coro を、中止で取り消せるタスクとして走らせる(開始前に中止が来ていれば走らせない)。"""
    if job.cancel_requested:
        coro.close()
        raise JobCancelled()
    await mvh._cancellable(job, coro)


# ---- short.analyze ----

def _wav_pcm(data: bytes) -> tuple:
    """WAV のバイト列 → ((チャンネル数, 幅, 周波数), PCM, 秒)。読めなければ JobFailed。"""
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            return (w.getnchannels(), w.getsampwidth(), w.getframerate()), w.readframes(w.getnframes()), w.getnframes() / w.getframerate()
    except (wave.Error, EOFError, ZeroDivisionError) as e:
        raise JobFailed("AivisSpeech から不正な WAV データが返されました") from e


def _write_wav(path: Path, fmt: tuple, parts: list) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(fmt[0])
        w.setsampwidth(fmt[1])
        w.setframerate(fmt[2])
        for pcm in parts:
            w.writeframes(pcm)


async def synthesize_narration(job: Job, ctx: Ctx, p: dict, work: Path) -> list:
    """セリフを文ごとに合成してつなぎ、work に narration.wav(44.1kHz ステレオにそろえたもの)・song.wav・vocals.wav(どちらも narration.wav と同じ中身。
    シーンのボーカルの切り出しと、MV と同じ流れのため)・sentences.json を置く。文の時刻 [{index, text, start, end, duration}] を返す。
    同じセリフ・話者の結果が残っていれば(失敗のあとの再開)、合成し直さない。"""
    narration, sentences_json = work / "narration.wav", work / "sentences.json"
    digest = hashlib.sha256(f"{p['speaker_id']}\n{p['narration']}".encode()).hexdigest()[:16]
    try:
        cached = json.loads(sentences_json.read_text("utf-8"))
    except (OSError, ValueError):
        cached = None
    if cached and cached.get("digest") == digest and narration.is_file() and (work / "song.wav").is_file() and (work / "vocals.wav").is_file():
        job.message = "合成済みの読み上げを使います"
        return cached["sentences"]
    if ctx.tts is None:
        raise JobFailed("音声合成(AivisSpeech)が設定されていません")
    texts = split_sentences(p["narration"])
    if not texts:
        raise JobFailed("読み上げる文章がありません")
    fmt, parts, sentences, elapsed = None, [], [], 0.0
    for n, text in enumerate(texts, start=1):
        job.progress, job.message = SYNTH_SHARE * (n - 1) / len(texts), f"読み上げを合成しています {n} / {len(texts)} 文"
        try:
            data = await ctx.tts.synthesize_wav(text, int(p["speaker_id"]))
        except TtsError as e:
            raise JobFailed(e.message) from e
        current, pcm, seconds = _wav_pcm(data)
        if fmt is not None and current != fmt:
            raise JobFailed("文ごとの合成音声の形式が一致しません")
        fmt = current
        parts.append(pcm)
        sentences.append({"index": n, "text": text, "start": round(elapsed, 3), "end": round(elapsed + seconds, 3), "duration": round(seconds, 3)})
        elapsed += seconds
    job.progress, job.message = SYNTH_SHARE, "読み上げの音声を整えています"
    raw = work / "narration_raw.wav"
    try:
        await asyncio.to_thread(_write_wav, raw, fmt, parts)
        await mv_audio.normalize(raw, narration)                 # 44.1kHz ステレオの PCM(合成の周波数が違っても、ここでそろう)
    except mv_audio.MvAudioError as e:
        raise JobFailed(e.message, e.log_tail) from e
    finally:
        raw.unlink(missing_ok=True)
    for name in ("song.wav", "vocals.wav"):
        await asyncio.to_thread(shutil.copyfile, narration, work / name)
    await asyncio.to_thread(sentences_json.write_text, json.dumps({"digest": digest, "duration": round(elapsed, 3), "sentences": sentences},
                                                                   ensure_ascii=False), "utf-8")     # 最後に書く(これがあれば全部できている)
    return sentences


async def _analyze(job: Job, ctx: Ctx) -> None:
    proj = mvh._project(ctx, job)
    pid, p = proj["id"], proj["params"]
    work = work_dir(ctx.outputs_dir, pid)
    work.mkdir(parents=True, exist_ok=True)
    timeline, scenes = p["timeline"], []
    if timeline == "speech":
        sentences = await synthesize_narration(job, ctx, p, work)
        job.progress, job.message = 0.95, "読み上げをシーンに分けています"
        plan = plan_speech_scenes(sentences, p["min_s"], p["max_s"])
        for s in plan:
            s["text"] = sentence_text(sentences, s["start"], s["end"], last=s["index"] == len(plan))
        scenes = make_scenes(mvclip.quantize_scene_plan(plan), timeline, person_index(p))
        analysis = {"duration": round(sentences[-1]["end"], 3), "sentences": len(sentences), "scenes": len(scenes)}
    elif timeline == "music":
        planned, analysis = await mvh.analyze_song(job, ctx, proj, work)
        scenes = make_scenes(planned, timeline)
    else:
        analysis = {"duration": float(p["length_s"]), "scenes": 0}          # シーンは short.plan が作る
    if timeline != "free" and not scenes:
        raise JobFailed("シーンに分けられませんでした")
    ctx.ledger.update_mv_project(pid, scenes=scenes, params_patch={"analysis": analysis, "assembled": False})
    job.result = analysis
    log.info("ショート動画 %s: %s で %.1f 秒、%d シーンに分けました", pid, timeline, analysis["duration"], len(scenes))


async def run_analyze(job: Job, ctx: Ctx) -> None:
    await _run(job, _analyze(job, ctx))


# ---- 筋(short.plan と short.script が使う) ----

async def _prepare(job: Job, ctx: Ctx, proj: dict) -> tuple:
    """人物の見た目と筋を作って制作に残し、(筋, 人物の見た目の辞書)を返す。残っていれば作り直さない(plan で作っていれば script は使うだけ)。
    人物の説明が空なら画像を見て作る(MV と同じ。読めなくても止めない)。人物の画像が無ければ見た目は {}。筋が読めなくても {} のまま進める。"""
    p = proj["params"]
    if "outline" in p and p.get("character_facts") is not None:
        return p["outline"], p["character_facts"]
    images = p.get("images") or []
    idx = person_index(p)
    character = {}
    if idx is not None:
        description = str(p.get("character") or "").strip()
        if not description and p.get("character_source") != "none":
            description = await mvh._describe_image(job, ctx, proj, images[idx]["id"])
        job.message = "人物の見た目を整理しています"
        character = await mvscript.character_facts(ctx.llm.chat, description)
    song = ctx.ledger.get_output(p["song_id"]) if p.get("song_id") else None
    job.message = "物語の筋を考えています"
    outline = await shortscript.story_outline(
        ctx.llm.chat, p.get("story") or "", song_concept=concept_from_song(song["params"]) if song else "", narration=p.get("narration") or "",
        character=character, sceneries=[it.get("description") or "" for it in images if it.get("role") != "person"])
    patch = {"outline": outline, "character_facts": character}
    if str(outline.get("title") or "").strip():
        patch["title"] = str(outline["title"]).strip()[:40]        # 筋の題を制作の題にする(それまでは物語の最初の文)
    ctx.ledger.update_mv_project(proj["id"], params_patch=patch)
    return outline, character


# ---- short.plan ----

async def _plan(job: Job, ctx: Ctx) -> None:
    proj = mvh._project(ctx, job)
    pid, p = proj["id"], proj["params"]
    if ctx.llm is None:
        raise JobFailed("LLM が設定されていません")
    try:
        outline, _ = await _prepare(job, ctx, proj)
        job.progress, job.message = 0.5, "シーンの長さと場面を決めています"
        items = await shortscript.plan_scenes(ctx.llm.chat, outline, float(p["length_s"]), p["min_s"], p["max_s"])
    except LlmError as e:
        raise JobFailed(e.message) from e
    except mvscript.ScenarioError as e:
        raise JobFailed(e.message) from e
    plan, t = [], 0.0
    for n, it in enumerate(items, start=1):
        plan.append({"index": n, "start": t, "end": t + float(it["duration"]), "duration": float(it["duration"]), "vocal_ratio": 0.0, "kind": "free",
                     "boundary_kind": "plan", "place": it.get("place"), "summary": it.get("summary")})
        t += float(it["duration"])
    quantized = mvclip.quantize_scene_plan(plan)
    scenes = make_scenes(quantized, "free")
    analysis = {**(p.get("analysis") or {}), "duration": quantized[-1]["end"], "scenes": len(scenes)}
    ctx.ledger.update_mv_project(pid, scenes=scenes, params_patch={"analysis": analysis, "assembled": False})
    job.result = analysis
    log.info("ショート動画 %s: 物語を %d シーン(%.1f 秒)に割りました", pid, len(scenes), analysis["duration"])


async def run_plan(job: Job, ctx: Ctx) -> None:
    await _run(job, _plan(job, ctx))


# ---- short.script ----

def _load_sentences(work: Path) -> list:
    try:
        return json.loads((work / "sentences.json").read_text("utf-8")).get("sentences") or []
    except (OSError, ValueError):
        return []


async def _script(job: Job, ctx: Ctx) -> None:
    proj = mvh._project(ctx, job)
    pid, p, scenes = proj["id"], proj["params"], proj["scenes"]
    if ctx.llm is None:
        raise JobFailed("LLM が設定されていません")
    images = p.get("images") or []

    def on_progress(done: int, total: int) -> None:
        job.progress, job.message = done / total, f"演出を書いています {done} / {total} シーン"
    try:
        outline, character = await _prepare(job, ctx, proj)
        job.message = f"演出を書いています 0 / {len(scenes)} シーン"
        sentences = await asyncio.to_thread(_load_sentences, work_dir(ctx.outputs_dir, pid)) if p["timeline"] == "speech" else None
        result = await shortscript.generate_scenario(
            ctx.llm.chat, scenes, outline, character, [it.get("description") or "" for it in images], timeline=p["timeline"],
            frame=mvscript.frame_kind(p["width"], p["height"]), background_text=p.get("background_text") or "auto",
            roles=[it.get("role") for it in images], narration_sentences=sentences, style=p.get("style") or "none",
            on_progress=on_progress)
    except LlmError as e:
        raise JobFailed(e.message) from e
    except mvscript.ScenarioError as e:
        raise JobFailed(e.message) from e
    items = {int(it["index"]): it for it in result["scenes"]}
    seed_base = p["seed"] if p.get("seed") is not None else int(hashlib.sha256(pid.encode()).hexdigest()[:8], 16)
    patches = {}
    for s in scenes:
        it = items[s["index"]]
        refs = [int(r) for r in it.get("refs") or [] if isinstance(r, int) and 0 <= r < len(images)]
        patches[s["index"]] = {"prompt": str(it["prompt"]).strip(), "emotion": str(it.get("emotion") or ""), "place": str(it.get("place") or s.get("place") or ""),
                               "refs": refs, "seed": mvclip.scene_seed(seed_base, s["index"])}
    ctx.ledger.update_mv_project(pid, scene_patches=patches, params_patch={"script": result["overview"]})
    log.info("ショート動画 %s: %d シーンの演出を書きました", pid, len(scenes))


async def run_script(job: Job, ctx: Ctx) -> None:
    await _run(job, _script(job, ctx))


# ---- short.scene ----

async def run_scene(job: Job, ctx: Ctx) -> None:
    if job.cancel_requested:
        raise JobCancelled()
    proj = mvh._project(ctx, job)
    pid, index, p, scenes = proj["id"], job.params["scene"], proj["params"], proj["scenes"]
    scene = next((s for s in scenes if s["index"] == index), None)
    if scene is None:
        raise JobFailed(f"シーン {index} がありません")
    if not scene.get("prompt"):
        raise JobFailed("このシーンの演出がまだありません(演出の作成からやり直してください)")
    tag = f"シーン {index} / {len(scenes)}"
    images, timeline = p.get("images") or [], p["timeline"]
    refs = [r for r in scene.get("refs") or []]
    if any(not isinstance(r, int) or not 0 <= r < len(images) for r in refs):
        raise JobFailed("このシーンの参照画像の指定が正しくありません(演出の作成からやり直してください)")
    speech = bool(scene.get("speech")) and timeline == "speech"
    work = work_dir(ctx.outputs_dir, pid)
    vocals, raw, clip, thumb = work / "vocals.wav", work / f"scene_{index:03d}_raw.mp4", work / scene_clip_name(index), work / scene_thumb_name(index)
    if speech and not vocals.is_file():
        raise JobFailed("読み上げの音声が見つかりません(分析からやり直してください)")
    started = time.monotonic()
    data = {"prompt": scene["prompt"], "width": str(p["width"]), "height": str(p["height"])}
    wav = None
    if speech:
        job.progress, job.message = 0.0, f"{tag}: 読み上げを切り出しています"
        try:
            wav = await mv_audio.cut(vocals, scene["start"], scene["end"], work / scene_vocals_name(index), rate=H3_AUDIO_RATE, channels=1,
                                     pad_to=H3_MIN_AUDIO_S)
        except mv_audio.MvAudioError as e:
            raise JobFailed(e.message, e.log_tail) from e
        if job.cancel_requested:
            raise JobCancelled()
        data["vocal_lock"] = "true"        # 口を合わせる。seconds は送らない(= 読み上げの長さ)
    else:
        job.progress, job.message = 0.0, f"{tag}: 準備しています"
        # 尺は seconds で明示(H3 は 17n+5 コマに丸めて短く返すことがあり、足りない分は generate_clip が足す)。曲のときは H3 の音を使わない(mute)。
        # どちらも無いとき(free)は H3 が作る環境音・効果音を残す
        data["seconds"] = f"{min(h3.MAX_SECONDS, max(H3_MIN_AUDIO_S, float(scene['duration']))):g}"
        if timeline == "music":
            data["mute"] = "true"
    if scene.get("seed") is not None:
        data["seed"] = str(scene["seed"])
    # 参照は refs の順に画像(プロンプトの <Picture k> の k は、この並びの 1 始まり)、speech なら最後に、そのシーンの読み上げ(<Audio 1>)
    files = await asyncio.to_thread(lambda: [h3._load_file(ctx, "references", images[r]["id"], "参照画像") for r in refs]
                                    + ([h3._file_part("references", wav)] if wav is not None else []))
    endpoint = h3.ENDPOINTS["ref2va" if files else "t2va"]
    res, frames = await mvh.generate_clip(job, ctx, endpoint, data, files, tag=tag, scene=scene, p=p, raw=raw, clip=clip, keep_audio=timeline == "free")
    await mvh.record_scene(job, ctx, LABEL, pid, scene, res, clip, thumb, started, frames, tag)


# ---- short.assemble ----

def _name(p: dict) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", str(p.get("title") or LABEL))[:40] + "_short.mp4"


async def run_assemble(job: Job, ctx: Ctx) -> None:
    await _run(job, _assemble(job, ctx))          # 中止は、走っている ffmpeg ごと止める(テロップの焼き込みは映像を作り直すので、長い動画では数分かかる)


async def _assemble(job: Job, ctx: Ctx) -> None:
    proj = mvh._project(ctx, job)
    pid, p = proj["id"], proj["params"]
    work = work_dir(ctx.outputs_dir, pid)
    scenes, clips = mvh.ready_clips(proj, work)
    timeline, final_audio = p["timeline"], p.get("final_audio") or "auto"
    duration = float((p.get("analysis") or {}).get("duration") or sum(s["duration"] for s in scenes))
    # 音声: 無し(final_audio = none)/ 読み上げ(+ 曲を BGM)/ 曲 / free は H3 の音(クリップに残してある)をそのまま
    narration = song = song_s = None
    if final_audio != "none":
        if timeline == "speech":
            narration = work / "narration.wav"
            if not narration.is_file():
                raise JobFailed("読み上げの音声が見つかりません(分析からやり直してください)")
        if timeline == "music" or (timeline == "speech" and p.get("song_id")):
            rec = ctx.ledger.get_output(p["song_id"])
            local = work / "song.wav"                  # 曲だけ(music)は、分析でそろえた曲。読み上げ(speech)の song.wav は読み上げ自身なので、BGM は台帳の曲
            song = local if timeline == "music" and local.is_file() else ctx.ledger.abs_path(rec["path"]) if rec is not None else None
            if song is None or not song.is_file():
                raise JobFailed("曲のファイルが見つかりません")
            song_s = (rec or {}).get("duration_s")
    listing, video_only, dest = work / "concat.txt", work / "video_only.mp4", mvh.final_path(ctx.outputs_dir, pid, "short")
    # セリフのテロップ(字幕): 読み上げの文の時刻から ASS(焼き込み)と SRT(配布用)を作る。セリフが無い制作では何もしない
    ass = srt = None
    if timeline == "speech" and p.get("subtitles"):
        sentences = _load_sentences(work)
        if not sentences:
            raise JobFailed("読み上げの文の時刻が見つかりません(分析からやり直してください)")
        ass, srt = work / "subtitles.ass", work / "subtitles.srt"
        count = await asyncio.to_thread(mvsub.write_subtitles, sentences, p["width"], p["height"], ass, srt, _subtitle_font())
        log.info("ショート動画 %s: テロップを %d 枚作りました", pid, count)
    final_concat = narration is None and song is None and ass is None     # 音声もテロップも足さない(音声なし・free)なら、つないだものがそのまま最終の動画
    try:
        job.progress, job.message = 0.1, "シーンをつないでいます"
        await asyncio.to_thread(listing.write_text, mvclip.concat_list(clips), "utf-8")
        await mvh._ffmpeg(lambda exe, tmp: mvclip.concat_args(exe, listing, tmp, drop_audio=final_audio == "none", faststart=final_concat),
                          dest if final_concat else video_only, "シーンの結合", duration)
        if not final_concat:
            if job.cancel_requested:
                raise JobCancelled()
            what = "(テロップを焼き込みながら)" if ass is not None else ""
            if narration is not None and song is not None:
                job.progress, job.message = 0.6, f"読み上げに曲(BGM)を混ぜて載せています{what}"
                await mvh._ffmpeg(lambda exe, tmp: mvclip.mix_args(exe, video_only, narration, song, tmp, duration, song_s, ass=ass), dest, "読み上げと曲の混合",
                                  duration)
            elif narration is not None:
                job.progress, job.message = 0.6, f"読み上げを載せています{what}"
                await mvh._ffmpeg(lambda exe, tmp: mvclip.mux_args(exe, video_only, narration, tmp, duration, ass=ass), dest, "読み上げの差し替え", duration)
            elif song is not None:
                job.progress, job.message = 0.6, "元の曲を載せています"
                await mvh._ffmpeg(lambda exe, tmp: mvclip.mux_args(exe, video_only, song, tmp, duration), dest, "曲の差し替え", duration)
            else:
                job.progress, job.message = 0.6, "テロップを焼き込んでいます"
                await mvh._ffmpeg(lambda exe, tmp: mvclip.burn_args(exe, video_only, ass, tmp), dest, "テロップの焼き込み", duration)
    finally:
        listing.unlink(missing_ok=True)
        video_only.unlink(missing_ok=True)
    srt_name = None
    if srt is not None:
        srt_name = dest.with_suffix(".srt").name                # 最終の動画の隣に SRT も置く(配布・投稿サイトの字幕用)
        await asyncio.to_thread(shutil.copyfile, srt, dest.with_suffix(".srt"))
    job.progress, job.message = 0.95, "ギャラリーに登録しています"
    meta = {"project_id": pid, "kind": job.kind, "timeline": timeline, "final_audio": final_audio, "subtitles": ass is not None, "srt": srt_name,
            "aspect": p.get("aspect"),
            "quality": p.get("quality"), "scenes": len(scenes), "song_id": p.get("song_id"), "images": [it["id"] for it in p.get("images") or []],
            "width": p.get("width"), "height": p.get("height")}
    await mvh.register_video(job, ctx, pid, dest, _name(p), meta, p, duration)
    log.info("ショート動画 %s: %d シーンを結合しました(音声: %s)→ %s(%.1f 秒)", pid, len(scenes),
             "なし" if final_audio == "none" else {"speech": "読み上げ" + ("+BGM" if song else ""), "music": "曲", "free": "H3 の音"}[timeline]
             + ("、テロップあり" if ass is not None else ""), dest.name, duration)


def _subtitle_font() -> str:
    """テロップの字体([mv] subtitle_font。空なら mvsub の既定)。"""
    return str(config.settings.mv.get("subtitle_font") or "").strip()


HANDLERS = {
    "short.analyze": Handler(component=None, normalize=normalize, run=run_analyze, cancel=mvh.cancel_task, gpu=False),
    "short.plan": Handler(component=None, normalize=normalize, run=run_plan, cancel=mvh.cancel_task, gpu=False),
    "short.script": Handler(component=None, normalize=normalize, run=run_script, cancel=mvh.cancel_task, gpu=False),
    "short.scene": Handler(component=h3.COMPONENT, normalize=normalize, run=run_scene, cancel=h3.cancel),
    "short.assemble": Handler(component=None, normalize=normalize, run=run_assemble, cancel=mvh.cancel_task, gpu=False),
}
