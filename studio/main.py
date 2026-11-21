"""管理役の FastAPI アプリ。画面(web/)と JSON API を 1 オリジンで配る。"""
import argparse
import asyncio
import logging
import mimetypes
import re
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from typing import Literal, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import procman
from .components import ComponentNotInstalled, all_specs
from .config import REPO_ROOT, settings
from .enhance import EnhanceError, enhance_prompt
from .gpu import GpuMonitor
from .handlers import HANDLERS
from .jobs import Ctx, JobRunner
from .ledger import Ledger
from .llm import Llm, LlmError
from .scheduler import Scheduler
from .songplan import SongPlanError, make_plan
from .tts import Tts, TtsError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

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
    llm.busy = runner.gpu_busy        # GPU で生成している間、LLM は起こさずに待つ
    app.state.monitor = monitor = GpuMonitor(rss_provider=sched.rss_map, generating=runner.is_generating)
    sched.monitor = monitor
    monitor.start()
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
            "jobs_done_counter": app.state.jobs.done_counter}


@app.post("/api/components/{name}/{action}")
async def api_component_action(name: str, action: str):
    if action not in ("start", "stop", "ensure"):
        raise HTTPException(404, "start / stop / ensure のいずれかです")
    comp = _comp(name)
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
    """テーマ・歌詞・キャプションを LLM で作って返す(生成はしない。「歌詞だけ作る」にも使える)。"""
    if app.state.jobs.gpu_busy():
        raise HTTPException(409, "生成中は歌詞を作れません(GPU を使っているため)。終わってからもう一度押してください")
    try:
        return await make_plan(app.state.llm.chat, body.genre, body.language, body.mood, body.vocal, body.theme.strip(),
                               want_lyrics=not body.instrumental)
    except (LlmError, SongPlanError) as e:
        raise HTTPException(503, str(e))


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
