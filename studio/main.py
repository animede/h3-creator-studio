"""管理役の FastAPI アプリ。画面(web/)と JSON API を 1 オリジンで配る。"""
import argparse
import asyncio
import contextlib
import json
import logging
import mimetypes
import re
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Literal, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import editclip, genres, mv_audio, presets, procman
from .components import ComponentNotInstalled, all_specs
from .config import REPO_ROOT, settings
from .edit import EditManager, ExportBusy
from .enhance import EnhanceError, enhance_prompt
from .genres import GenreError
from .gpu import GpuMonitor
from .handlers import HANDLERS
from .jobs import TERMINAL, Ctx, JobRunner
from .ledger import Ledger
from .llm import Llm, LlmError
from .mv import Director, concept_from_song, pick_image, pick_song, song_has_info
from .mvdescribe import describe_character, listen_song, propose_concept
from .presets import PresetError
from .scheduler import Scheduler
from .short import ShortDirector, propose_narration
from .handlers import ace as ace_handler
from .songplan import SongPlanError, make_caption, make_plan
from .tts import Tts, TtsError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


class _QuietPolling(logging.Filter):
    """画面が 1〜2 秒おきに取りに来る状態・進捗のアクセスログは出さない(端末が埋まって肝心の行が見えなくなる)。"""
    _QUIET = ("/api/status", "/api/jobs/", "/api/jobs?", "/api/operations/", "/api/gallery", "/api/llm/status",
              "/api/components/", "/api/mv", "/api/short", "/api/edit", "/api/presets", "/outputs/", "/app.js", "/style.css", "/gallery.js", "/jobs.js",
              "/presets.js", "/image.js", "/video.js", "/music.js", "/mv.js", "/short.js", "/edit.js", "/dnd.js", "/viz.js", "/favicon.ico")

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not ('"GET ' in msg and any(q in msg for q in self._QUIET))


logging.getLogger("uvicorn.access").addFilter(_QuietPolling())

OUTPUTS_DIR = settings.root / "outputs"
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_KINDS = {"image": "image", "video": "video", "audio": "audio"}   # MIME の大分類 -> 台帳の kind


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.sched = sched = Scheduler()
    app.state.ledger = ledger = Ledger(OUTPUTS_DIR)
    stale = await asyncio.to_thread(ledger.remove_stale_parts)        # 前回の強制終了で残った作りかけ(*.part)。起動時はまだ何も書いていない
    if stale:
        logging.getLogger("studio.main").info("作りかけのファイル(*.part)を %d 個消しました", stale)
    app.state.tts = Tts(sched, ledger, OUTPUTS_DIR)
    app.state.llm = llm = Llm(settings)
    app.state.jobs = runner = JobRunner(Ctx(sched, ledger, OUTPUTS_DIR, llm=llm, tts=app.state.tts), HANDLERS)
    app.state.mv = director = Director(runner, ledger, OUTPUTS_DIR)
    director.restore()       # 再起動で途切れた MV の制作は、一時停止にして残す
    app.state.short = short_director = ShortDirector(runner, ledger, OUTPUTS_DIR)
    short_director.restore()       # ショート動画も同じ
    app.state.edit = EditManager(runner, ledger, OUTPUTS_DIR)      # 編集タブ(ジョブは再起動で消えるので、復元するものは無い)
    llm.busy = lambda: runner.gpu_pending() or sched.gpu_switching()     # GPU の仕事が走っている(待っている)間、LLM は CUDA を隠して CPU だけで動く
    app.state.monitor = monitor = GpuMonitor(rss_provider=sched.rss_map, generating=runner.is_generating)
    sched.monitor = monitor
    monitor.start()
    sched.configure_gpu(settings.gpu_mode)       # exclusive / share を、総 VRAM(監視の最初の値)で決める
    runner.start()
    await sched.adopt_running()
    yield
    await runner.stop()
    await llm.stop()
    monitor.stop()
    ledger.close()


app = FastAPI(title="YUZU Creator Studio", lifespan=lifespan)


def _comp(name: str):
    c = app.state.sched.comps.get(name)
    if c is None:
        raise HTTPException(404, f"未知の部品です: {name}")
    return c


@app.get("/api/status")
def api_status():
    sched, snap = app.state.sched, app.state.monitor.snapshot()
    return {"components": sched.status(), "gpu": snap["gpu"], "ram": snap["ram"], "shared_gpu_mib": snap["shared_gpu_mib"],
            "shared_gpu_total_mib": snap.get("shared_gpu_total_mib"),          # 共有 GPU メモリの上限(Windows だけ。RAM の半分。他は null)
            "warning": snap["warning"], "operation": sched.current_operation(), "busy": sched.is_busy(), "ts": time.time(),
            "jobs_done_counter": app.state.jobs.done_counter,
            "gpu_mode": sched.mode, "residents": sched.resident_status(), "lanes": app.state.jobs.lane_status()}


