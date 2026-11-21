"""H3-Image(画像)のハンドラ: 参照画像のアップロード → ジョブ投入 → 進捗の取得 → 画像の取り込み。"""
import asyncio
import logging
import mimetypes
import time
from pathlib import Path

import httpx

from ..jobs import Ctx, Handler, Job, JobCancelled, JobFailed
from .common import download as _download, error_text as _error_text

log = logging.getLogger("studio.h3image")

COMPONENT = "h3image"
POLL_S = 1.0
TOTAL_TIMEOUT_S = 30 * 60
MAX_POLL_ERRORS = 30           # 部品へ連続して届かなかった回数(1 秒おき)がこれを超えたら失敗にする
MAX_REFS = 16
# 部品自体は 8192 まで受けるが、このアプリでは 4096 までにする(6〜12GB の GPU では
# それ以上は実用にならず、VRAM あふれで極端に遅くなるため)。
MIN_SIZE, MAX_SIZE, SNAP = 256, 4096, 32
STEPS_CHOICES = (2, 3, 6)


def _snap(value, name: str) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} は整数で指定してください")
    return min(MAX_SIZE, max(MIN_SIZE, round(v / SNAP) * SNAP))


def normalize(kind: str, params: dict, ledger=None) -> dict:
    """画面から来た params を検証して整える。不正なら ValueError(日本語)。"""
    mode = kind.split(".", 1)[1]
    prompt = str(params.get("prompt") or "").strip()
    if not 1 <= len(prompt) <= 8000:
        raise ValueError("プロンプトは 1〜8000 文字で入力してください")
    refs = [str(r) for r in (params.get("references") or [])]
    if mode == "t2i" and refs:
        raise ValueError("t2i では参照画像を使えません(edit を使ってください)")
    if mode == "edit" and not 1 <= len(refs) <= MAX_REFS:
        raise ValueError(f"参照画像は 1〜{MAX_REFS} 枚にしてください")
    seed = params.get("seed")
    if seed in (None, ""):
        seed = None
    else:
        try:
            seed = int(seed)
        except (TypeError, ValueError):
            raise ValueError("seed は整数で指定してください")
    steps = params.get("steps")
    steps = None if steps in (None, "") else int(steps)
    if steps is not None and steps not in STEPS_CHOICES:
        raise ValueError(f"steps は {STEPS_CHOICES} のいずれかか、未指定にしてください")
    if ledger is not None:
        for r in refs:
            rec = ledger.get_output(r)
            if rec is None or rec["kind"] != "image":
                raise ValueError(f"参照画像が見つかりません: {r}")
    return {"prompt": prompt, "width": _snap(params.get("width", 1024), "width"),
            "height": _snap(params.get("height", 1024), "height"), "seed": seed, "steps": steps, "references": refs}


def _client(ctx: Ctx, timeout: float = 60.0) -> httpx.AsyncClient:
    spec = ctx.sched.comps[COMPONENT].spec
    return httpx.AsyncClient(base_url=spec.base_url, timeout=timeout, transport=ctx.transport)


async def _upload(c: httpx.AsyncClient, path: Path, filename: str) -> str:
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    r = await c.post("/api/assets", files={"file": (filename, await asyncio.to_thread(path.read_bytes), mime)})
    if r.status_code != 201:
        raise JobFailed(f"参照画像のアップロードに失敗しました: {_error_text(r)}")
    return r.json()["id"]


