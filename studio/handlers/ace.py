"""ACE-Step(音楽)の簡単モードとおまかせ: 管理役の LLM で曲のプランを作り → ACE の LM で BPM・調を決め → 生成 → mp3 を取り込む。

流れ(Easy Music と同じ):
  ace.simple  prepare(部品を載せる前): テーマ・歌詞・キャプションのうち、空のものだけを LLM で作る(書いたものは作り直さない)
              run: モデルを確認(未読込なら /v1/init)→ /format_input で BPM・調・拍子 → /release_task → /query_result を 2 秒おきに見る → mp3 を取り込む
  ace.omakase 一言(query)を /release_task の sample_query で渡す。LLM は使わず、ACE の LM が全部決める
"""
import asyncio
import json
import logging
import random
import time
import uuid
from urllib.parse import quote

import httpx

from .. import genres, media, songplan
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
INIT_TIMEOUT_S = 900        # /v1/init(モデルの入れ替え)は XL で数十秒かかる
PRIMARY_SLOT = 1            # 既定の生成に使うスロット。入れ替えには使わない
FALLBACK_SLOT = 2           # スロット一覧が取れない古い ACE のとき
SFT_MAX_BATCH = 2           # SFT は CFG ありで実効バッチが倍になるので OOM を避ける
MIN_SECONDS, MAX_SECONDS = 10, 300
INSTRUMENTAL_LYRICS = "[Instrumental]"
CAPTION_MAX = 1000          # 利用者が書くキャプションの文字数の上限(画面の欄・生成のジョブ・テーマ/作詞の API で同じ)
DEFAULT_NEGATIVE_PROMPT = "low quality, noisy, distorted, muddy, clipping, off-key, out of tune, amateur, poorly mixed"

# 言語の英語名(LLM へ渡す)と ACE の vocal_language。名前でもコードでも受ける
LANGUAGES = {"Japanese": "ja", "English": "en", "Korean": "ko", "Chinese": "zh", "Spanish": "es", "French": "fr",
             "German": "de", "Italian": "it", "Portuguese": "pt", "Russian": "ru"}
_LANG_LOOKUP = {**{k.lower(): k for k in LANGUAGES}, **{v: k for k, v in LANGUAGES.items()}}
INFER_METHODS = ("ODE", "SDE")      # サンプリング方式(自動 = None は ACE の既定の ODE)


def _text(params: dict, key: str, limit: int) -> str:
    v = str(params.get(key) or "").strip()
    if len(v) > limit:
        raise ValueError(f"{key} は {limit} 文字以内にしてください")
    return v


def _viz(params: dict) -> str:
    """スペクトラムの雰囲気(自作のジャンルのチップが持つ viz。生成物の params に残り、曲のカードの色になる)。空は指定なし。"""
    viz = str(params.get("viz") or "").strip().lower()
    if viz and viz not in genres.VIZ_IDS:
        raise ValueError("viz は " + ", ".join(genres.VIZ_IDS) + " のいずれかにしてください")
    return viz


def _opt_int(params: dict, key: str, low: int, high: int, label: str):
    v = params.get(key)
    if v in (None, "", "auto"):
        return None
    try:
        v = int(v)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{label}は整数で指定してください")
    if not low <= v <= high:
        raise ValueError(f"{label}は {low}〜{high} にしてください")
    return v


def _advanced(params: dict, model: str) -> dict:
    """詳細設定(簡単もおまかせも共通)。空・未指定は None で、ACE の既定に任せる。"""
    shift = params.get("shift")
    try:
        shift = None if shift in (None, "", "auto") else float(shift)
    except (TypeError, ValueError):
        raise ValueError("shift は 1〜5 の数で指定してください")
    if shift is not None and not 1.0 <= shift <= 5.0:
        raise ValueError("shift は 1〜5 の数で指定してください")
    infer = str(params.get("infer_method") or "").strip().upper()
    if infer in ("AUTO", ""):
        infer = None
    elif infer not in INFER_METHODS:
        raise ValueError(f"infer_method は {', '.join(INFER_METHODS)} のいずれかにしてください")
    batch = _opt_int(params, "batch_size", 1, 4, "曲数") or 1
    return {"model": model or None, "steps": _opt_int(params, "steps", 1, 200, "STEP"),
            "seed": _opt_int(params, "seed", 0, 2**31 - 1, "シード"), "shift": shift, "infer_method": infer,
            "negative_prompt": _text(params, "negative_prompt", 500) or DEFAULT_NEGATIVE_PROMPT,
            "thinking": params.get("thinking") is not False, "batch_size": batch,
            "use_format_input": bool(params.get("use_format_input", False))}