@app.post("/api/components/{name}/{action}")
async def api_component_action(name: str, action: str):
    if action not in ("start", "stop", "ensure"):
        raise HTTPException(404, "start / stop / ensure のいずれかです")
    comp = _comp(name)
    if comp.spec.uses_gpu and action == "ensure":
        plan = app.state.sched.plan(name)
        if plan.action == "wait":      # 仕事中の部品を降ろさないと載らない: 待たせずに断る(画面の操作が、生成の終わりまで固まらないように)
            busy = "、".join(app.state.sched.comps[n].spec.label for n in plan.blockers) or "別の部品"
            raise HTTPException(409, f"{comp.spec.label} を GPU に載せられません({busy} が生成中のため)。生成が終わってからもう一度押してください")
    if comp.spec.uses_gpu and action in ("start", "ensure"):
        await app.state.llm.release_before_gpu()      # 管理役の LLM が VRAM を持っていたら返す
    return {"operation_id": app.state.sched.launch(action, name).id}


@app.get("/api/operations/{op_id}")
def api_operation(op_id: str):
    op = app.state.sched.operations.get(op_id)
    if op is None:
        raise HTTPException(404, "その操作は見つかりません")
    return op.to_dict()


@app.get("/api/components/{name}/log")
def api_log(name: str, lines: int = 100):
    _comp(name)
    return {"name": name, "log": procman.log_tail(name, max(1, min(lines, 1000)))}


@app.get("/api/config")
def api_config():
    comps = {}
    for name, spec in all_specs().items():
        try:
            spec.build_command(spec.dir)
            installed, hint = True, None
        except ComponentNotInstalled as e:
            installed, hint = False, e.hint
        comps[name] = {"label": spec.label, "dir": str(spec.dir), "dir_exists": spec.dir.is_dir(), "port": spec.port,
                       "installed": installed, "hint": hint, "uses_gpu": spec.uses_gpu}
    return {"host": settings.host, "port": settings.port, "repo_root": str(REPO_ROOT),
            "components_dir": settings.components_dir, "components": comps}


# ---- ジョブ ----

class JobIn(BaseModel):
    tab: Literal["image", "video", "music"]
    kind: str
    params: dict = {}


# /api/jobs から入れられるジョブの種類(画像・動画・音楽の部品のジョブだけ)。MV・ショート動画のジョブは Director、編集のジョブは EditManager だけが入れる
# (tab を絞っても kind は自由に書けたので、制作の外から mv.assemble や、検証していない state の edit.export を入れられた)
PUBLIC_JOB_PREFIXES = ("h3image.", "h3.", "ace.")


@app.post("/api/jobs", status_code=202)
def api_job_submit(body: JobIn):
    if not body.kind.startswith(PUBLIC_JOB_PREFIXES) or body.kind not in HANDLERS:
        raise HTTPException(400, f"このジョブの種類は受け付けていません: {body.kind}(画像・動画・音楽のジョブだけを入れられます)")
    try:
        job = app.state.jobs.submit(body.tab, body.kind, body.params)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"id": job.id}


@app.get("/api/jobs")
def api_job_list(tab: str = None, limit: int = 50):
    return app.state.jobs.list(tab, max(1, min(limit, 200)))


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str):
    job = app.state.jobs.get(job_id)
    if job is None:
        past = app.state.ledger.get_job(job_id)
        if past is None:
            raise HTTPException(404, "そのジョブは見つかりません")
        return past
    return job.to_dict()


@app.post("/api/jobs/{job_id}/cancel")
async def api_job_cancel(job_id: str):
    job = await app.state.jobs.cancel(job_id)
    if job is None:
        raise HTTPException(404, "そのジョブは見つかりません(完了済みで一覧から消えた可能性があります)")
    return job.to_dict()


# ---- LLM(プロンプト強化・作詞) ----

@app.get("/api/llm/status")
def api_llm_status():
    return app.state.llm.status()


class EnhanceIn(BaseModel):
    tab: Literal["image", "video"]
    text: str
    mode: Optional[str] = None            # 動画: brief / storyboard / translate / h3-official
    seconds: Optional[float] = None       # 動画: storyboard 用の総尺
    task: Optional[str] = None            # 動画: t2va / fl2va / ref2va(h3-official)
    lang: Optional[str] = None            # 動画: en / ja(h3-official)
    references: list[str] = []            # 画像: 参照画像の生成物 ID(あれば編集用の強化)


@app.post("/api/enhance")
async def api_enhance(body: EnhanceIn):
    """プロンプトを、その部品が持つ強化の仕組み(管理役の LLM を使う)で書き直す。"""
    try:
        return await enhance_prompt(app.state.jobs, **body.model_dump())
    except EnhanceError as e:
        raise HTTPException(e.status, e.message)


class SongPlanIn(BaseModel):
    genre: str = "J-POP"
    language: str = "Japanese"
    mood: str = ""
    vocal: str = ""
    theme: str = ""
    caption: str = ""                     # 利用者が書いたキャプション(あれば、AI が作るテーマ・歌詞がその雰囲気に沿う。AI が作ったものは入れない)
    instrumental: bool = False


def _genre(text: str) -> str:
    """ジャンル(チップの名前か、自由に書いた文。1 行にして 200 文字まで)。空なら J-POP。"""
    try:
        return genres.clean_genre(text) or "J-POP"
    except ValueError as e:
        raise HTTPException(400, str(e))


def _caption(text: str) -> str:
    """利用者が書いたキャプション(テーマ・歌詞を作るときの参考。生成のジョブの caption と同じ 1000 文字まで)。空は ""。"""
    text = (text or "").strip()
    if len(text) > ace_handler.CAPTION_MAX:
        raise HTTPException(400, f"caption は {ace_handler.CAPTION_MAX} 文字以内にしてください")
    return text


