"""ACE-Step(音楽)の簡単モード: 管理役の LLM で曲のプランを作り → ACE の LM で BPM・調を決め → 生成 → mp3 を取り込む。

流れ(Easy Music の簡単モードと同じ):
  prepare(部品を載せる前): テーマ・歌詞・キャプションを LLM で作る(空のものだけ)
  run: /format_input で BPM・調・拍子 → /release_task(turbo 8 ステップ)→ /query_result を 2 秒おきに見る → mp3 を取り込む
"""
import asyncio
import json
import logging
import random
import time
import uuid
from urllib.parse import quote

import httpx

from .. import media, songplan
from ..jobs import Ctx, Handler, Job, JobCancelled, JobFailed
from ..llm import LlmError
from .common import download as _download, error_text as _error_text

log = logging.getLogger("studio.ace")

COMPONENT = "ace"
POLL_S = 2.0
TOTAL_TIMEOUT_S = 30 * 60
MAX_POLL_ERRORS = 30
FORMAT_TIMEOUT_S = 900      # 初回の /format_input は ACE の LM の読み込みを含む
SUBMIT_TIMEOUT_S = 120
MIN_SECONDS, MAX_SECONDS = 10, 300
INSTRUMENTAL_LYRICS = "[Instrumental]"
DEFAULT_NEGATIVE_PROMPT = "low quality, noisy, distorted, muddy, clipping, off-key, out of tune, amateur, poorly mixed"

# 言語の英語名(LLM へ渡す)と ACE の vocal_language。名前でもコードでも受ける
LANGUAGES = {"Japanese": "ja", "English": "en", "Korean": "ko", "Chinese": "zh", "Spanish": "es", "French": "fr",
             "German": "de", "Italian": "it", "Portuguese": "pt", "Russian": "ru"}
_LANG_LOOKUP = {**{k.lower(): k for k in LANGUAGES}, **{v: k for k, v in LANGUAGES.items()}}


def _text(params: dict, key: str, limit: int) -> str:
    v = str(params.get(key) or "").strip()
    if len(v) > limit:
        raise ValueError(f"{key} は {limit} 文字以内にしてください")
    return v


def normalize(kind: str, params: dict, ledger=None) -> dict:
    """画面から来た params を検証して整える。不正なら ValueError(日本語)。"""
    language = _LANG_LOOKUP.get(str(params.get("language") or "Japanese").strip().lower())
    if language is None:
        raise ValueError(f"言語は {', '.join(LANGUAGES)} のいずれかにしてください")
    duration = params.get("duration", -1)
    try:
        duration = -1 if duration in (None, "", "auto") else int(duration)
    except (TypeError, ValueError):
        raise ValueError("duration は秒数(整数)か -1(自動)で指定してください")
    if duration != -1 and not MIN_SECONDS <= duration <= MAX_SECONDS:
        raise ValueError(f"曲の長さは {MIN_SECONDS}〜{MAX_SECONDS} 秒か、自動(-1)にしてください")
    model = str(params.get("model") or "").strip()
    if model and not all(ch.isalnum() or ch in "-_." for ch in model):
        raise ValueError("model の名前が不正です")
    return {"genre": _text(params, "genre", 60) or "J-POP", "theme": _text(params, "theme", 200),
            "lyrics": _text(params, "lyrics", 6000), "language": language, "mood": _text(params, "mood", 100),
            "vocal": _text(params, "vocal", 100), "instrumental": bool(params.get("instrumental")),
            "duration": duration, "model": model or None}


# ---- prepare: 曲のプラン(LLM) ----

async def prepare(job: Job, ctx: Ctx) -> None:
    """テーマ・歌詞・キャプションのうち空のものを、管理役の LLM で作る。結果は job.result["plan"] に置く。"""
    p = job.params
    inst = p["instrumental"]
    plan = {"theme": p["theme"], "lyrics": INSTRUMENTAL_LYRICS if inst else p["lyrics"],
            "caption": songplan.template_caption(p["genre"], p["mood"], p["vocal"], inst)}
    want_lyrics = not inst and not p["lyrics"]
    if want_lyrics or not p["theme"]:
        job.message = "テーマ・歌詞・キャプションを作っています"
        try:
            if ctx.llm is None:
                raise LlmError("LLM が設定されていません")
            made = await songplan.make_plan(ctx.llm.chat, p["genre"], p["language"], p["mood"], p["vocal"], p["theme"], want_lyrics)
            plan.update(theme=made["theme"], caption=made["caption"])
            if want_lyrics:
                plan["lyrics"] = made["lyrics"]
        except (LlmError, songplan.SongPlanError) as e:
            if want_lyrics:
                raise JobFailed(f"歌詞を作れませんでした: {e}")
            log.warning("LLM が使えないので、キャプションはひな形で続けます: %s", e)   # 歌詞が手元にあるなら生成は続けられる
    plan["theme"] = plan["theme"] or p["genre"]
    job.result["plan"] = plan