def normalize(kind: str, params: dict, ledger=None) -> dict:
    """画面から来た params を検証して整える。不正なら ValueError(日本語)。"""
    language = _LANG_LOOKUP.get(str(params.get("language") or "Japanese").strip().lower())
    if language is None:
        raise ValueError(f"言語は {', '.join(LANGUAGES)} のいずれかにしてください")
    duration = params.get("duration", -1)
    try:
        duration = -1 if duration in (None, "", "auto") else int(duration)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("duration は秒数(整数)か -1(自動)で指定してください")
    if duration != -1 and not MIN_SECONDS <= duration <= MAX_SECONDS:
        raise ValueError(f"曲の長さは {MIN_SECONDS}〜{MAX_SECONDS} 秒か、自動(-1)にしてください")
    model = str(params.get("model") or "").strip()
    if model and not all(ch.isalnum() or ch in "-_." for ch in model):
        raise ValueError("model の名前が不正です")
    common = {"language": language, "duration": duration, "instrumental": bool(params.get("instrumental")),
              **_advanced(params, model)}
    if kind == "ace.omakase":
        query = _text(params, "query", 300)
        if not query:
            raise ValueError("どんな曲か、一言入力してください")
        return {"query": query, **common, "thinking": True}       # おまかせは thinking 固定(Easy Music と同じ)
    caption_auto = params.get("caption_auto")
    return {"genre": genres.clean_genre(params.get("genre")) or "J-POP", "viz": _viz(params), "theme": _text(params, "theme", 200),
            "lyrics": _text(params, "lyrics", 6000), "mood": _text(params, "mood", 100), "vocal": _text(params, "vocal", 100),
            "caption": _text(params, "caption", CAPTION_MAX), "caption_auto": None if caption_auto is None else bool(caption_auto),
            **common}


def _is_turbo(model) -> bool:
    return not model or "turbo" in model.lower()          # モデル名なしは turbo 扱い(ACE の既定を読めなかったときの備え。読めれば run() が実名を入れる)


def _needs_cfg(model) -> bool:
    """SFT / Base は CFG ガイダンスが要る(Turbo は蒸留済みで不要)。"""
    return bool(model) and any(k in model.lower() for k in ("sft", "base"))


def default_steps(model) -> int:
    return 8 if _is_turbo(model) else 50


def clamp_batch(model, batch: int) -> int:
    return min(batch, SFT_MAX_BATCH) if model and "sft" in model.lower() else batch


# ---- prepare: 曲のプラン(LLM) ----

async def _no_llm(*args, **kwargs) -> str:
    raise LlmError("LLM が設定されていません")


def _inst_caption(caption: str) -> str:
    return caption if "instrumental" in caption.lower() else "instrumental, no vocals, " + caption


