"""MiniMax-H3(動画・静止画)のハンドラ: T2VA / FL2VA / Ref2VA / T2I / Ref2I。

エンジンの生成 API は、動画ができあがるまで返らない(数分〜最長 20 分ほど)同期の POST。
そこで POST を 1 本のタスクで待ち、もう 1 本のタスクが /api/progress を 1 秒おきに読んで進捗を写す。
"""
import asyncio
import logging
import mimetypes
import re
from pathlib import Path

import httpx

from .. import h3cache
from ..jobs import Ctx, Handler, Job, JobCancelled, JobFailed
from .common import download, error_text

log = logging.getLogger("studio.h3")

COMPONENT = "h3"
POLL_S = 1.0
POST_TIMEOUT_S = 3600.0        # 6GB のカードで最長 20 分ほどかかる。余裕を見て 1 時間
MAX_POLL_ERRORS = 60
MIN_SECONDS, MAX_SECONDS = 5, 15    # エンジンの既定の下限は 5 秒(H3_MIN_SECONDS)。それ未満はエンジンが 400 で断る
MAX_IMAGES, MAX_VIDEOS, MAX_AUDIOS = 9, 3, 3
MIN_SIZE, MAX_SIZE, SNAP = 256, 2048, 32
DEFAULT_RESOLUTION = "768x768"

ENDPOINTS = {"t2va": "/api/t2va", "fl2va": "/api/fl2va", "ref2va": "/api/ref2va", "t2i": "/api/t2i", "ref2i": "/api/ref2va"}
STILL_MODES = ("t2i", "ref2i")
REF_MODES = ("ref2va", "ref2i")

# エンジンの /api/progress の phase -> 画面に出す文言
PHASE_LABEL = {
    "starting": "生成を始めています", "loading_text_encoder": "テキストエンコーダを読み込んでいます",
    "encoding": "プロンプトと参照をエンコードしています", "loading_transformer": "モデルを読み込んでいます",
    "denoising": "生成中", "decoding": "動画に書き出しています", "done": "仕上げています",
}


def _int(value, name: str, allow_none: bool = False):
    if value in (None, ""):
        if allow_none:
            return None
        raise ValueError(f"{name} を指定してください")
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} は整数で指定してください")


def _size(value, name: str) -> int:
    return min(MAX_SIZE, max(MIN_SIZE, round(_int(value, name) / SNAP) * SNAP))


def _parse_resolution(text) -> tuple[int, int]:
    m = re.fullmatch(r"(\d{3,4})x(\d{3,4})", str(text or DEFAULT_RESOLUTION))
    if not m:
        raise ValueError("解像度は 768x768 のような形で指定してください")
    return _size(m[1], "幅"), _size(m[2], "高さ")


def _check_image(ledger, oid, label: str) -> str:
    if ledger is not None:
        rec = ledger.get_output(str(oid))
        if rec is None or rec["kind"] != "image":
            raise ValueError(f"{label}の画像が見つかりません(削除されたかもしれません)")
    return str(oid)


def normalize(kind: str, params: dict, ledger=None) -> dict:
    """画面から来た params を検証して整える。不正なら ValueError(日本語)。"""
    mode = kind.split(".", 1)[1]
    prompt = str(params.get("prompt") or "").strip()
    if not 1 <= len(prompt) <= 8000:
        raise ValueError("プロンプトは 1〜8000 文字で入力してください")
    out = {"prompt": prompt, "seed": _int(params.get("seed"), "seed", allow_none=True)}

    if mode in REF_MODES:
        if params.get("width") not in (None, "") or params.get("height") not in (None, ""):
            out["width"], out["height"] = _size(params.get("width"), "幅"), _size(params.get("height"), "高さ")
        elif params.get("resolution"):
            out["width"], out["height"] = _parse_resolution(params["resolution"])
        else:
            out["width"] = out["height"] = None    # 未指定ならエンジンが参照から決める
    else:
        # エンジンの resolution は 768x768 / 768x1344 / 1344x768 の名前だけを受けるので、
        # 512x512 などは width / height で渡す(明示指定はプリセットより優先され、32 の倍数に丸められる)
        out["width"], out["height"] = _parse_resolution(params.get("resolution"))
        out["resolution"] = "%dx%d" % (out["width"], out["height"])

    if mode not in STILL_MODES:
        try:
            seconds = float(params.get("seconds", MIN_SECONDS))
        except (TypeError, ValueError):
            raise ValueError("秒数は数字で指定してください")
        if mode == "ref2va" and seconds == 0:
            pass    # 0 = 音声の長さに合わせる(下で音声の参照を確かめる)
        elif not MIN_SECONDS <= seconds <= MAX_SECONDS:
            raise ValueError(f"秒数は {MIN_SECONDS}〜{MAX_SECONDS} で指定してください" + ("(0 なら音声の長さに合わせます)" if mode == "ref2va" else ""))
        out["seconds"] = seconds

    if mode == "fl2va":
        first, last = params.get("first_image") or None, params.get("last_image") or None
        if not (first or last):
            raise ValueError("先頭フレームか最後のフレームのどちらかは画像を入れてください")
        out["first_image"] = _check_image(ledger, first, "先頭フレーム") if first else None
        out["last_image"] = _check_image(ledger, last, "最後のフレーム") if last else None

    if mode in REF_MODES:
        refs = [str(r) for r in (params.get("references") or [])]
        if not refs:
            raise ValueError("参照を 1 つ以上入れてください(画像・動画・音声)")
        counts = {"image": 0, "video": 0, "audio": 0}
        if ledger is not None:
            for r in refs:
                rec = ledger.get_output(r)
                if rec is None:
                    raise ValueError(f"参照が見つかりません(削除されたかもしれません): {r}")
                counts[rec["kind"]] += 1
        for n, limit, label in ((counts["image"], MAX_IMAGES, "画像"), (counts["video"], MAX_VIDEOS, "動画"), (counts["audio"], MAX_AUDIOS, "音声")):
            if n > limit:
                raise ValueError(f"参照の{label}は最大 {limit} 個です({n} 個入っています)")
        if mode == "ref2va" and out["seconds"] == 0 and ledger is not None and counts["audio"] != 1:
            raise ValueError("音声の長さに合わせられるのは、音声の参照がちょうど 1 つのときだけです")
        out["references"] = refs
        # Vocal Lock は音声の参照があるときだけ意味がある(ref2va のみ)
        out["vocal_lock"] = mode == "ref2va" and bool(params.get("vocal_lock")) and (ledger is None or counts["audio"] > 0)
        # この PC で参照モードに要るキャッシュ(transformer_ref_int8)が無ければ受けない(エンジンが断る前に、作り方を出す)
        h3cache.check_ref_cache()
    return out