@app.post("/api/songplan")
async def api_songplan(body: SongPlanIn):
    """テーマ・歌詞・キャプションを LLM で作って返す(生成はしない。「歌詞だけ作る」にも使える)。生成中は LLM が CPU 専用になり、遅くなる。
    caption(任意)は、利用者が書いたキャプション。あれば、作るテーマ・歌詞がその雰囲気に沿う。"""
    try:
        return await make_plan(app.state.llm.chat, _genre(body.genre), body.language, body.mood, body.vocal, body.theme.strip(),
                               want_lyrics=not body.instrumental, custom=genres.custom_map(app.state.ledger), user_caption=_caption(body.caption))
    except (LlmError, SongPlanError) as e:
        raise HTTPException(503, str(e))


# ---- 音楽タブの「作る」ボタン(テーマ・歌詞・キャプション。管理役の LLM を使う。生成中でも作れる) ----

class MusicIn(BaseModel):
    genre: str = "J-POP"
    language: str = "Japanese"
    theme: str = ""
    lyrics: str = ""
    caption: str = ""                     # 利用者が書いたキャプション(テーマ・歌詞を作るときだけ使う。あれば、その雰囲気に沿う。AI が作ったものは入れない)
    mood: str = ""
    vocal: str = ""
    thinking: bool = True
    instrumental: bool = False


@app.post("/api/music/theme")
async def api_music_theme(body: MusicIn):
    """ジャンルと言語から、曲のテーマを 1 つ作る。利用者が書いたキャプション(caption)があれば、その雰囲気に沿わせる。"""
    try:
        plan = await make_plan(app.state.llm.chat, _genre(body.genre), body.language, want_lyrics=False, want_caption=False,
                               custom=genres.custom_map(app.state.ledger), user_caption=_caption(body.caption))
    except (LlmError, SongPlanError) as e:
        raise HTTPException(503, str(e))
    return {"theme": plan["theme"]}


@app.post("/api/music/lyrics")
async def api_music_lyrics(body: MusicIn):
    """歌詞を作る。テーマがあればそのテーマで書き、無ければテーマも作る。利用者が書いたキャプション(caption)があれば、その雰囲気に沿わせる。"""
    try:
        plan = await make_plan(app.state.llm.chat, _genre(body.genre), body.language, body.mood, body.vocal, body.theme.strip(),
                               want_lyrics=True, want_caption=False, custom=genres.custom_map(app.state.ledger), user_caption=_caption(body.caption))
    except (LlmError, SongPlanError) as e:
        raise HTTPException(503, str(e))
    return {"theme": plan["theme"], "lyrics": plan["lyrics"], "recommended_duration": plan["recommended_duration"]}


@app.post("/api/music/caption")
async def api_music_caption(body: MusicIn):
    """テーマ・歌詞・ムード・ボーカルから、ACE-Step に渡すキャプション(英語)を作る(書いてあるキャプションは、作り直すものなので使わない)。"""
    try:
        caption = await make_caption(app.state.llm.chat, _genre(body.genre), body.theme.strip(), body.lyrics, body.mood, body.vocal,
                                     body.language, body.thinking, body.instrumental, custom=genres.custom_map(app.state.ledger))
    except (LlmError, SongPlanError) as e:
        raise HTTPException(503, str(e))
    return {"caption": caption}


@app.get("/api/music/models")
async def api_music_models():
    """ACE の DiT モデル一覧(読込済み・VRAM に載るか・既定)。ACE は起こさず、動いていなければ空の一覧を返す。"""
    if "ace" not in app.state.sched.comps:          # studio.toml で ACE を無効にしている: 画面が落ちないよう、空の一覧で答える
        return {"models": [], "default_model": None, "note": "音楽の部品(ACE-Step)は無効になっています"}
    spec = _comp("ace").spec
    return await ace_handler.list_models(spec.base_url, app.state.jobs.ctx.transport)


# ---- 音楽タブのジャンル(同梱の 12 と、自作。自作は名前から LLM で定義を作って台帳に保存する) ----