async def prepare(job: Job, ctx: Ctx) -> None:
    """テーマ・歌詞・キャプションのうち、空のものだけを管理役の LLM で作る(書かれたものは作り直さない)。
    結果は job.result["plan"] に置く。おまかせ(ace.omakase)は LLM を使わず、一言を画面に見せるだけ。"""
    p = job.params
    if job.kind == "ace.omakase":
        job.result["plan"] = {"theme": p["query"], "caption": "ACE-Step の LM が曲の中身を決めます", "lyrics": ""}
        return
    inst = p["instrumental"]
    # 歌詞欄に短い一文(構造タグ無し、1 行、40 文字以下)が入っていたら、歌詞ではなくテーマとして扱う。
    # 「秋の空」のような 3 文字の歌詞で 12 秒の曲ができていた(2026-11-22)
    if looks_like_theme(p["lyrics"]):
        if not p["theme"]:
            p["theme"] = p["lyrics"].strip()
        p["lyrics"] = ""
        job.message = "歌詞欄の短い文はテーマとして使い、歌詞は AI が作ります"
    manual = songplan.clean_caption(p["caption"])
    auto = p["caption_auto"]            # None: 従来どおり(プランを作るついでに作る) / True: 空なら必ず AI が作る / False: ひな形
    want_lyrics = not inst and not p["lyrics"]
    want_plan = want_lyrics or not p["theme"]
    want_caption = not manual and auto is not False
    caption_in_plan = want_plan and want_caption and p["thinking"]     # thinking OFF は楽器まで書く詳しい説明(3〜5 文)が要るので別に頼む
    plan = {"theme": p["theme"], "lyrics": INSTRUMENTAL_LYRICS if inst else p["lyrics"],
            "caption": manual or songplan.template_caption(p["genre"], p["mood"], p["vocal"], inst)}
    chat = ctx.llm.chat if ctx.llm is not None else _no_llm
    custom = genres.custom_map(ctx.ledger)         # 自作のジャンル(ジャンルが自作の名前なら、その世界観と場面・感情の種を使う)
    if want_plan:
        job.message = "テーマ・歌詞・キャプションを作っています"
        try:
            made = await songplan.make_plan(chat, p["genre"], p["language"], p["mood"], p["vocal"], p["theme"],
                                            want_lyrics, caption_in_plan, custom=custom, user_caption=manual)      # 書いたキャプションだけ(AI が作ったものは渡さない)
            plan["theme"] = made["theme"]
            if caption_in_plan:
                plan["caption"] = made["caption"]
            if want_lyrics:
                plan["lyrics"] = made["lyrics"]
        except (LlmError, songplan.SongPlanError) as e:
            if want_lyrics:
                raise JobFailed(f"歌詞を作れませんでした: {e}")
            log.warning("LLM が使えないので、キャプションはひな形で続けます: %s", e)   # 歌詞が手元にあるなら生成は続けられる
    if want_caption and not caption_in_plan and (auto is True or want_plan):
        job.message = "キャプションを作っています"
        try:
            plan["caption"] = await songplan.make_caption(chat, p["genre"], plan["theme"], "" if inst else plan["lyrics"],
                                                          p["mood"], p["vocal"], p["language"], p["thinking"], inst, custom=custom)
        except (LlmError, songplan.SongPlanError) as e:
            log.warning("キャプションを作れなかったので、ひな形で続けます: %s", e)
    if inst:
        plan["caption"] = _inst_caption(plan["caption"])
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


async def list_models(base_url: str, transport=None) -> dict:
    """ACE の /v1/models を画面向けに整える(部品を起こしはしない)。動いていなければ空の一覧と note を返す。"""
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=5.0, transport=transport) as c:
            r = await c.get("/v1/models")
        if not _ok(r):
            raise ValueError(_envelope_error(r))
        data = r.json().get("data") or {}
    except (httpx.HTTPError, ValueError):
        return {"models": [], "default_model": None, "note": "ACE が起動していません"}
    default = data.get("default_model")
    models = [{"name": m["name"], "is_loaded": m.get("is_loaded") is not False, "vram_ok": m.get("vram_ok") is not False,
               "min_vram_gb": m.get("min_vram_gb"), "is_default": m["name"] == default}
              for m in data.get("models") or [] if m.get("name")]
    return {"models": models, "default_model": default}


def pick_slot(slots: list, model: str):
    """入れ替えに使うスロットを選ぶ(Easy Music の pick_slot_for_model)。primary(1)は既定の生成に使うので触らない。
    優先: そのモデルが入っているスロット → 未読込のスロット → GPU に載っていないスロット → 番号の大きいスロット。無ければ None。"""
    candidates = [s for s in slots if int(s.get("slot", 0)) != PRIMARY_SLOT]
    for s in candidates:
        if s.get("model") == model:
            return int(s["slot"])
    candidates.sort(key=lambda s: (bool(s.get("loaded")), bool(s.get("on_gpu")), -int(s["slot"])))
    return int(candidates[0]["slot"]) if candidates else None


async def _default_model(c: httpx.AsyncClient) -> str:
    """ACE が既定で使うモデル名(/v1/models の default_model)。一覧が取れない古い ACE では空を返す。"""
    try:
        r = await c.get("/v1/models", timeout=15.0)
        return str((r.json().get("data") or {}).get("default_model") or "") if _ok(r) else ""
    except (httpx.HTTPError, ValueError):
        return ""