# ---- エンジンへの送信 ----

def _client(ctx: Ctx, timeout: float = 15.0) -> httpx.AsyncClient:
    spec = ctx.sched.comps[COMPONENT].spec
    return httpx.AsyncClient(base_url=spec.base_url, timeout=timeout, transport=ctx.transport)


def _file_part(field: str, path: Path, filename: str = None) -> tuple:
    """ファイル 1 つを multipart のエンジンにする(MV が、台帳に載せない作業用の音声を渡すのにも使う)。"""
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return field, (filename or path.name, path.read_bytes(), mime)


def _load_file(ctx: Ctx, field: str, oid: str, label: str) -> tuple:
    """台帳の生成物を multipart のファイル 1 つにする。"""
    rec = ctx.ledger.get_output(oid)
    if rec is None:
        raise JobFailed(f"{label}が見つかりません(削除されたかもしれません): {oid}")
    path = ctx.ledger.abs_path(rec["path"])
    return _file_part(field, path, rec.get("filename"))


def _build_request(job: Job, ctx: Ctx) -> tuple[str, dict, list]:
    """(エンドポイント, フォームの値, ファイルの並び)を作る。ファイルを読むので別スレッドで呼ぶ。"""
    p, mode = job.params, job.kind.split(".", 1)[1]
    data: dict = {"prompt": p["prompt"]}
    files: list = []
    if p.get("seed") is not None:
        data["seed"] = str(p["seed"])
    if p.get("width") and mode not in REF_MODES:
        data["width"], data["height"] = str(p["width"]), str(p["height"])
    if mode in ("t2va", "fl2va", "ref2va") and p["seconds"] > 0:
        data["seconds"] = str(p["seconds"])        # ref2va で秒数を送らない = 音声の長さに合わせる(エンジンの仕様)
    if mode == "fl2va":
        if p.get("first_image"):
            files.append(_load_file(ctx, "image", p["first_image"], "先頭フレーム"))
        if p.get("last_image"):
            files.append(_load_file(ctx, "last_image", p["last_image"], "最後のフレーム"))
    if mode in REF_MODES:
        if p.get("width"):
            data["width"], data["height"] = str(p["width"]), str(p["height"])
        for oid in p["references"]:
            files.append(_load_file(ctx, "references", oid, "参照"))
        if mode == "ref2i":
            data["still"] = "1"
        if p.get("vocal_lock"):
            data["vocal_lock"] = "true"
    return ENDPOINTS[mode], data, files


async def _progress_job_id(c: httpx.AsyncClient):
    """いまエンジンが持っている(前回の)ジョブ ID。今回のジョブの進捗と取り違えないための目印。"""
    try:
        return (await c.get("/api/progress")).json().get("job_id")
    except (httpx.HTTPError, ValueError, AttributeError):
        return None