class GenreIn(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None      # 一行の説明(任意。自由に書いた文をジャンルとして保存するときは、その文)


class GenrePatch(BaseModel):
    name: Optional[str] = None
    vibe: Optional[str] = None
    settings: Any = None                   # 場面の配列(形の検査は genres.check_seeds。日本語の 400)
    emotions: Any = None
    viz: Optional[str] = None


@contextlib.contextmanager
def _genre_errors():
    """ジャンルの操作の失敗を HTTP のエラーにする(理由は日本語)。"""
    try:
        yield
    except GenreError as e:
        raise HTTPException(e.status, e.message)


@app.get("/api/music/genres")
def api_music_genres():
    """同梱のジャンル(builtin: true)、自作のジャンル(作った順)。"""
    return genres.list_genres(app.state.ledger)


@app.post("/api/music/genres")
async def api_music_genres_create(body: GenreIn):
    """名前(と任意の説明)から、LLM でジャンルの定義(世界観・場面・感情・スペクトラムの雰囲気)を作って保存し、返す。CPU では 10〜60 秒かかる。"""
    with _genre_errors():
        return await genres.create_genre(app.state.ledger, app.state.llm.chat, body.name, body.description)


@app.put("/api/music/genres/{gid}")
def api_music_genres_update(gid: str, body: GenrePatch):
    """自作のジャンルの、渡した項目だけを書き換える(手での編集)。同梱のジャンルは 400。"""
    with _genre_errors():
        return genres.update_genre(app.state.ledger, gid, **{k: v for k, v in body.model_dump().items() if v is not None})


@app.delete("/api/music/genres/{gid}")
def api_music_genres_delete(gid: str):
    with _genre_errors():
        genres.delete_genre(app.state.ledger, gid)
    return {"ok": True}


# ---- 音声合成(AivisSpeech) ----

class TtsIn(BaseModel):
    text: str
    style_id: int
    speed: float = 1.0


@app.get("/api/tts/speakers")
async def api_tts_speakers():
    try:
        return await app.state.tts.speakers()
    except TtsError as e:
        raise HTTPException(e.status, e.message)


@app.post("/api/tts")
async def api_tts(body: TtsIn):
    try:
        return _public(await app.state.tts.synthesize(body.text, body.style_id, body.speed))
    except TtsError as e:
        raise HTTPException(e.status, e.message)


# ---- MV(ミュージックビデオ。歌わせる・歌わせないの 2 通り) ----

class MvIn(BaseModel):
    song_id: str = ""
    image_id: str = ""
    character: str = ""            # 人物の説明(空なら、画像のプロンプト。それも無ければ中立の文)
    concept: str = ""              # 曲の内容・世界観(空なら、曲のテーマ・キャプション・ジャンル)
    aspect: str = "16:9"           # 16:9 / 9:16 / 1:1 / 4:5
    quality: str = "fast"          # fast / standard / high
    min_s: float = 5.0             # シーンの長さ(5〜10 秒)
    max_s: float = 10.0
    seed: Optional[int] = None
    background_text: str = "auto"  # 背景の文字の扱い: auto / large / none(画面には出さない)
    lipsync: bool = True           # 歌わせる(リップシンク)か、歌わせない(イメージ MV。H3 に音声を渡さない)か


class MvRetryIn(BaseModel):
    prompt: Optional[str] = None   # 演出(H3 に渡すプロンプト全文)を書き換えるとき
    seed: Optional[int] = None


@contextlib.contextmanager
def _mv_errors():
    """制作の操作の失敗を HTTP のエラーにする(見つからない → 404、入力や状態が合わない → 400。理由はどちらも日本語)。"""
    try:
        yield
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


def _mv_public(view: Optional[dict]) -> Optional[dict]:
    """制作の辞書の最終の MV(台帳の行)を、ギャラリーと同じ形(url つき)にする。"""
    return {**view, "output": _public(view["output"]) if view.get("output") else None} if view else view


@app.get("/api/mv/tools")
def api_mv_tools():
    """MV に要る道具(Demucs と librosa の入った Python、ffmpeg)があるか。無ければ、画面に出す案内(hint)つきで知らせる。"""
    hint = None
    try:
        mv_audio.tools_python()
    except mv_audio.MvToolsMissing as e:
        hint = e.message
    if hint is None and shutil.which("ffmpeg") is None:
        hint = "ffmpeg が見つかりません(PATH に入れてください)"
    return {"available": hint is None, "hint": hint}


class DescribeImageIn(BaseModel):
    image_id: str = ""


class DescribeSongIn(BaseModel):
    song_id: str = ""


@app.post("/api/mv/describe/image")
async def api_mv_describe_image(body: DescribeImageIn):
    """画像を管理役の LLM(Gemma 4 の画像入力)が見て、人物の説明(日本語の 1 行。見た目だけ)を作る。入力欄を埋める用で、制作は作らない。
    {text, facts(英語の特徴), source, note}。source は vision(画像を見た)/ prompt(画像を読めなかったので、画像の台帳のプロンプトから)/
    default(どちらも無く、text は空。理由は note)。生成中は LLM が CPU 専用になり、遅くなる(CPU では 10〜60 秒ほど)。"""
    with _mv_errors():
        rec = pick_image(app.state.ledger, body.image_id)
    try:
        return await describe_character(app.state.llm, app.state.ledger.abs_path(rec["path"]), str(rec["params"].get("prompt") or ""))
    except LlmError as e:
        raise HTTPException(503, e.message)


@app.post("/api/mv/describe/song")
async def api_mv_describe_song(body: DescribeSongIn):
    """曲の内容・世界観の案(日本語 2〜3 文)を作る。入力欄を埋める用で、制作は作らない。{text, source, listen}。
    音楽タブの曲(台帳にテーマ・キャプション・ジャンルがある)は、聞かずに、その params から書く(source = "params"、listen は無い)。
    外から持ち込んだ曲は、管理役の LLM(Gemma 4 の音声入力)が曲の 30 秒を最大 2 か所聞いて、言語・ジャンル・雰囲気・楽器・ボーカル・テンポを読み取り
    (listen。歌詞は聞き取らない)、そこから書く(source = "listen")。生成中は LLM が CPU 専用になり、遅くなる(CPU では 20〜60 秒ほど)。GPU も ACE-Step も使わない。"""
    with _mv_errors():
        rec = pick_song(app.state.ledger, body.song_id)
    params = rec["params"]
    try:
        if song_has_info(params):
            return {"text": await propose_concept(app.state.llm, {}, params), "source": "params", "listen": None}
        heard = await listen_song(app.state.llm, app.state.ledger.abs_path(rec["path"]), rec.get("duration_s"))
        return {"text": await propose_concept(app.state.llm, heard, params), "source": "listen", "listen": heard}
    except (LlmError, mv_audio.MvAudioError) as e:
        raise HTTPException(503, e.message)


@app.get("/api/mv")
def api_mv_list(limit: int = 100):
    """制作の一覧(新しい順。シーンの中身は含めない)。"""
    return [_mv_public(v) for v in app.state.mv.list(max(1, min(limit, 500)))]


@app.post("/api/mv")
async def api_mv_create(body: MvIn):
    with _mv_errors():
        return _mv_public(app.state.mv.create(body.model_dump()))


@app.get("/api/mv/{pid}")
def api_mv_get(pid: str):
    view = app.state.mv.view(pid)
    if view is None:
        raise HTTPException(404, "その MV の制作は見つかりません(削除されたかもしれません)")
    return _mv_public(view)


@app.post("/api/mv/{pid}/pause")
async def api_mv_pause(pid: str):
    with _mv_errors():
        return _mv_public(await app.state.mv.pause(pid))


@app.post("/api/mv/{pid}/resume")
async def api_mv_resume(pid: str):
    with _mv_errors():
        return _mv_public(app.state.mv.resume(pid))


@app.post("/api/mv/{pid}/cancel")
async def api_mv_cancel(pid: str):
    with _mv_errors():
        return _mv_public(await app.state.mv.cancel(pid))


@app.post("/api/mv/{pid}/reassemble")
async def api_mv_reassemble(pid: str):
    with _mv_errors():
        return _mv_public(app.state.mv.reassemble(pid))


@app.post("/api/mv/{pid}/scenes/{index}/retry")
async def api_mv_retry_scene(pid: str, index: int, body: Optional[MvRetryIn] = None):
    body = body or MvRetryIn()
    with _mv_errors():
        return _mv_public(app.state.mv.retry_scene(pid, index, body.prompt, body.seed))


@app.delete("/api/mv/{pid}")
async def api_mv_delete(pid: str, with_output: bool = False):
    """制作を消す(作業フォルダごと。走っていれば、ジョブを中止して)。ギャラリーの最終の MV は残る。
    with_output=1 なら、その制作の MV(結合し直す前の版も)もギャラリーから消す。まだ要るものは消さず、結果の refused に理由を入れる。"""
    with _mv_errors():
        ids = app.state.mv.output_ids(pid) if with_output else []
        await app.state.mv.delete(pid)
    return {"ok": True, **(await asyncio.to_thread(_delete_outputs, ids) if with_output else {})}


# ---- ショート動画(MV と同じ制作の管理。kind = short) ----

class ShortImageIn(BaseModel):
    id: str = ""
    role: str = ""                 # person(人物。1 枚まで)/ scenery(景色・物)
    description: str = ""          # 画面の説明(空なら、画像の台帳のプロンプト)


class ShortIn(BaseModel):
    story: str = ""                # 物語・映像のイメージ(曲もセリフも無ければ必須)
    song_id: str = ""              # 曲(BGM。あれば)
    narration: str = ""            # セリフ・ナレーション(あれば AivisSpeech で読み上げ、人物が口を合わせる)
    speaker_id: Optional[int] = None
    images: list[ShortImageIn] = Field(default_factory=list)       # 参照画像 0〜4 枚
    length_s: Optional[float] = None     # 曲もセリフも無いときの長さ(30 秒以上。空なら 60)
    aspect: str = "16:9"
    quality: str = "fast"
    min_s: float = 5.0
    max_s: float = 10.0
    seed: Optional[int] = None
    background_text: str = "auto"
    final_audio: str = "auto"      # auto / none(映像だけ)
    subtitles: bool = False        # セリフのテロップ(字幕)を焼き込む(セリフがあるときだけ意味がある)
    character: str = ""            # 人物の説明(空なら、画像のプロンプト → 画像を見て作る)


class ShortReassembleIn(BaseModel):
    final_audio: Optional[str] = None      # 渡せば、制作の設定を書き換えてから結合し直す
    subtitles: Optional[bool] = None       # 同上(セリフのテロップの焼き込み)


class DescribeNarrationIn(BaseModel):
    story: str = ""
    song_id: str = ""


@app.post("/api/short/describe/narration")
async def api_short_describe_narration(body: DescribeNarrationIn):
    """物語の文章(と、あれば曲の内容)から、ナレーションの案(日本語 3〜8 文)を管理役の LLM が書く。入力欄を埋める用で、制作は作らない。{text}。
    生成中は LLM が CPU 専用になり、遅くなることがある。"""
    concept = ""
    with _mv_errors():
        if body.song_id.strip():
            concept = concept_from_song(pick_song(app.state.ledger, body.song_id)["params"])
    try:
        return {"text": await propose_narration(app.state.llm, body.story, concept)}
    except LlmError as e:
        raise HTTPException(400 if not body.story.strip() else 503, e.message)


@app.get("/api/short")
def api_short_list(limit: int = 100):
    """制作の一覧(新しい順。シーンの中身は含めない)。ショート動画だけ(MV は /api/mv)。"""
    return [_mv_public(v) for v in app.state.short.list(max(1, min(limit, 500)))]


@app.post("/api/short")
async def api_short_create(body: ShortIn):
    with _mv_errors():
        return _mv_public(app.state.short.create(body.model_dump()))


@app.get("/api/short/{pid}")
def api_short_get(pid: str):
    view = app.state.short.view(pid)
    if view is None:
        raise HTTPException(404, "そのショート動画の制作は見つかりません(削除されたかもしれません)")
    return _mv_public(view)


@app.post("/api/short/{pid}/pause")
async def api_short_pause(pid: str):
    with _mv_errors():
        return _mv_public(await app.state.short.pause(pid))


@app.post("/api/short/{pid}/resume")
async def api_short_resume(pid: str):
    with _mv_errors():
        return _mv_public(app.state.short.resume(pid))


@app.post("/api/short/{pid}/cancel")
async def api_short_cancel(pid: str):
    with _mv_errors():
        return _mv_public(await app.state.short.cancel(pid))


@app.post("/api/short/{pid}/reassemble")
async def api_short_reassemble(pid: str, body: Optional[ShortReassembleIn] = None):
    """結合し直す。body の final_audio(auto / none)・subtitles があれば、制作の設定を書き換えてから。前の動画はギャラリーに残る。"""
    with _mv_errors():
        return _mv_public(app.state.short.reassemble(pid, body.final_audio if body else None, body.subtitles if body else None))


@app.post("/api/short/{pid}/scenes/{index}/retry")
async def api_short_retry_scene(pid: str, index: int, body: Optional[MvRetryIn] = None):
    body = body or MvRetryIn()
    with _mv_errors():
        return _mv_public(app.state.short.retry_scene(pid, index, body.prompt, body.seed))


@app.delete("/api/short/{pid}")
async def api_short_delete(pid: str, with_output: bool = False):
    """制作を消す(作業フォルダごと。走っていれば、ジョブを中止して)。ギャラリーの最終の動画は残る。with_output=1 なら、その制作の動画も消す(MV と同じ)。"""
    with _mv_errors():
        ids = app.state.short.output_ids(pid) if with_output else []
        await app.state.short.delete(pid)
    return {"ok": True, **(await asyncio.to_thread(_delete_outputs, ids) if with_output else {})}


# ---- 編集(ffmpeg だけの簡易編集。編集プロジェクトの state と履歴は台帳、プレビュー・書き出しは CPU のジョブ) ----

class EditCreateIn(BaseModel):
    name: str = ""
    state: Optional[dict] = None   # 渡せば最初の状態(検証する)。無ければ空


class EditStateIn(BaseModel):
    state: dict


class EditRenameIn(BaseModel):
    name: str = ""


class EditProbeIn(BaseModel):
    id: str = ""


@contextlib.contextmanager
def _edit_errors():
    """編集の操作の失敗を HTTP のエラーにする(見つからない → 404、書き出し中 → 409、入力や状態が合わない → 400。理由はどれも日本語)。"""
    try:
        yield
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ExportBusy as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


def _edit_public(view: Optional[dict]) -> Optional[dict]:
    """編集の辞書(view・一覧の行)の、書き出した動画(台帳の行)を、ギャラリーと同じ形(url つき)にする。"""
    return {**view, "output": _public(view["output"]) if view.get("output") else None} if view else view


@app.get("/api/edit")
def api_edit_list(limit: int = 200):
    """編集プロジェクトの一覧(更新が新しい順。state の中身は含めない)。"""
    return [_edit_public(v) for v in app.state.edit.list(max(1, min(limit, 500)))]


@app.post("/api/edit")
async def api_edit_create(body: EditCreateIn):
    with _edit_errors():
        return _edit_public(app.state.edit.create(body.name, body.state))


@app.post("/api/edit/probe")
def api_edit_probe(body: EditProbeIn):
    """生成物の種類・長さ・寸法(画面が、ドロップした素材の長さを知る用)。"""
    with _edit_errors():
        return app.state.edit.probe(body.id)


@app.get("/api/edit/{pid}")
def api_edit_get(pid: str):
    view = app.state.edit.view(pid)
    if view is None:
        raise HTTPException(404, "その編集プロジェクトは見つかりません(削除されたかもしれません)")
    return _edit_public(view)


@app.put("/api/edit/{pid}/state")
async def api_edit_state(pid: str, body: EditStateIn):
    """state 全体を置き換える(検証して、履歴に 1 段積む)。プレビューは自動では作らない(続けて /preview を呼ぶ)。"""
    with _edit_errors():
        return _edit_public(app.state.edit.set_state(pid, body.state))


@app.post("/api/edit/{pid}/undo")
async def api_edit_undo(pid: str):
    with _edit_errors():
        return _edit_public(app.state.edit.undo(pid))


@app.post("/api/edit/{pid}/redo")
async def api_edit_redo(pid: str):
    with _edit_errors():
        return _edit_public(app.state.edit.redo(pid))


@app.post("/api/edit/{pid}/preview")
async def api_edit_preview(pid: str):
    """いまの状態のプレビューを作るジョブを入れる(走っている・順番待ちのプレビューは中止して)。"""
    with _edit_errors():
        return _edit_public(await app.state.edit.preview(pid))


class EditFrameIn(BaseModel):
    t: float = 0.0                 # プレビューの再生位置(書き出しの秒)


@app.post("/api/edit/{pid}/frame")
async def api_edit_frame(pid: str, body: EditFrameIn):
    """再生位置のコマを、元の動画から元の解像度の PNG に抜き出してギャラリーに載せる(同期)。ギャラリーの公開形を返す。"""
    with _edit_errors():
        return _public(await asyncio.to_thread(app.state.edit.extract_frame, pid, body.t))


@app.post("/api/edit/{pid}/export")
async def api_edit_export(pid: str):
    """書き出しのジョブを入れる。書き出しが走っていれば 409。"""
    with _edit_errors():
        return _edit_public(app.state.edit.export(pid))


@app.patch("/api/edit/{pid}")
async def api_edit_rename(pid: str, body: EditRenameIn):
    with _edit_errors():
        return _edit_public(app.state.edit.rename(pid, body.name))


@app.delete("/api/edit/{pid}")
async def api_edit_delete(pid: str):
    """プロジェクトとプレビューを消す(走っているジョブは中止して)。書き出した動画はギャラリーに残る。"""
    with _edit_errors():
        await app.state.edit.delete(pid)
    return {"ok": True}


# ---- プリセット(各タブの入力欄の値に名前を付けたもの) ----

class PresetIn(BaseModel):
    tab: Optional[str] = None      # image / video / music / mv / short
    name: Optional[str] = None
    values: Any = None             # 入力欄の値の辞書(文字・数・真偽値・それらの配列)。形の検査は presets.check_values(日本語の 400)


class PresetPatch(BaseModel):
    name: Optional[str] = None
    values: Any = None


@contextlib.contextmanager
def _preset_errors():
    """プリセットの操作の失敗を HTTP のエラーにする(理由は日本語)。"""
    try:
        yield
    except PresetError as e:
        raise HTTPException(e.status, e.message)


@app.get("/api/presets")
def api_presets(tab: str = None):
    """同梱のプリセット(builtin: true)、自分のプリセットの順。tab で絞る。"""
    with _preset_errors():
        return presets.list_presets(app.state.ledger, tab)


@app.post("/api/presets")
def api_presets_create(body: PresetIn):
    """自分のプリセットを作る。同じ名前があれば「名前(2)」…にして、作ったものを返す。"""
    with _preset_errors():
        return presets.create_preset(app.state.ledger, body.tab, body.name, body.values)


@app.post("/api/presets/import")
async def api_presets_import(request: Request):
    """書き出した JSON(プリセットの配列か {"presets": [...]})から、自分のプリセットを作って返す。1 件でも不正なら何も作らない。"""
    try:
        data = await request.json()
    except ValueError:
        raise HTTPException(400, "JSON として読めません")
    with _preset_errors():
        return await asyncio.to_thread(presets.import_presets, app.state.ledger, data)


@app.get("/api/presets/export")
def api_presets_export(tab: str = None, include_builtin: bool = False):
    """自分のプリセット(include_builtin=1 なら同梱のものも)を JSON のファイルで返す。そのまま import できる。"""
    with _preset_errors():
        data = presets.export_presets(app.state.ledger, tab, include_builtin)
    filename = f"h3-presets-{tab}.json" if tab else "h3-presets.json"       # tab は検査済み(image / video / music / mv / short)
    return Response(json.dumps(data, ensure_ascii=False, indent=2), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.put("/api/presets/{pid}")
def api_presets_update(pid: str, body: PresetPatch):
    """名前と値のうち、渡したほうだけを書き換える。同梱のプリセットは 400。"""
    with _preset_errors():
        return presets.update_preset(app.state.ledger, pid, body.name, body.values)


@app.delete("/api/presets/{pid}")
def api_presets_delete(pid: str):
    with _preset_errors():
        presets.delete_preset(app.state.ledger, pid)
    return {"ok": True}


# ---- ギャラリーとアップロード ----

def _public(rec: dict) -> dict:
    out = {k: v for k, v in rec.items() if k not in ("path", "thumb_path")}
    out["url"] = "/outputs/" + rec["path"]
    out["thumb_url"] = "/outputs/" + rec["thumb_path"] if rec["thumb_path"] else None
    return out


@app.get("/api/gallery")
def api_gallery(kind: str = "all", tab: str = None, limit: int = 200):
    if kind not in ("all", "image", "video", "audio"):
        raise HTTPException(400, "kind は all / image / video / audio のいずれかです")
    recs = app.state.ledger.list_outputs(None if kind == "all" else kind, tab, limit=max(1, min(limit, 2000)))
    return [_public(r) for r in recs]


JOB_IN_USE = "実行中・順番待ちのジョブで使っています"
EDIT_IN_USE = "編集プロジェクトで使っています。先にクリップ・曲から外すか、プロジェクトを削除してください"
DELETE_MAX = 500          # 一度に消せる数(画面のギャラリーが読む数)


def _strings(value):
    """params の中の文字列の値(入れ子のリスト・辞書の中も)。"""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)


def _project_in_use(proj: dict) -> str:
    """MV・ショート動画の制作が使っている生成物を消せない理由(どの制作かを、題か ID で示す)。"""
    noun = "ショート動画の" if proj.get("kind") == "short" else "MV の"
    title = str(proj["params"].get("title") or "").strip()
    which = f"「{title}」" if title else f"(ID {proj['id']})"
    return f"{noun}制作{which}で使っています。先に制作を削除してください"


def _in_use(ids) -> dict:
    """ids のうち、まだ要る生成物の ID -> 消せない理由。順番待ち・準備中・実行中のジョブが params で使っているもの(references・first_image・
    last_image など。入れ子の中も見る)と、MV・ショート動画の制作(状態を問わない。完成した制作もシーンのやり直し・結合し直し、中止した制作も
    再開ができるので)が曲・画像に使っているもの、編集プロジェクトの state が使っているクリップ・曲。"""
    ids, busy = set(ids), {}
    for job in list(app.state.jobs.jobs.values()):
        if job.status not in TERMINAL:
            for s in _strings(job.params):
                if s in ids:
                    busy.setdefault(s, JOB_IN_USE)
    for proj in app.state.ledger.list_mv_projects(limit=100000):
        params = proj["params"]
        for oid in (params.get("song_id"), params.get("image_id"), *(it.get("id") for it in params.get("images") or [])):       # ショート動画は参照画像(images)
            if oid in ids:
                busy.setdefault(oid, _project_in_use(proj))
    for proj in app.state.ledger.list_edit_projects(limit=100000):       # 編集プロジェクトは、いまの state のクリップと曲を使い続ける
        for oid in editclip.used_ids(proj["state"]):
            if oid in ids:
                busy.setdefault(oid, EDIT_IN_USE)
    return busy


def _delete_outputs(ids: list) -> dict:
    """生成物を消す(ファイルとサムネイルも)。同じ ID が重なっていても 1 回だけ。
    {"deleted": 消した ID, "missing": もう無かった ID, "refused": [{"id", "reason"}](まだ要るので消さなかったもの)}"""
    ledger, ids = app.state.ledger, list(dict.fromkeys(ids))
    found = {oid for oid in ids if ledger.get_output(oid) is not None}
    busy = _in_use(found)
    out = {"deleted": [], "missing": [], "refused": []}
    for oid in ids:
        if oid in busy:
            out["refused"].append({"id": oid, "reason": busy[oid]})
        elif oid in found and ledger.delete_output(oid):
            out["deleted"].append(oid)
        else:
            out["missing"].append(oid)       # 調べてから消すまでの間に、別の操作で消えたものもここ
    return out


class GalleryDeleteIn(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=DELETE_MAX)


@app.post("/api/gallery/delete")
def api_gallery_delete_many(body: GalleryDeleteIn):
    """生成物をまとめて消す。まだ要るもの(使っているジョブ・MV やショート動画の制作・編集プロジェクトがあるもの)は消さずに、理由を返す。"""
    return _delete_outputs(body.ids)


@app.delete("/api/gallery/{output_id}")
def api_gallery_delete(output_id: str):
    res = _delete_outputs([output_id])
    if res["refused"]:
        raise HTTPException(409, res["refused"][0]["reason"])
    if res["missing"]:
        raise HTTPException(404, "その生成物は見つかりません")
    return {"ok": True}


def _save_upload(f: UploadFile) -> dict:
    mime = f.content_type if (f.content_type or "").split("/")[0] in UPLOAD_KINDS else None
    mime = mime or mimetypes.guess_type(f.filename or "")[0] or ""
    kind = UPLOAD_KINDS.get(mime.split("/")[0])
    if kind is None:
        raise HTTPException(400, f"画像・動画・音声のファイルだけ取り込めます: {f.filename}")
    ext = re.sub(r"[^a-z0-9]", "", (f.filename or "").rsplit(".", 1)[-1].lower())[:5] if "." in (f.filename or "") else ""
    ext = ext or (mimetypes.guess_extension(mime) or ".bin").lstrip(".")
    dest = OUTPUTS_DIR / "uploads" / f"{uuid.uuid4().hex}.{ext}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as out:
        shutil.copyfileobj(f.file, out)
    try:
        return app.state.ledger.add_output(kind, dest, "upload", filename=f.filename, params={"uploaded": True})
    except Exception as e:  # noqa: BLE001  壊れた画像など
        dest.unlink(missing_ok=True)
        raise HTTPException(400, f"{f.filename} を読み込めません: {type(e).__name__}")


@app.post("/api/uploads")
async def api_uploads(files: list[UploadFile]):
    """OS からドロップされたファイルを outputs/uploads/ に保存し、ギャラリーに登録する。"""
    return [_public(await asyncio.to_thread(_save_upload, f)) for f in files]


@app.middleware("http")
async def _no_stale_web(request, call_next):
    """画面のファイル(HTML・JS・CSS)はブラウザに古い版を使わせない(更新後に、古い gallery.js が残ってギャラリーの挙動が変わらなかった。
    2026-11-25)。no-cache = 毎回サーバに確かめる(変わっていなければ 304 で速い)。生成物(/outputs/)と API はそのまま。"""
    response = await call_next(request)
    path = request.url.path
    if request.method == "GET" and not path.startswith(("/api/", "/outputs/")) and (path == "/" or path.endswith((".html", ".js", ".css"))):
        response.headers["Cache-Control"] = "no-cache"
    return response


app.mount("/outputs", StaticFiles(directory=str(OUTPUTS_DIR)), name="outputs")
app.mount("/", StaticFiles(directory=str(REPO_ROOT / "web"), html=True), name="web")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="YUZU Creator Studio 管理役")
    ap.add_argument("--host", default=settings.host)
    ap.add_argument("--port", type=int, default=settings.port)
    args = ap.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
