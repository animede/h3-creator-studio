"""H3-Image(画像)のハンドラ: 参照画像のアップロード → ジョブ投入 → 進捗の取得 → 画像の取り込み。"""
import asyncio
import logging
import mimetypes
import time
from pathlib import Path

import httpx

from .. import config, h3imgcache
from ..angles import apply_angle, check_angle
from ..styles import apply_style, check_style
from ..jobs import Ctx, Handler, Job, JobCancelled, JobFailed
from .common import download as _download, error_text as _error_text

log = logging.getLogger("studio.h3image")

COMPONENT = "h3image"
POLL_S = 1.0
TOTAL_TIMEOUT_S = 30 * 60
# 本体のキャッシュが無いステップ数の初回は、エンジンが bf16 の 62GB を取得して作る(1 時間以上)。そのときだけ待つ上限を延ばす
BUILD_TIMEOUT_S = 4 * 60 * 60
BUILDING_MESSAGE = "本体のキャッシュを作っています(初回だけ。62GB のダウンロードで 1 時間以上かかります)"
MAX_POLL_ERRORS = 30           # エンジンへ連続して届かなかった回数(1 秒おき)がこれを超えたら失敗にする
MAX_REFS = 16
# エンジン自体は 8192 まで受けるが、このアプリでは 4096 までにする(6〜12GB の GPU では
# それ以上は実用にならず、VRAM あふれで極端に遅くなるため)。
MIN_SIZE, MAX_SIZE, SNAP = 256, 4096, 32
STEPS_CHOICES = (2, 3, 6)


def _snap(value, name: str) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} は整数で指定してください")
    return min(MAX_SIZE, max(MIN_SIZE, round(v / SNAP) * SNAP))


def normalize(kind: str, params: dict, ledger=None) -> dict:
    """画面から来た params を検証して整える。不正なら ValueError(日本語)。"""
    mode = kind.split(".", 1)[1]
    prompt = str(params.get("prompt") or "").strip()
    angle = check_angle(params.get("angle")) if mode == "edit" else "none"      # アングルは edit だけ(t2i は常に none)
    # edit でアングルを選んでいれば、プロンプトは空でよい(アングルの文がプロンプトになる)
    if not (1 if angle == "none" else 0) <= len(prompt) <= 8000:
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
        except (TypeError, ValueError, OverflowError):
            raise ValueError("seed は整数で指定してください")
    steps = params.get("steps")
    if steps in (None, ""):
        steps = None
    else:
        try:
            steps = int(steps)
        except (TypeError, ValueError, OverflowError):      # 数でない値も、選べる数と同じ日本語の理由で断る
            steps = -1
    if steps is not None and steps not in STEPS_CHOICES:
        raise ValueError(f"steps は {STEPS_CHOICES} のいずれかか、未指定にしてください")
    if ledger is not None:
        for r in refs:
            rec = ledger.get_output(r)
            if rec is None or rec["kind"] != "image":
                raise ValueError(f"参照画像が見つかりません: {r}")
    return {"prompt": prompt, "width": _snap(params.get("width", 1024), "width"),
            "height": _snap(params.get("height", 1024), "height"), "seed": seed, "steps": steps, "references": refs,
            "style": check_style(params.get("style")), "angle": angle}


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

            # 画風・アングルは入力欄の文とは別の設定。送る直前に英語の文を足す(順は 入力欄の文 → アングル → 画風。
            # edit は参照画像があるので、画風は被写体を参照のまま保つ文)
            prompt = apply_angle(p["prompt"], p.get("angle")) if mode == "edit" else p["prompt"]
            body = {"mode": mode, "prompt": apply_style(prompt, p.get("style"), mode == "edit"), "width": p["width"], "height": p["height"]}
            for key in ("seed", "steps"):
                if p.get(key) is not None:
                    body[key] = p[key]
            if conditions:
                body["conditions"] = conditions
            r = await c.post("/api/jobs", json=body)
            if r.status_code != 202:
                error = _error_text(r)
                raise JobFailed(h3imgcache.friendly_error(error) or f"H3-Image がジョブを受け付けませんでした: {error}")
            job.remote_id = r.json()["id"]
            info = await _poll(job, c, building=not _cache_ready(p.get("steps")))

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


def _cache_ready(steps) -> bool:
    """このステップ数(None はエンジンの既定)の本体のキャッシュがあるか。確かめられなければ(フォルダが無い・外部のエンジン)ある扱い。"""
    try:
        d = config.settings.component_dir(COMPONENT)
        return not d.is_dir() or h3imgcache.steps_ready(d, steps, h3imgcache.engine_env())
    except Exception:  # noqa: BLE001
        return True


async def _poll(job: Job, c: httpx.AsyncClient, building: bool = False) -> dict:
    """エンジンのジョブが終わるまで 1 秒おきに見る。完了なら JobResponse を返す。building はキャッシュ作りから始まるか:
    エンジンはその間 progress を 0 のまま(ステップの進みは 0.05 から)にするので、0.05 未満のあいだは作っている旨を出し、待つ上限も延ばす。"""
    limit = BUILD_TIMEOUT_S if building else TOTAL_TIMEOUT_S
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
            error = info.get("error") or "H3-Image が失敗を報告しました"
            raise JobFailed(h3imgcache.friendly_error(error) or error)      # テキストエンコーダが無い: setup を最後まで、と案内する
        if status == "interrupted":
            raise JobCancelled()
        if status == "running":
            job.progress = max(0.0, min(0.99, float(info.get("progress") or 0)))
            job.message = BUILDING_MESSAGE if building and job.progress < 0.05 else f"生成中 {round(job.progress * 100)}%"
        else:
            job.message = "H3-Image の順番を待っています"
        if time.monotonic() - t0 > limit:
            raise JobFailed("生成が 30 分以内に終わりませんでした" if not building else "生成が 4 時間以内に終わりませんでした")
        await asyncio.sleep(POLL_S)


async def _send_interrupt(job: Job, c: httpx.AsyncClient) -> bool:
    if not job.remote_id:
        return False   # まだエンジンに投げていない(run が cancel_requested を見て止まる)か、次の周で送る
    try:
        r = await c.post("/api/interrupt", json={"job_id": job.remote_id}, timeout=15.0)
        return r.status_code < 300
    except httpx.HTTPError:
        return False


async def cancel(job: Job, ctx: Ctx) -> None:
    """実行中のジョブの中止要求をエンジンへ送る(状態が interrupted になったら run が JobCancelled を投げる)。"""
    if not job.remote_id:
        return
    async with _client(ctx, timeout=15.0) as c:
        await _send_interrupt(job, c)


HANDLER = Handler(component=COMPONENT, normalize=normalize, run=run, cancel=cancel)
HANDLERS = {"h3image.t2i": HANDLER, "h3image.edit": HANDLER}