async def _watch_progress(job: Job, c: httpx.AsyncClient, stale_id, prefix: str = "") -> None:
    """生成の POST を待つ間、/api/progress を読んでジョブの進捗・文言・エンジン側 ID を更新し続ける(キャンセルされるまで)。
    prefix は文言の頭に付ける(MV が「シーン 3 / 12: 」を付ける)。"""
    errors, interrupt_sent = 0, False
    while True:
        await asyncio.sleep(POLL_S)
        if job.cancel_requested and job.remote_id and not interrupt_sent:
            interrupt_sent = await _send_interrupt(job, c)
        try:
            r = await c.get("/api/progress")
            r.raise_for_status()
            info, errors = r.json(), 0
        except (httpx.HTTPError, ValueError):
            errors += 1
            if errors == MAX_POLL_ERRORS:
                log.warning("H3 の進捗を読めません(%d 回連続)", errors)
            continue
        phase, jid = info.get("phase"), info.get("job_id")
        if phase in (None, "idle") or not jid or jid == stale_id:
            continue
        job.remote_id = jid
        if phase == "error":
            continue
        label = PHASE_LABEL.get(phase) or info.get("message") or str(phase)
        step, total = info.get("step") or 0, info.get("total_steps") or 0
        if phase == "denoising" and total:
            job.progress = max(job.progress, min(0.99, step / total))
            label = f"{label} {step}/{total}"
        elif phase == "decoding":
            job.progress = 0.99
        # それ以外の段(読み込み・エンコード、書き出しの後のテキストエンコーダの読み直し)では進み具合を戻さない
        if not job.cancel_requested:
            job.message = prefix + label


async def _send_interrupt(job: Job, c: httpx.AsyncClient) -> bool:
    if not job.remote_id:
        return False
    try:
        r = await c.post("/api/interrupt", json={"job_id": job.remote_id}, timeout=15.0)
        return r.status_code < 300
    except httpx.HTTPError:
        return False


def _is_interrupt(r: httpx.Response) -> bool:
    text = error_text(r)
    return r.status_code == 499 or "interrupt" in text.lower() or "中断" in text


async def _generate(job: Job, c: httpx.AsyncClient, endpoint: str, data: dict, files: list, prefix: str = "") -> dict:
    """生成の POST を待ち、結果の JSON を返す。その間の進捗は別タスクが読む(文言の頭には prefix を付ける)。"""
    stale_id = await _progress_job_id(c)
    watcher = asyncio.create_task(_watch_progress(job, c, stale_id, prefix))
    try:
        r = await c.post(endpoint, data=data, files=files or None, timeout=POST_TIMEOUT_S)
    except httpx.ReadTimeout:
        raise JobFailed("H3 の応答が 1 時間以内に返りませんでした")
    except httpx.HTTPError as e:
        raise JobFailed(f"H3 と通信できません: {type(e).__name__}: {e}")
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
    if r.status_code == 200:
        return r.json()
    if _is_interrupt(r):
        raise JobCancelled()
    if r.status_code == 409:
        raise JobFailed("H3 は別の生成を実行中です。終わってからもう一度試してください")
    detail = error_text(r)
    if h3cache.is_engine_refusal(detail):
        # 参照モード用のキャッシュが無いのでエンジンが断った(作る前からある制作の再開・やり直しでも、ここに来る)。
        # エンジンの英文の代わりに作り方を出し、原文は log_tail に残す
        log.warning("H3 が参照モードを断りました(参照モード用のキャッシュがありません): %s", detail)
        raise JobFailed(h3cache.missing_message(h3cache.command_for()), detail)
    raise JobFailed(f"H3 が生成に失敗しました: {detail}")


async def run(job: Job, ctx: Ctx) -> None:
    mode = job.kind.split(".", 1)[1]
    endpoint, data, files = await asyncio.to_thread(_build_request, job, ctx)
    if job.cancel_requested:
        raise JobCancelled()
    async with _client(ctx) as c:
        job.message = "H3 に送っています"
        res = await _generate(job, c, endpoint, data, files)
        job.result = {"generation_seconds": res.get("total_elapsed_s"), "peak_vram_gb": res.get("peak_vram_gb"),
                      "steps_used": res.get("num_inference_steps"), "seed": res.get("seed")}
        meta = {**job.params, "kind": job.kind, "result": job.result}
        job.message = "生成物を取り込んでいます"
        if mode in STILL_MODES:
            if not res.get("image_url"):
                raise JobFailed("H3 が静止画を返しませんでした")
            dest = ctx.outputs_dir / "image" / f"{job.id}.png"
            await download(c, res["image_url"], dest)
            rec = await asyncio.to_thread(ctx.ledger.add_output, "image", dest, job.tab, job.id, dest.name, meta)
        else:
            if not res.get("video_url"):
                raise JobFailed("H3 が動画を返しませんでした")
            dest = ctx.outputs_dir / "video" / f"{job.id}.mp4"
            await download(c, res["video_url"], dest)
            rec = await asyncio.to_thread(ctx.ledger.add_output, "video", dest, job.tab, job.id, dest.name, meta,
                                          duration_s=res.get("duration_s"))
        job.outputs.append(rec["id"])


async def cancel(job: Job, ctx: Ctx) -> None:
    """実行中の生成へ中止を要求する(反応はステップの区切りまで遅れる)。エンジン側 ID がまだ無ければ、見張りが分かった時点で送る。"""
    if not job.remote_id:
        return
    async with _client(ctx) as c:
        await _send_interrupt(job, c)


HANDLER = Handler(component=COMPONENT, normalize=normalize, run=run, cancel=cancel)
HANDLERS = {f"h3.{m}": HANDLER for m in ENDPOINTS}
