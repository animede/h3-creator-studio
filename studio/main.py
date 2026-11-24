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
from pydantic import BaseModel

from . import mv_audio, presets, procman
from .components import ComponentNotInstalled, all_specs
from .config import REPO_ROOT, settings
from .enhance import EnhanceError, enhance_prompt
from .gpu import GpuMonitor
from .handlers import HANDLERS
from .jobs import Ctx, JobRunner
from .ledger import Ledger
from .llm import Llm, LlmError
from .mv import Director
from .presets import PresetError
from .scheduler import Scheduler
from .handlers import ace as ace_handler
from .songplan import SongPlanError, make_caption, make_plan
from .tts import Tts, TtsError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


class _QuietPolling(logging.Filter):
    """画面が 1〜2 秒おきに取りに来る状態・進捗のアクセスログは出さない(端末が埋まって肝心の行が見えなくなる)。"""
    _QUIET = ("/api/status", "/api/jobs/", "/api/jobs?", "/api/operations/", "/api/gallery", "/api/llm/status",
              "/api/components/", "/api/mv", "/api/presets", "/outputs/", "/app.js", "/style.css", "/gallery.js", "/jobs.js",
              "/presets.js", "/image.js", "/video.js", "/music.js", "/mv.js", "/dnd.js", "/favicon.ico")

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
    app.state.tts = Tts(sched, ledger, OUTPUTS_DIR)
    app.state.llm = llm = Llm(settings)
    app.state.jobs = runner = JobRunner(Ctx(sched, ledger, OUTPUTS_DIR, llm=llm), HANDLERS)
    app.state.mv = director = Director(runner, ledger, OUTPUTS_DIR)
    director.restore()       # 再起動で途切れた MV の制作は、一時停止にして残す
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


app = FastAPI(title="h3-creator-studio", lifespan=lifespan)


def _comp(name: str):
    c = app.state.sched.comps.get(name)
    if c is None:
        raise HTTPException(404, f"未知の部品です: {name}")
    return c


@app.get("/api/status")
def api_status():
    sched, snap = app.state.sched, app.state.monitor.snapshot()
    return {"components": sched.status(), "gpu": snap["gpu"], "ram": snap["ram"], "shared_gpu_mib": snap["shared_gpu_mib"],
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


@app.post("/api/jobs", status_code=202)
def api_job_submit(body: JobIn):
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
    instrumental: bool = False


@app.post("/api/songplan")
async def api_songplan(body: SongPlanIn):
    """テーマ・歌詞・キャプションを LLM で作って返す(生成はしない。「歌詞だけ作る」にも使える)。生成中は LLM が CPU 専用になり、遅くなる。"""
    try:
        return await make_plan(app.state.llm.chat, body.genre, body.language, body.mood, body.vocal, body.theme.strip(),
                               want_lyrics=not body.instrumental)
    except (LlmError, SongPlanError) as e:
        raise HTTPException(503, str(e))


# ---- 音楽タブの「作る」ボタン(テーマ・歌詞・キャプション。管理役の LLM を使う。生成中でも作れる) ----

class MusicIn(BaseModel):
    genre: str = "J-POP"
    language: str = "Japanese"
    theme: str = ""
    lyrics: str = ""
    mood: str = ""
    vocal: str = ""
    thinking: bool = True
    instrumental: bool = False


@app.post("/api/music/theme")
async def api_music_theme(body: MusicIn):
    """ジャンルと言語から、曲のテーマを 1 つ作る。"""
    try:
        plan = await make_plan(app.state.llm.chat, body.genre, body.language, want_lyrics=False, want_caption=False)
    except (LlmError, SongPlanError) as e:
        raise HTTPException(503, str(e))
    return {"theme": plan["theme"]}


@app.post("/api/music/lyrics")
async def api_music_lyrics(body: MusicIn):
    """歌詞を作る。テーマがあればそのテーマで書き、無ければテーマも作る。"""
    try:
        plan = await make_plan(app.state.llm.chat, body.genre, body.language, body.mood, body.vocal, body.theme.strip(),
                               want_lyrics=True, want_caption=False)
    except (LlmError, SongPlanError) as e:
        raise HTTPException(503, str(e))
    return {"theme": plan["theme"], "lyrics": plan["lyrics"], "recommended_duration": plan["recommended_duration"]}


@app.post("/api/music/caption")
async def api_music_caption(body: MusicIn):
    """テーマ・歌詞・ムード・ボーカルから、ACE-Step に渡すキャプション(英語)を作る。"""
    try:
        caption = await make_caption(app.state.llm.chat, body.genre, body.theme.strip(), body.lyrics, body.mood, body.vocal,
                                     body.language, body.thinking, body.instrumental)
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


# ---- MV(リップシンク) ----

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
async def api_mv_delete(pid: str):
    """制作を消す(作業フォルダごと)。ギャラリーの最終の MV は残る。"""
    with _mv_errors():
        await app.state.mv.delete(pid)
    return {"ok": True}


# ---- プリセット(各タブの入力欄の値に名前を付けたもの) ----

class PresetIn(BaseModel):
    tab: Optional[str] = None      # image / video / music / mv
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
    filename = f"h3-presets-{tab}.json" if tab else "h3-presets.json"       # tab は検査済み(image / video / music / mv)
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


@app.delete("/api/gallery/{output_id}")
def api_gallery_delete(output_id: str):
    if not app.state.ledger.delete_output(output_id):
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


app.mount("/outputs", StaticFiles(directory=str(OUTPUTS_DIR)), name="outputs")
app.mount("/", StaticFiles(directory=str(REPO_ROOT / "web"), html=True), name="web")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="h3-creator-studio 管理役")
    ap.add_argument("--host", default=settings.host)
    ap.add_argument("--port", type=int, default=settings.port)
    args = ap.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