async def _ensure_model(job: Job, c: httpx.AsyncClient, model) -> None:
    """選んだモデルが ACE のスロットに載っていなければ、primary 以外のスロットへ読み込む(/v1/init)。
    ACE は未読込のモデルを黙って既定に置き換えず、エラーにするので、生成の前に載せる。
    VRAM に載らないモデル(vram_ok=false)は断る。一覧が取れない古い ACE では、そのまま生成を頼む。"""
    if not model:
        return
    try:
        r = await c.get("/v1/models", timeout=15.0)
        models = ((r.json().get("data") or {}).get("models") or []) if _ok(r) else []
    except (httpx.HTTPError, ValueError):
        models = []
    if not models:
        log.warning("ACE のモデル一覧を取れないので、%s の読み込み確認を省きます", model)
        return
    entry = next((m for m in models if m.get("name") == model), None)
    if entry is None:
        raise JobFailed(f"{model} は ACE-Step の checkpoints にありません。setup で取得してください")
    if entry.get("is_loaded") is not False:
        return
    if entry.get("vram_ok") is False:
        raise JobFailed(f"{model} はこの GPU には載りません(約 {entry.get('min_vram_gb') or '?'}GB 必要)。2B 系のモデルを選んでください")
    try:
        slots = (((await c.get("/v1/residency", timeout=15.0)).json().get("data") or {}).get("slots")) or []
    except (httpx.HTTPError, ValueError):
        slots = [{"slot": FALLBACK_SLOT}]       # スロット一覧の無い古い ACE
    slot = pick_slot(slots, model)
    if slot is None:
        raise JobFailed("この ACE-Step はスロットが 1 本だけなので、起動後のモデル切り替えはできません。起動時の設定(ACESTEP_CONFIG_PATH)で指定してください")
    job.message = f"モデル {model} を読み込んでいます(数十秒かかります)"
    log.info("モデル %s をスロット %s に読み込みます", model, slot)
    try:
        r = await c.post("/v1/init", json={"model": model, "slot": slot}, timeout=INIT_TIMEOUT_S)
    except httpx.HTTPError as e:
        raise JobFailed(f"モデル {model} を読み込めませんでした: {type(e).__name__}: {e}")
    if not _ok(r):
        raise JobFailed(f"モデル {model} を読み込めませんでした: {_envelope_error(r)}")


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


def looks_like_theme(lyrics: str) -> bool:
    """歌詞欄の中身が、歌詞ではなくテーマ(短い一文)に見えるか。"""
    t = (lyrics or "").strip()
    return bool(t) and "[" not in t and "\n" not in t and len(t) <= 40


def build_release_body(p: dict, plan: dict, meta: dict, task_id: str, seed: int) -> dict:
    """Easy Music と同じ設定(thinking、CoT のキャプションと言語、モデルに合わせた STEP とガイダンス)。
    おまかせ(ace.omakase)は sample_query に一言を入れ、プロンプトと歌詞は空(ACE の LM が決める)。"""
    inst, model = p["instrumental"], p["model"]
    omakase = "query" in p
    query = p["query"] if omakase else ""
    if omakase and inst and "instrumental" not in query.lower():
        query += " (instrumental, no vocals)"       # ACE は一言の中の語から「歌なし」を読み取る
    body = {"task_id": task_id, "prompt": "" if omakase else plan["caption"], "lyrics": "" if omakase else plan["lyrics"],
            "thinking": p["thinking"], "vocal_language": "" if inst else LANGUAGES[p["language"]], "audio_duration": p["duration"],
            "batch_size": clamp_batch(model, p["batch_size"]), "audio_format": "mp3",
            "inference_steps": p["steps"] or default_steps(model), "guidance_scale": 7.0 if _needs_cfg(model) else 3.0, "seed": seed,
            "use_cot_caption": True, "use_cot_language": True, "lm_negative_prompt": p["negative_prompt"]}
    if omakase:
        body["sample_query"] = query
    if inst:
        body["instrumental"] = True
    if meta:
        body.update(meta)
        body["use_cot_metas"] = False       # すでに決めたので、生成時の重複した推定は省く
    for key in ("model", "shift", "infer_method"):
        if p[key] is not None:
            body[key] = p[key]
    return body


async def _send_cancel(c: httpx.AsyncClient, task_id: str) -> bool:
    try:
        return _ok(await c.post("/v1/cancel", json={"task_id": task_id}, timeout=15.0))
    except httpx.HTTPError:
        return False