# ---- run: ACE-Step ----

def _client(ctx: Ctx, timeout: float = 60.0) -> httpx.AsyncClient:
    spec = ctx.sched.comps[COMPONENT].spec
    return httpx.AsyncClient(base_url=spec.base_url, timeout=timeout, transport=ctx.transport)


def _ok(r: httpx.Response) -> bool:
    """ACE は {code, data, error} で包んで返す。HTTP 200 でも code が 200 でなければ失敗。"""
    try:
        return r.status_code == 200 and r.json().get("code", 200) == 200
    except (ValueError, AttributeError):
        return False


def _envelope_error(r: httpx.Response) -> str:
    try:
        return str(r.json().get("error") or _error_text(r))
    except (ValueError, AttributeError):
        return _error_text(r)


async def _format_input(job: Job, c: httpx.AsyncClient, plan: dict) -> dict:
    """ACE 自身の LM で、BPM・調・拍子を決める。失敗しても生成は続ける(空の dict を返す)。"""
    job.message = "ACE-Step の LM で BPM と調を決めています(初回はモデルの読み込みで時間がかかります)"
    body = {"prompt": plan["caption"], "lyrics": "" if job.params["instrumental"] else plan["lyrics"], "temperature": 0.4}
    try:
        r = await c.post("/format_input", json=body, timeout=FORMAT_TIMEOUT_S)
        if not _ok(r):
            log.warning("format_input に失敗: %s", _envelope_error(r))
            return {}
        data = r.json().get("data") or {}
    except (httpx.HTTPError, ValueError) as e:
        log.warning("format_input に失敗: %s", e)
        return {}
    meta = {}
    try:
        bpm = int(data.get("bpm") or 0)
    except (TypeError, ValueError):
        bpm = 0
    if bpm > 0:
        meta["bpm"] = bpm
    for key in ("key_scale", "time_signature"):
        if data.get(key):
            meta[key] = str(data[key])
    return meta


def build_release_body(p: dict, plan: dict, meta: dict, task_id: str, seed: int) -> dict:
    """Easy Music の簡単モードと同じ設定(turbo 8 ステップ、thinking、CoT のキャプションと言語)。"""
    inst = p["instrumental"]
    body = {"task_id": task_id, "prompt": plan["caption"], "lyrics": plan["lyrics"], "thinking": True,
            "vocal_language": "" if inst else LANGUAGES[p["language"]], "audio_duration": p["duration"], "batch_size": 1,
            "audio_format": "mp3", "inference_steps": 8, "guidance_scale": 3.0, "seed": seed,
            "use_cot_caption": True, "use_cot_language": True, "lm_negative_prompt": DEFAULT_NEGATIVE_PROMPT}
    if inst:
        body["instrumental"] = True
    if meta:
        body.update(meta)
        body["use_cot_metas"] = False       # すでに決めたので、生成時の重複した推定は省く
    if p["model"]:
        body["model"] = p["model"]
    return body


async def _send_cancel(c: httpx.AsyncClient, task_id: str) -> bool:
    try:
        return _ok(await c.post("/v1/cancel", json={"task_id": task_id}, timeout=15.0))
    except httpx.HTTPError:
        return False


def _first_result(item: dict) -> dict:
    """query_result の 1 件の result(JSON 文字列のリスト)の先頭。読めなければ空の dict。"""
    raw = item.get("result")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return raw[0] if isinstance(raw, list) and raw and isinstance(raw[0], dict) else {}