async def run(job: Job, ctx: Ctx) -> None:
    p = job.params
    mode = job.kind.split(".", 1)[1]
    assets: list = []
    async with _client(ctx, timeout=60.0) as c:
        try:
            conditions = []
            for i, oid in enumerate(p["references"], 1):
                rec = ctx.ledger.get_output(oid)
                if rec is None:
                    raise JobFailed(f"参照画像が見つかりません(削除されたかもしれません): {oid}")
                job.message = f"参照画像 {i}/{len(p['references'])} を送っています"
                aid = await _upload(c, ctx.ledger.abs_path(rec["path"]), rec["filename"] or Path(rec["path"]).name)
                assets.append(aid)
                conditions.append({"asset_id": aid})
            if job.cancel_requested:
                raise JobCancelled()

            body = {"mode": mode, "prompt": p["prompt"], "width": p["width"], "height": p["height"]}
            for key in ("seed", "steps"):
                if p.get(key) is not None:
                    body[key] = p[key]
            if conditions:
                body["conditions"] = conditions
            r = await c.post("/api/jobs", json=body)
            if r.status_code != 202:
                raise JobFailed(f"H3-Image がジョブを受け付けませんでした: {_error_text(r)}")
            job.remote_id = r.json()["id"]
            info = await _poll(job, c)

            ext = Path(info["image_url"]).suffix.lower()
            dest = ctx.outputs_dir / "image" / f"{job.id}{ext if ext in ('.png', '.jpg', '.jpeg', '.webp') else '.png'}"
            job.message = "画像を取り込んでいます"
            await _download(c, info["image_url"], dest)
            job.result = {k: info.get(k) for k in ("generation_seconds", "peak_vram_gb", "steps_used", "profile")}
            rec = await asyncio.to_thread(ctx.ledger.add_output, "image", dest, job.tab, job.id, dest.name,
                                          {**p, "kind": job.kind, "result": job.result})
            job.outputs.append(rec["id"])
        finally:
            for aid in assets:   # アップロードした参照は(失敗・中止でも)消す
                try:
                    await c.delete(f"/api/assets/{aid}")
                except httpx.HTTPError:
                    pass


async def _poll(job: Job, c: httpx.AsyncClient) -> dict:
    """部品のジョブが終わるまで 1 秒おきに見る。完了なら JobResponse を返す。"""
    t0, errors, interrupt_sent = time.monotonic(), 0, False
    while True:
        if job.cancel_requested and not interrupt_sent:
            interrupt_sent = await _send_interrupt(job, c)
        try:
            r = await c.get(f"/api/jobs/{job.remote_id}", timeout=15.0)
            r.raise_for_status()
            info, errors = r.json(), 0
        except (httpx.HTTPError, ValueError) as e:
            errors += 1
            if errors > MAX_POLL_ERRORS:
                raise JobFailed(f"H3-Image と通信できません: {e}")
            await asyncio.sleep(POLL_S)
            continue
        status = info.get("status")
        if status == "completed" and info.get("image_url"):
            return info
        if status == "failed":
            raise JobFailed(info.get("error") or "H3-Image が失敗を報告しました")
        if status == "interrupted":
            raise JobCancelled()
        if status == "running":
            job.progress = max(0.0, min(0.99, float(info.get("progress") or 0)))
            job.message = f"生成中 {round(job.progress * 100)}%"
        else:
            job.message = "H3-Image の順番を待っています"
        if time.monotonic() - t0 > TOTAL_TIMEOUT_S:
            raise JobFailed("生成が 30 分以内に終わりませんでした")
        await asyncio.sleep(POLL_S)


async def _send_interrupt(job: Job, c: httpx.AsyncClient) -> bool:
    if not job.remote_id:
        return False   # まだ部品に投げていない(run が cancel_requested を見て止まる)か、次の周で送る
    try:
        r = await c.post("/api/interrupt", json={"job_id": job.remote_id}, timeout=15.0)
        return r.status_code < 300
    except httpx.HTTPError:
        return False


async def cancel(job: Job, ctx: Ctx) -> None:
    """実行中のジョブの中止要求を部品へ送る(状態が interrupted になったら run が JobCancelled を投げる)。"""
    if not job.remote_id:
        return
    async with _client(ctx, timeout=15.0) as c:
        await _send_interrupt(job, c)


HANDLER = Handler(component=COMPONENT, normalize=normalize, run=run, cancel=cancel)
HANDLERS = {"h3image.t2i": HANDLER, "h3image.edit": HANDLER}
