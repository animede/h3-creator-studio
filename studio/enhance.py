"""プロンプト強化: 各エンジンが持つ強化の API(H3 の /api/prompt/enhance、H3-Image の /api/prompts/enhance)を呼ぶ。

プロンプトの作り込み自体はエンジンの側にある。エンジンは環境変数(H3_LLM_URL / H3IMG_LLM_BASE_URL)で
管理役の LLM(llama-server)を指しているので、ここでは「LLM を起こす」「エンジンを準備する」「エンジンに頼む」の順に進めるだけ。
"""
import logging

import httpx

from .handlers.common import error_text
from .handlers.h3image import _upload as _upload_asset
from .jobs import JobFailed
from .llm import LlmError
from .scheduler import SwitchError

log = logging.getLogger("studio.enhance")

COMPONENTS = {"image": "h3image", "video": "h3"}
H3_MODES = ("brief", "storyboard", "translate", "h3-official")
H3_TASKS = ("t2va", "fl2va", "ref2va")
TIMEOUT_S = 600.0


class EnhanceError(Exception):
    """利用者に見せる失敗。status は HTTP ステータス。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def _fail(label: str, r: httpx.Response) -> EnhanceError:
    if r.status_code == 501:     # エンジンが LLM の接続先を知らない = 管理役が起動したエンジンではない
        return EnhanceError(409, f"{label} に管理役の LLM が設定されていません。管理役から起動した {label} でないと強化できません"
                                 "(外部で動いている場合は、いったん停止して管理役から起動し直してください)")
    return EnhanceError(502, f"{label} がプロンプトを強化できませんでした: {error_text(r)}")


async def _enhance_video(c: httpx.AsyncClient, text: str, mode, seconds, task, lang) -> dict:
    mode, task = mode or "h3-official", task or "t2va"
    if mode not in H3_MODES or task not in H3_TASKS:
        raise EnhanceError(400, f"mode は {H3_MODES}、task は {H3_TASKS} のいずれかです")
    form = {"text": text, "mode": mode, "task": task, "lang": lang or "en", "seconds": str(seconds or 10)}
    r = await c.post("/api/prompt/enhance", data=form)
    if r.status_code != 200:
        raise _fail("MiniMax-H3", r)
    return {"text": r.json()["result"]}


async def _enhance_image(c: httpx.AsyncClient, ledger, text: str, references: list) -> dict:
    """参照画像があれば編集用(edit)の強化。参照は一時的にエンジンへ送り、終わったら消す。"""
    assets = []
    try:
        for oid in references:
            rec = ledger.get_output(oid)
            if rec is None or rec["kind"] != "image":
                raise EnhanceError(400, f"参照画像が見つかりません: {oid}")
            assets.append(await _upload_asset(c, ledger.abs_path(rec["path"]), rec["filename"] or "ref.png"))
        body = {"prompt": text, "mode": "edit" if assets else "t2i", "conditions": [{"asset_id": a} for a in assets]}
        r = await c.post("/api/prompts/enhance", json=body)
        if r.status_code != 200:
            raise _fail("MiniMax-H3-Image", r)
        js = r.json()
        return {"text": js["prompt"], **{k: js[k] for k in ("aspect_ratio", "suggested_width", "suggested_height") if k in js}}
    finally:
        for a in assets:
            try:
                await c.delete(f"/api/assets/{a}")
            except httpx.HTTPError:
                pass


async def enhance_prompt(runner, tab: str, text: str, mode: str = None, seconds: float = None, task: str = None,
                         lang: str = None, references: list = ()) -> dict:
    """{"text": 強化したプロンプト, ...}。失敗は EnhanceError。"""
    ctx = runner.ctx
    text = (text or "").strip()
    if tab not in COMPONENTS:
        raise EnhanceError(400, "tab は image か video です")
    if not text:
        raise EnhanceError(400, "プロンプトを入力してください")
    name = COMPONENTS[tab]
    if name not in ctx.sched.comps:
        raise EnhanceError(409, f"{name} が無効になっています(studio.toml の [components.{name}] enabled を確認してください)")
    # 生成中でも強化はできる(LLM は CPU 専用で動く)。ただしエンジンが載っていないときは、載せるために仕事中のエンジンを降ろすことになるなら断る
    plan = ctx.sched.plan(name) if hasattr(ctx.sched, "plan") else None
    if plan is not None and plan.action == "wait":
        busy = "、".join(ctx.sched.comps[n].spec.label for n in plan.blockers) or "別のエンジン"
        raise EnhanceError(409, f"{ctx.sched.comps[name].spec.label} を GPU に載せられません({busy} が生成中のため)。"
                                "生成が終わってからもう一度押してください")
    if (plan is None or plan.action != "ready") and ctx.llm is not None:
        # エンジンを GPU に載せる前に、管理役の LLM が GPU に持っている VRAM を返す(GPU のジョブと同じ。持たせたまま載せると、12GB ではエンジンの取り分が
        # 減っていた)。LLM の区間(session)に入る前に呼ぶ(入った後だと、自分の区間の終わりを待ってしまう)。エンジンの後で起こす LLM は、空きに合わせて載る
        await ctx.llm.release_before_gpu()
    mark, idle = getattr(ctx.sched, "mark_busy", None), getattr(ctx.sched, "mark_idle", None)
    if mark is not None:
        mark(name)                 # 強化の最中は、別の生成がこのエンジンを GPU から降ろさないようにする
    try:
        return await _enhance(ctx, name, tab, text, mode, seconds, task, lang, references)
    finally:
        if idle is not None:
            idle(name)


async def _enhance(ctx, name: str, tab: str, text: str, mode, seconds, task, lang, references) -> dict:
    async with ctx.llm.session():
        try:
            await ctx.sched.ensure(name)                 # エンジンが応えられる状態に(強化の後、どのみち生成で使う)
            await ctx.llm.ensure_running()               # エンジンが話す相手。エンジンが載った後で起こすので、空きに合わせて GPU に載る
        except SwitchError as e:
            raise EnhanceError(503, f"{ctx.sched.comps[name].spec.label} を準備できませんでした: {e.message}")
        except LlmError as e:
            raise EnhanceError(503, f"LLM を起動できませんでした: {e}")
        spec = ctx.sched.comps[name].spec
        try:
            async with httpx.AsyncClient(base_url=spec.base_url, timeout=TIMEOUT_S, transport=ctx.transport) as c:
                if tab == "video":
                    return await _enhance_video(c, text, mode, seconds, task, lang)
                return await _enhance_image(c, ctx.ledger, text, list(references))
        except JobFailed as e:       # 参照画像のアップロードの失敗
            raise EnhanceError(502, e.message)
        except httpx.HTTPError as e:
            raise EnhanceError(502, f"{spec.label} と通信できませんでした: {type(e).__name__}: {e}")