async def _poll(job: Job, c: httpx.AsyncClient) -> dict:
    """ACE のタスクが終わるまで 2 秒おきに見る。成功なら結果(file と metas を含む dict)を返す。"""
    t0, errors, cancel_sent = time.monotonic(), 0, False
    while True:
        if job.cancel_requested and not cancel_sent:
            cancel_sent = await _send_cancel(c, job.remote_id)
        try:
            r = await c.post("/query_result", json={"task_id_list": [job.remote_id]}, timeout=15.0)
            r.raise_for_status()
            data = r.json().get("data") or []
            errors = 0
        except (httpx.HTTPError, ValueError) as e:
            errors += 1
            if errors > MAX_POLL_ERRORS:
                raise JobFailed(f"ACE-Step と通信できません: {e}")
            await asyncio.sleep(POLL_S)
            continue
        item = data[0] if data else {}
        first = _first_result(item)
        status = item.get("status", 0)
        if status == 1:
            if not first.get("file"):
                raise JobFailed("ACE-Step が音声を返しませんでした")
            return first
        if status == 2:
            err = str(first.get("error") or (item.get("result") if isinstance(item.get("result"), str) else "") or "原因不明")
            if "cancel" in err.lower():
                raise JobCancelled()
            raise JobFailed(f"ACE-Step が失敗を報告しました: {err}")
        progress = float(first.get("progress") or 0)
        job.progress = max(job.progress, min(0.99, progress))
        job.message = f"生成中 {round(job.progress * 100)}%" if progress > 0 else "ACE-Step で生成しています"
        if time.monotonic() - t0 > TOTAL_TIMEOUT_S:
            await _send_cancel(c, job.remote_id)
            raise JobFailed("生成が 30 分以内に終わりませんでした")
        await asyncio.sleep(POLL_S)


def _audio_url(file: str) -> str:
    return file if file.startswith(("http", "/v1/audio")) else "/v1/audio?path=" + quote(file, safe="")


async def run(job: Job, ctx: Ctx) -> None:
    p, plan = job.params, job.result["plan"]
    t0 = time.monotonic()
    async with _client(ctx) as c:
        meta = await _format_input(job, c, plan)
        if job.cancel_requested:
            raise JobCancelled()

        seed = random.randint(0, 2**31 - 1)
        task_id = str(uuid.uuid4())     # こちらで決めて送る: 応答が遅れて待ちをやめても、この ID で中止できる
        job.remote_id, job.message = task_id, "ACE-Step に生成を頼んでいます"
        try:
            r = await c.post("/release_task", json=build_release_body(p, plan, meta, task_id, seed), timeout=SUBMIT_TIMEOUT_S)
        except httpx.HTTPError as e:
            await _send_cancel(c, task_id)      # 受け付けられていたら宙に浮かないよう止める
            raise JobFailed(f"ACE-Step に生成を頼めませんでした: {type(e).__name__}: {e}")
        if not _ok(r):
            raise JobFailed(f"ACE-Step が生成を受け付けませんでした: {_envelope_error(r)}")
        done = await _poll(job, c)

        dest = ctx.outputs_dir / "audio" / f"{job.id}.mp3"
        job.message = "音楽を取り込んでいます"
        await _download(c, _audio_url(done["file"]), dest)
        metas = done.get("metas") or {}
        info = {"bpm": metas.get("bpm") or meta.get("bpm"), "key_scale": metas.get("keyscale") or meta.get("key_scale"),
                "time_signature": metas.get("timesignature") or meta.get("time_signature")}
        job.result.update(generation_seconds=round(time.monotonic() - t0, 1), **info)
        seconds = (await asyncio.to_thread(media.probe, dest))["duration_s"] or metas.get("duration")   # ffprobe が無ければ ACE の値
        rec = await asyncio.to_thread(ctx.ledger.add_output, "audio", dest, job.tab, job.id, dest.name,
                                      {**p, "theme": plan["theme"], "lyrics": plan["lyrics"], "caption": plan["caption"],
                                       **info, "seed": seed, "kind": job.kind,
                                       "result": {k: v for k, v in job.result.items() if k != "plan"}},
                                      duration_s=seconds)
        job.outputs.append(rec["id"])


async def cancel(job: Job, ctx: Ctx) -> None:
    """実行中のジョブの中止要求を ACE へ送る(状態が失敗(cancelled by client)になったら _poll が JobCancelled を投げる)。"""
    if not job.remote_id:
        return
    async with _client(ctx, timeout=15.0) as c:
        await _send_cancel(c, job.remote_id)


HANDLER = Handler(component=COMPONENT, normalize=normalize, run=run, cancel=cancel, prepare=prepare)
HANDLERS = {"ace.simple": HANDLER}