def _results(item: dict) -> list:
    """query_result の 1 件の result(JSON 文字列のリスト。曲数ぶん)を dict のリストにする。読めなければ空。"""
    raw = item.get("result")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    return [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []


async def _poll(job: Job, c: httpx.AsyncClient) -> list:
    """ACE のタスクが終わるまで 2 秒おきに見る。成功なら結果(file と metas を含む dict)のリストを返す(曲数ぶん)。"""
    t0, errors, cancel_sent = time.monotonic(), 0, False
    while True:
        if job.cancel_requested and not cancel_sent:
            cancel_sent = True
            if not await _send_cancel(c, job.remote_id):
                # 古い ACE には /v1/cancel が無い: サーバは最後まで作るが、こちらは待つのをやめる
                raise JobCancelled()
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
        results = _results(item)
        first = results[0] if results else {}
        status = item.get("status", 0)
        if status == 1:
            files = [r for r in results if r.get("file")]
            if not files:
                raise JobFailed("ACE-Step が音声を返しませんでした")
            return files
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
    omakase = job.kind == "ace.omakase"
    t0 = time.monotonic()
    async with _client(ctx) as c:
        if not p["model"]:
            # モデル未指定(画面の「ACE の既定」)を turbo 扱いにすると、既定が XL-SFT のサーバへ 8 ステップ・CFG 3.0 を送って曲が壊れる(2026-11-22 に確認)。
            # ACE の既定のモデル名を読んで、STEP・ガイダンス・バッチ数をそのモデルに合わせる。読めない古い ACE は、これまでどおり turbo 扱い
            name = await _default_model(c)
            if name:
                log.info("モデル未指定なので ACE の既定 %s を使います", name)
                p["model"] = name
                if p["steps"] == (50 if _is_turbo(name) else 8):       # 画面の STEP は既定のモデルを知らずに選んだ値(turbo 前提の 8 など)。食い違うならモデルの既定に任せる
                    p["steps"] = None
        await _ensure_model(job, c, p["model"])
        # BPM・調は生成時に ACE の思考(CoT)に決めさせる。事前の format_input は同じ入力に BPM 200 や 40 を返すことがあり、
        # その値を use_cot_metas=False で押し付けると曲がでたらめになる(2026-11-22 に確認)。use_format_input=true で従来どおり
        meta = await _format_input(job, c, plan) if (not omakase and p.get("use_format_input")) else {}
        if job.cancel_requested:
            raise JobCancelled()

        seed = p["seed"] if p["seed"] is not None else random.randint(0, 2**31 - 1)
        task_id = str(uuid.uuid4())     # こちらで決めて送る: 応答が遅れて待ちをやめても、この ID で中止できる
        job.remote_id, job.message = task_id, "ACE-Step に生成を頼んでいます"
        try:
            r = await c.post("/release_task", json=build_release_body(p, plan, meta, task_id, seed), timeout=SUBMIT_TIMEOUT_S)
        except httpx.HTTPError as e:
            await _send_cancel(c, task_id)      # 受け付けられていたら宙に浮かないよう止める
            raise JobFailed(f"ACE-Step に生成を頼めませんでした: {type(e).__name__}: {e}")
        if not _ok(r):
            raise JobFailed(f"ACE-Step が生成を受け付けませんでした: {_envelope_error(r)}")
        # 問い合わせはサーバが返した task_id で行う。古い ACE(2026-11-20 以前)はこちらの ID を無視して
        # 自分の ID を振るので、こちらの ID で待つと永久に「生成中」になる
        try:
            returned = str(((r.json() or {}).get("data") or {}).get("task_id") or "")
        except ValueError:
            returned = ""
        if returned and returned != task_id:
            log.warning("ACE-Step が別の task_id を返しました(古い版): %s -> %s", task_id, returned)
            job.remote_id = returned
        done = await _poll(job, c)

        job.message = "音楽を取り込んでいます"
        metas = done[0].get("metas") or {}
        if omakase:     # ACE の LM が決めた中身を、画面と台帳に残す
            plan.update(caption=done[0].get("prompt") or metas.get("prompt") or "", lyrics=done[0].get("lyrics") or metas.get("lyrics") or "")
        info = {"bpm": metas.get("bpm") or meta.get("bpm"), "key_scale": metas.get("keyscale") or meta.get("key_scale"),
                "time_signature": metas.get("timesignature") or meta.get("time_signature")}
        model = p["model"] or done[0].get("dit_model")
        job.result.update(generation_seconds=round(time.monotonic() - t0, 1), seed=seed, model=model, **info)
        for i, res in enumerate(done):      # 曲数ぶん(batch_size)の mp3 を、それぞれ別の生成物として登録する
            suffix = f"_{i + 1}" if len(done) > 1 else ""
            dest = ctx.outputs_dir / "audio" / f"{job.id}{suffix}.mp3"
            await _download(c, _audio_url(res["file"]), dest)
            seconds = (await asyncio.to_thread(media.probe, dest))["duration_s"] or (res.get("metas") or {}).get("duration")   # ffprobe が無ければ ACE の値
            rec = await asyncio.to_thread(ctx.ledger.add_output, "audio", dest, job.tab, job.id, dest.name,
                                          {**p, "theme": plan["theme"], "lyrics": plan["lyrics"], "caption": plan["caption"],
                                           **info, "seed": seed, "model": model, "kind": job.kind,
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
HANDLERS = {"ace.simple": HANDLER, "ace.omakase": HANDLER}
