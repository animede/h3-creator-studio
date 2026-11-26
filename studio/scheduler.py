"""GPU エンジンの載せ替えと同居の状態機械。

exclusive: 今 GPU にいるエンジンを空けてから、次のエンジンを載せる(常駐は 1 つ)。
share: 総 VRAM に余裕があるとき、複数のエンジンを常駐させる。エンジンごとに VRAM の「利用枠」を割り当て、利用枠の合計が収まるかぎり
       他を降ろさずに載せる。足りなければ仕事中でないエンジンを、最後に使った時刻の古い順に降ろす。それでも足りなければ、
       仕事中のエンジンが空くまで待つ(エンジンの利用枠は、起動時に環境変数でエンジンへ渡す)。
"""
import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

import httpx

from . import h3imgcache, procman
from .components import ComponentNotInstalled, all_specs, default_budget_gb

log = logging.getLogger("studio.scheduler")

STATES = ("stopped", "starting", "ready", "unloaded", "stopping", "error")
GPU_FREE_MARGIN_MIB = 1536     # 空き確認: 基準 + 1.5GiB まで下がれば OK(share の判定の余白も同じ)
GPU_FREE_WAIT_S = 60
SHARE_MIN_TOTAL_MIB = 40 * 1024     # auto: 総 VRAM がこれ以上なら share


_NO_MODULE = re.compile(r"(?:No module named|ModuleNotFoundError:[^\n]*?module named)\s+['\"]?([\w.]+)")


def exit_message(label: str, name: str, code, tail: str) -> str:
    """起動中に終了したときの文。ログに「No module named x」があれば、仮想環境が不完全なので setup をやり直すよう案内する。"""
    m = _NO_MODULE.search(tail or "")
    if m:
        return (f"{label} の Python 環境が不完全です(モジュール {m.group(1)} がありません)。"
                f"スタジオを止めてから setup.bat --only {name}(Linux は ./setup.sh --only {name})を実行してください")
    if name == h3imgcache.COMPONENT and h3imgcache.friendly_error(tail):
        return h3imgcache.TE_MISSING_MESSAGE        # 起動時に読み込む設定(H3IMG_PRELOAD=1)で、テキストエンコーダが無かった
    return f"{label} が途中で終了しました(終了コード {code})"


class SwitchError(Exception):
    """載せ替え・起動の失敗。log_tail はエンジンのログ末尾(画面に出す)。"""

    def __init__(self, message: str, log_tail: str = ""):
        super().__init__(message)
        self.message = message
        self.log_tail = log_tail


class Busy(SwitchError):
    pass


def resolve_gpu_mode(setting: str, total_mib: Optional[int]) -> str:
    """[gpu] mode(auto / exclusive / share)を、実際に使う exclusive / share に決める。

    auto は総 VRAM が 40GiB 以上なら share、未満なら exclusive。share は利用枠の計算に総 VRAM が要るので、分からなければ exclusive。
    """
    if not total_mib or setting == "exclusive":
        return "exclusive"
    if setting == "share":
        return "share"
    return "share" if total_mib >= SHARE_MIN_TOTAL_MIB else "exclusive"


# ---- HTTP(テストではここを差し替える) ----

async def http_get_json(url: str, timeout: float = 3.0):
    """(ステータス, JSON または None)。接続できなければ (None, None)。"""
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.get(url)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, None
    except httpx.HTTPError:
        return None, None


async def http_post(url: str, timeout: float = 30.0) -> Optional[int]:
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            return (await c.post(url)).status_code
    except httpx.HTTPError:
        return None


class Operation:
    def __init__(self, kind: str, name: str):
        self.id = uuid.uuid4().hex[:8]
        self.kind, self.name = kind, name
        self.steps: list = []
        self.done = False
        self.error: Optional[str] = None
        self.log_tail = ""
        self.wait_reason: Optional[str] = None      # GPU の空きを待っている間の理由(ジョブの表示に写す)
        self.started = time.time()

    def add(self, msg: str) -> None:
        self.steps.append({"t": round(time.time() - self.started, 1), "msg": msg})
        log.info("[%s %s] %s", self.kind, self.name, msg)

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "name": self.name, "steps": self.steps,
                "done": self.done, "error": self.error, "log_tail": self.log_tail}


class Component:
    def __init__(self, spec):
        self.spec = spec
        self.state = "stopped"
        self.proc = None
        self.external = False
        self.last_error: Optional[str] = None
        self.budget_mib: Optional[int] = None    # share で起動したときの利用枠。止めるまで変えない(エンジンは起動時の利用枠のまま動く)
        self.since = time.time()
        self.died_at: Optional[float] = None     # 自分で起動したプロセスが、止めていないのに終わっていたのを見つけた時刻(ジョブの失敗の理由とやり直しに使う)


@dataclass
class Resident:
    """GPU に載っているエンジン 1 つぶんの記録。"""
    budget_mib: Optional[int] = None             # share のときの VRAM の利用枠(exclusive では None)
    last_used: float = field(default_factory=time.time)    # 降ろす順(古い方から)を決める


@dataclass
class Plan:
    """エンジンを ready にするときの見通し(何も変えずに調べた結果)。"""
    action: str                                  # ready: 載っている / fit: 降ろさずに載る / evict: 古い順に降ろせば載る / wait: 仕事中のエンジンが空くまで待つ / refuse: 載せられない
    victims: list = field(default_factory=list)  # evict: 先に降ろすエンジン(古い順)
    blockers: list = field(default_factory=list)   # wait: 終わるのを待つ仕事中のエンジン
    needs: set = field(default_factory=set)        # 降ろしたいエンジン(待っている仕事より後の仕事を、これらに入れないための目印)
    reason: str = ""                             # wait / refuse のときの、利用者向けの文


class Scheduler:
    def __init__(self, specs: dict = None, monitor=None, poll_s: float = 1.0, mode: str = "exclusive", total_mib: int = None):
        self.comps = {n: Component(s) for n, s in (specs if specs is not None else all_specs()).items()}
        self.monitor = monitor
        self.poll_s = poll_s
        self.mode = mode                         # exclusive: GPU の常駐は 1 エンジン / share: 利用枠が収まるかぎり複数(configure_gpu で決める)
        self.total_mib = total_mib               # 総 VRAM(share の利用枠の既定と、載せる判定に使う)
        self.gpu_wait_s = GPU_FREE_WAIT_S
        self.residents: dict = {}                # GPU に載っているエンジンの名前 -> Resident(exclusive では高々 1 つ)
        self.baseline_mib: Optional[int] = None
        self.operations: dict = {}
        self._busy: dict = {}                    # 仕事を割り当てたエンジン -> 件数(JobRunner が付け外しする。仕事中のエンジンは降ろさない)
        self._listeners: list = []
        self._gpu_lock = asyncio.Lock()
        self._cpu_lock = asyncio.Lock()
        self._tasks: set = set()

    @property
    def gpu_resident(self) -> Optional[str]:
        """互換用: 常駐中のエンジンの名前(exclusive では唯一の常駐者、share では最初の常駐者)。いなければ None。"""
        return next(iter(self.residents), None)

    @gpu_resident.setter
    def gpu_resident(self, name: Optional[str]) -> None:
        self.residents = {name: Resident()} if name else {}

    def configure_gpu(self, setting: str = "auto") -> str:
        """[gpu] mode を、総 VRAM(監視の最初の値)で exclusive / share に決める。起動時に 1 回呼ぶ。"""
        gpu = self.monitor.snapshot().get("gpu") if self.monitor else None
        self.total_mib = gpu["total_mib"] if gpu else None
        self.mode = resolve_gpu_mode(setting, self.total_mib)
        if setting == "share" and self.mode != "share":
            log.warning("[gpu] mode = share ですが、総 VRAM が分からないので exclusive で動かします")
        log.info("GPU の使い方: %s(設定 %s、総 VRAM %s MiB)", self.mode, setting, self.total_mib)
        return self.mode

    # ---- 状態 ----

    def _set(self, c: Component, state: str, error: str = None) -> None:
        c.state, c.since, c.last_error = state, time.time(), error
        self._notify()

    def watch(self, callback) -> None:
        """エンジンの状態や仕事中の印が変わったときに呼ばれる関数を登録する(JobRunner が、待っているジョブを見直すのに使う)。"""
        self._listeners.append(callback)

    def _notify(self) -> None:
        for cb in list(self._listeners):
            try:
                cb()
            except Exception:  # noqa: BLE001  通知の失敗で載せ替えを止めない
                log.exception("状態の通知に失敗")

    def new_operation(self, kind: str, name: str) -> Operation:
        op = Operation(kind, name)
        self.operations[op.id] = op
        for old in list(self.operations)[:-20]:
            del self.operations[old]
        return op

    def current_operation(self) -> Optional[dict]:
        if not self.operations:
            return None
        ops = list(self.operations.values())
        running = [o for o in ops if not o.done]
        return (running[-1] if running else ops[-1]).to_dict()

    def is_busy(self) -> bool:
        return any(not o.done for o in self.operations.values())

    def gpu_switching(self) -> bool:
        """GPU エンジンの起動・載せ替え・停止の操作が進行中か(管理役の LLM を CPU 専用で動かす目安)。"""
        return any(not o.done and o.name in self.comps and self.comps[o.name].spec.uses_gpu for o in list(self.operations.values()))

    def _lock_for(self, c: Component) -> asyncio.Lock:
        return self._gpu_lock if c.spec.uses_gpu else self._cpu_lock

    def rss_map(self) -> dict:
        return {n: procman.rss_bytes(c.proc) for n, c in self.comps.items() if c.proc is not None}

    def status(self) -> dict:
        out = {}
        for n, c in self.comps.items():
            rss = procman.rss_bytes(c.proc) if c.proc is not None else None
            r = self.residents.get(n)
            out[n] = {"label": c.spec.label, "state": c.state, "port": c.spec.port, "external": c.external,
                      "gpu_resident": r is not None, "budget_mib": r.budget_mib if r else None, "busy": n in self._busy,
                      "uses_gpu": c.spec.uses_gpu,
                      "can_unload": c.spec.can_unload, "rss_mib": int(rss / 1048576) if rss else None,
                      "since": c.since, "error": c.last_error}
        return out

    def resident_status(self) -> list:
        """常駐中のエンジン(利用枠つき)。画面の状態バーに出す。"""
        return [{"name": n, "label": self.comps[n].spec.label, "budget_mib": r.budget_mib, "busy": n in self._busy}
                for n, r in list(self.residents.items()) if n in self.comps]

    # ---- 仕事中の印(JobRunner が、仕事を割り当てるときに付け、終わったら外す) ----

    def mark_busy(self, name: str) -> None:
        """エンジンに仕事を割り当てた。外すまで、このエンジンは降ろさず、利用枠も押さえたままにする。"""
        self._busy[name] = self._busy.get(name, 0) + 1
        self._touch(name)

    def mark_idle(self, name: str) -> None:
        n = self._busy.get(name, 0) - 1
        if n > 0:
            self._busy[name] = n
        else:
            self._busy.pop(name, None)
        self._touch(name)
        self._notify()

    def _touch(self, name: str) -> None:
        r = self.residents.get(name)
        if r is not None:
            r.last_used = time.time()

    # ---- 起動時: すでに動いているエンジンを取り込む ----

    async def adopt_running(self) -> None:
        for n, c in self.comps.items():
            proc = await asyncio.to_thread(procman.adopt_if_running, c.spec)
            if proc is None:
                continue
            status, js = await http_get_json(c.spec.health_url)
            c.proc, c.external = proc, True
            self._set(c, "ready" if status == 200 and c.spec.health_ok(js) else "starting")
            if c.spec.uses_gpu:
                if self.mode == "share":
                    self.residents[n] = Resident(budget_mib=self._budget_of(c))      # 外部のエンジンも、宣言の利用枠で数える
                elif self.gpu_resident is None:
                    self.gpu_resident = n
                else:
                    log.warning("GPU エンジンが複数動いています(%s と %s)。VRAM が足りなくなるかもしれません", self.gpu_resident, n)
        if self.mode == "share":
            self._note_baseline()      # 何も載っていなければ、起動時に他のアプリが使っていた量を覚える

    # ---- 実行(バックグラウンド) ----

    def launch(self, kind: str, name: str) -> Operation:
        """ensure / start / stop をバックグラウンドで走らせ、操作 ID 付きの Operation を返す。"""
        if name not in self.comps:
            raise KeyError(name)
        op = self.new_operation(kind, name)
        task = asyncio.create_task(self._run(op, getattr(self, kind)(name, op)))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return op

    async def _run(self, op: Operation, coro) -> None:
        try:
            await coro
        except SwitchError as e:
            op.error, op.log_tail = e.message, e.log_tail
            op.add(f"失敗: {e.message}")
        except Exception as e:  # noqa: BLE001
            log.exception("操作に失敗")
            op.error = f"{type(e).__name__}: {e}"
        finally:
            op.done = True

    # ---- 入口 ----

    async def ensure(self, name: str, op: Operation = None) -> None:
        """生成の前に必ず呼ぶ。このエンジンを使える状態にする。GPU エンジンは、exclusive なら唯一の常駐者に、share なら利用枠の収まる範囲で載せる。"""
        own = op is None
        op = op or self.new_operation("ensure", name)
        c = self.comps[name]
        try:
            await self._ensure_impl(c, op)
        except SwitchError as e:
            if own:
                op.error, op.log_tail = e.message, e.log_tail
                op.add(f"失敗: {e.message}")
            raise
        finally:
            if own:
                op.done = True

    async def _ensure_impl(self, c: Component, op: Operation) -> None:
        name = c.spec.name
        if self.mode == "share" and c.spec.uses_gpu:
            await self._ensure_share(c, op)
            return
        async with self._lock_for(c):
            self._check_alive(c)
            if c.spec.uses_gpu:
                if self.gpu_resident == name and c.state == "ready":
                    op.add("準備完了")
                    return
                self._note_baseline(name)
                if self.gpu_resident and self.gpu_resident != name:
                    await self._release(self.comps[self.gpu_resident], op, target=c)
            await self._bring_up(c, op)
            op.add("準備完了")

    async def _ensure_share(self, c: Component, op: Operation) -> None:
        """share: 載っていれば何もしない。利用枠が収まれば他を降ろさずに載せ、足りなければ仕事中でないエンジンを古い順に降ろす。
        仕事中のエンジンを降ろさないと足りないときは、その仕事が終わるまでロックを持たずに待つ。"""
        name, waited = c.spec.name, None
        while True:
            if self._ready_resident(c):          # 載っていれば何も要らない(別のエンジンを載せている最中でも待たない)
                self._touch(name)
                op.wait_reason = None
                op.add("準備完了")
                return
            async with self._gpu_lock:           # 判定から載せ終わりまで 1 つずつ(判定が別の載せ替えとすれ違わないように)
                self._check_alive(c)
                if self._ready_resident(c):
                    self._touch(name)
                    op.wait_reason = None
                    op.add("準備完了")
                    return
                self._note_baseline(name)
                plan = self.plan(name, admitting=True)
                if plan.action == "refuse":
                    raise SwitchError(plan.reason)
                if plan.action != "wait":
                    op.wait_reason = None
                    for victim in plan.victims:
                        await self._release(self.comps[victim], op, target=c)
                    await self._admit(c, op)
                    op.add("準備完了")
                    return
            if plan.reason != waited:            # 待つ間にロックは離す(常駐中のエンジンの仕事を止めない)
                waited = op.wait_reason = plan.reason
                op.add(plan.reason)
            await asyncio.sleep(self.poll_s)

    async def start(self, name: str, op: Operation = None) -> None:
        """エンジンを起動するだけ(常駐者は変えない)。GPU を使用中の別エンジンがいれば断る。"""
        own = op is None
        op = op or self.new_operation("start", name)
        c = self.comps[name]
        try:
            await self._start_impl(c, op)
        except SwitchError as e:
            if own:
                op.error, op.log_tail = e.message, e.log_tail
                op.add(f"失敗: {e.message}")
            raise
        finally:
            if own:
                op.done = True

    async def _start_impl(self, c: Component, op: Operation) -> None:
        name = c.spec.name
        async with self._lock_for(c):
            self._check_alive(c)
            if c.spec.uses_gpu and self.mode == "share":
                plan = self.plan(name)
                if plan.action == "refuse":
                    raise SwitchError(plan.reason)
                if plan.action not in ("ready", "fit"):
                    busy = f"({'、'.join(self._short(n) for n in plan.blockers)} が生成中です)" if plan.blockers else ""
                    raise SwitchError(f"{c.spec.label} を載せる GPU の空きがありません{busy}。「準備」で切り替えてください")
            elif c.spec.uses_gpu and self.gpu_resident not in (None, name):
                raise SwitchError(f"{self.comps[self.gpu_resident].spec.label} が GPU を使っています。「準備」で切り替えてください")
            if c.spec.uses_gpu:
                self._note_baseline(name)
            await self._admit(c, op)
            op.add("起動しました")

    async def stop(self, name: str, op: Operation = None) -> None:
        own = op is None
        op = op or self.new_operation("stop", name)
        c = self.comps[name]
        try:
            await self._stop_impl(c, op)
        except SwitchError as e:
            if own:
                op.error, op.log_tail = e.message, e.log_tail
                op.add(f"失敗: {e.message}")
            raise
        finally:
            if own:
                op.done = True

    async def _stop_impl(self, c: Component, op: Operation) -> None:
        async with self._lock_for(c):
            if c.external:
                raise SwitchError(f"{c.spec.label} は外部で動いているため、ここからは停止しません")
            await self._stop_process(c, op)
            op.add("停止しました")

    # ---- 載せる判定(share の利用枠) ----

    def _default_budget_mib(self, c: Component) -> int:
        """利用枠の既定: studio.toml の vram_budget_gb → 総 VRAM からの既定(h3 / h3image / ace)→ 1 回の生成に要る量の目安。"""
        spec = c.spec
        if spec.vram_budget_gb:
            return int(spec.vram_budget_gb * 1024)
        gb = default_budget_gb(spec.name, self.total_mib) if self.total_mib else None
        return int(gb * 1024) if gb else spec.vram_need_mib

    def _budget_of(self, c: Component) -> int:
        """このエンジンの利用枠(MiB)。share で起動したエンジンは、止めるまで起動時の利用枠のまま(reload でも変わらない)。"""
        if c.budget_mib and c.proc is not None and not c.external:
            return c.budget_mib
        return self._default_budget_mib(c)

    def _baseline(self) -> int:
        """管理役の起動時に、他のアプリが使っていた VRAM(MiB)。測れていなければ、いまの使用量から常駐中の利用枠を引いて見積もる。"""
        if self.baseline_mib is not None:
            return self.baseline_mib
        gpu = self.monitor.snapshot().get("gpu") if self.monitor else None
        if not gpu:
            return 0
        return max(0, gpu["used_mib"] - sum(self._budget_of(self.comps[n]) for n in self.residents if n in self.comps))

    def capacity_mib(self) -> Optional[int]:
        """share で、エンジンの利用枠の合計が収まるべき上限: 総 VRAM - 他のアプリの使用量 - 余白 1.5GiB。総 VRAM が分からなければ None。"""
        if not self.total_mib:
            return None
        return max(0, self.total_mib - self._baseline() - GPU_FREE_MARGIN_MIB)

    def _pinned(self, c: Component) -> bool:
        """降ろせないエンジン(止められない外部の ACE など)。"""
        return c.external and not c.spec.can_unload

    def _last_used(self, name: str) -> float:
        r = self.residents.get(name)
        return r.last_used if r is not None else time.time()

    def _short(self, name: str) -> str:
        return re.split(r"[(（]", self.comps[name].spec.label)[0]

    def _wait_text(self, blockers: list) -> str:
        return f"GPU の空きを待っています({'、'.join(self._short(n) for n in blockers)} が生成中)"

    def _ready_resident(self, c: Component) -> bool:
        self._check_alive(c)
        return c.spec.name in self.residents and c.state == "ready"

    def plan(self, name: str, admitting: bool = False) -> Plan:
        """エンジン name を ready にするときの見通し(何も変えない)。JobRunner が仕事を始めてよいかの判断と、強化 API が載せてよいかの判断に使う。
        admitting は、ロックを持って実際に載せる直前の判定(share の _ensure_share)。仕事を割り当てただけでまだ載っていないエンジンの利用枠は数えない。"""
        c = self.comps[name]
        if not c.spec.uses_gpu:
            return Plan("ready")
        for n in list(self.residents):           # 落ちたエンジンの利用枠を押さえたままにしない
            if n in self.comps:
                self._check_alive(self.comps[n])
        return self._plan_share(c, admitting) if self.mode == "share" else self._plan_exclusive(c)

    def _plan_exclusive(self, c: Component) -> Plan:
        name = c.spec.name
        busy = [n for n in self._busy if n != name and n in self.comps and self.comps[n].spec.uses_gpu]
        if busy:          # 別の GPU エンジンに仕事が付いている間は載せ替えない(受け付けた順に 1 つずつ)
            return Plan("wait", blockers=busy, needs=set(busy), reason=self._wait_text(busy))
        res = self.gpu_resident
        if res == name and c.state == "ready":
            return Plan("ready")
        if res and res != name:
            return Plan("evict", victims=[res], needs={res})
        return Plan("fit")

    def _plan_share(self, c: Component, admitting: bool = False) -> Plan:
        name = c.spec.name
        if name in self.residents and c.state == "ready":
            return Plan("ready")
        cap, need = self.capacity_mib(), self._budget_of(c)
        held = {n: self._budget_of(self.comps[n]) for n in self.residents if n != name and n in self.comps}
        # 仕事を割り当てた(まだ載っていない)エンジンも、載るつもりで利用枠を押さえる(後から来た仕事を割り当てすぎないように)。ただし載せる直前の判定
        # (admitting)では数えない: 載せるのはロックで 1 つずつなので、まだ載っていないエンジンは自分の番に、載ったエンジンを見て判定する。数えると、割り当てた後に
        # 容量が縮んだとき(他のアプリが VRAM を使った)、まだ載っていない 2 つの仕事が互いを「生成中」と見て、どちらも載らずに待ち続けていた
        for n in (() if admitting else self._busy):
            if n != name and n not in held and n in self.comps and self.comps[n].spec.uses_gpu:
                held[n] = self._budget_of(self.comps[n])
        pinned = [n for n in held if self._pinned(self.comps[n])]

        def fits(names: list) -> bool:
            return cap is None or not names or sum(held[n] for n in names) + need <= cap      # 1 つだけなら、利用枠が大きくても載せる

        names = list(held)
        if fits(names):
            return Plan("fit")
        remaining, victims = list(names), []
        for n in sorted((n for n in names if n not in self._busy and n not in pinned), key=self._last_used):
            remaining.remove(n)
            victims.append(n)
            if fits(remaining):
                return Plan("evict", victims=victims, needs=set(victims))
        if not fits(pinned):
            return self._plan_pinned(c, pinned)
        # 動かせるエンジンを全部降ろせば収まるが、仕事中のエンジンが居る: 降ろしたいエンジン(使った時刻の古い順に、収まるまで)を控えて、仕事の終わりを待つ
        needs, remaining = set(), list(names)
        for n in sorted((n for n in names if n not in pinned), key=self._last_used):
            remaining.remove(n)
            needs.add(n)
            if fits(remaining):
                break
        blockers = [n for n in names if n in needs and n in self._busy]
        return Plan("wait", blockers=blockers, needs=needs, reason=self._wait_text(blockers))

    def _plan_pinned(self, c: Component, pinned: list) -> Plan:
        """止められない外部のエンジンが居て、利用枠では収まらない: 今までどおり、GPU の実際の空きに載せる分があれば同居し、無ければ断る。"""
        free, need = self._free_mib(), c.spec.vram_need_mib + 1024
        if free is not None and need and free >= need:
            return Plan("fit")
        return Plan("refuse", reason=self._external_refusal(self.comps[pinned[0]].spec))

    def _external_refusal(self, spec) -> str:
        free = self._free_mib()
        hint = f"(GPU の空き {free // 1024}GiB では同居できません)" if free is not None else ""
        return (f"{spec.label} は外部で動いていて止められません{hint}。先に自分で停止するか、"
                f"studio.local.toml の [components.{spec.name}] に別の port を書いて管理役に起こさせてください")

    # ---- 内部 ----

    def check_alive(self, name: str) -> None:
        """エンジン name(自分で起動したもの)が落ちていたら stopped に戻す。ensure を通らない使い方(TTS が「ready なら ensure を飛ばす」)の前に呼ぶ。"""
        c = self.comps.get(name)
        if c is not None:
            self._check_alive(c)

    def engine_died(self, name: str, since: float) -> Optional[bool]:
        """エンジン name(自分で起動したもの)のプロセスが、時刻 since 以降に終わっていたか(落ちていれば、ここで stopped に戻す)。
        ジョブがエンジンと通信できなくなったとき、エンジンが死んだのか(RAM 不足で OS に止められた、など)を見分けるのに使う。
        割り当ての見直し(plan)が先に落ちたのを見つけていても、その時刻(died_at)で分かる。
        終わっていれば True、まだ動いていれば False。見張れるプロセスが無い(外部で動いていたもの・起動していない)なら None(待っても分からない)。"""
        c = self.comps.get(name)
        if c is None:
            return None
        self._check_alive(c)
        if c.died_at is not None and c.died_at >= since:
            return True
        return False if c.proc is not None and not c.external else None

    def _check_alive(self, c: Component) -> None:
        """自分で起動したエンジンが落ちていたら stopped に戻す(見つけた時刻を died_at に残す)。"""
        if c.proc is not None and not c.external and not procman.is_alive(c.proc):
            log.warning("%s のプロセスが終了していました", c.spec.label)
            c.died_at = time.time()
            c.proc = None
            self.residents.pop(c.spec.name, None)
            if c.state in ("ready", "unloaded", "starting"):
                self._set(c, "stopped")

    def _note_baseline(self, target: str = None) -> None:
        """どの GPU エンジンも載っていない今の VRAM 使用量を、空き確認の基準として覚える。target(いま載せようとしているエンジン)以外の GPU エンジンに
        仕事を割り当ててある間は測り直さない(その仕事は今の基準の容量で割り当てたので、ここで容量が縮むと、その仕事が載れなくなる)。"""
        gpu = self.monitor.snapshot().get("gpu") if self.monitor else None
        if any(n != target and n in self.comps and self.comps[n].spec.uses_gpu for n in self._busy):
            return
        if gpu and self.gpu_resident is None and not any(
                c.state in ("starting", "ready") and c.spec.uses_gpu for c in self.comps.values()):
            self.baseline_mib = gpu["used_mib"]

    def _free_mib(self) -> Optional[int]:
        gpu = self.monitor.snapshot().get("gpu") if self.monitor else None
        return (gpu["total_mib"] - gpu["used_mib"]) if gpu else None

    def _held_mib(self) -> int:
        """いま常駐しているエンジンの利用枠の合計(share。exclusive は 0)。"""
        if self.mode != "share":
            return 0
        return sum(self._budget_of(self.comps[n]) for n in self.residents if n in self.comps)

    async def _release(self, c: Component, op: Operation, target: Component = None) -> None:
        """今 GPU にいるエンジン c を空ける。unload できればプロセスは残し、できなければ止める。

        止められない外部のエンジンでも、GPU に target の分の空きがあれば止めずに同居する(大きな GPU の開発機向け)。
        """
        spec = c.spec
        if c.external and not spec.can_unload and target is not None:
            free, need = self._free_mib(), target.spec.vram_need_mib + 1024
            if free is not None and need and free >= need:
                op.add(f"{spec.label} は外部で動いていますが、GPU に {free // 1024}GiB の空きがあるので止めずに同居します")
                return
        if spec.can_unload:
            op.add(f"{spec.label} を GPU から降ろしています")
            code = await http_post(spec.base_url + spec.unload_path, timeout=120)
            if code == 409:
                raise Busy("生成中のエンジンがあるので切り替えられません")
            if code is None and await self._gone(c):
                self._forget(c, op)
            elif code is None or code >= 300:
                raise SwitchError(f"{spec.label} の unload に失敗しました(HTTP {code})", procman.log_tail(spec.name, 20))
            else:
                self._set(c, "unloaded")
        elif c.external:
            if not await self._gone(c):
                raise SwitchError(self._external_refusal(spec))
            self._forget(c, op)
        else:
            await self._stop_process(c, op)
        self.residents.pop(spec.name, None)
        await self._wait_gpu_free(op, keep_mib=self._held_mib())

    async def _gone(self, c: Component) -> bool:
        """エンジンのプロセスがもう無いか: 準備確認の URL が応答せず、自分で起動したものならプロセスも終わっている(外部のものはプロセスを見られない)。
        外部で動いていたエンジンが閉じられると、常駐の記録が残ったまま unload が届かず、他のエンジンの仕事がすべて失敗し続けていた。"""
        if not c.external and procman.is_alive(c.proc):
            return False          # 応答しなくても、生きているプロセスは VRAM を持っているかもしれない(今までどおり失敗にする)
        status, _ = await http_get_json(c.spec.health_url)
        return status is None

    def _forget(self, c: Component, op: Operation) -> None:
        """もう動いていないエンジンを、止まったものとして扱う(常駐から外し、外部の印も外す。次に使うときは管理役が起動し直す)。"""
        op.add(f"{c.spec.label} はもう動いていないので、止まったものとして扱います")
        c.proc, c.external, c.budget_mib = None, False, None
        self.residents.pop(c.spec.name, None)
        self._set(c, "stopped")

    async def _stop_process(self, c: Component, op: Operation) -> None:
        op.add(f"{c.spec.label} を停止しています")
        self._set(c, "stopping")
        proc, c.proc = c.proc, None
        if proc is not None:
            await asyncio.to_thread(procman.stop, proc)
        self.residents.pop(c.spec.name, None)
        c.budget_mib = None
        self._set(c, "stopped")

    async def _wait_gpu_free(self, op: Operation, keep_mib: int = 0) -> None:
        """降ろした分の VRAM が空くのを待つ。share では、残っているエンジンが利用枠まで使っていてもよいものとして数える(keep_mib)。"""
        gpu = self.monitor.snapshot().get("gpu") if self.monitor else None
        if not gpu or self.baseline_mib is None:
            return     # 計れないときは待たない
        op.add("GPU メモリが空くのを待っています")
        limit = self.baseline_mib + GPU_FREE_MARGIN_MIB + keep_mib
        t0 = time.time()
        while time.time() - t0 < self.gpu_wait_s:
            gpu = self.monitor.snapshot().get("gpu")
            if gpu and gpu["used_mib"] <= limit:
                return
            await asyncio.sleep(self.poll_s)
        op.add("GPU メモリが完全には空きませんでした(続行します)")

    async def _admit(self, c: Component, op: Operation) -> None:
        """c を載せる(_bring_up)。share では載せている間も利用枠を押さえ、失敗したら返す(別の判定が、この分を空きと見ないように)。"""
        name = c.spec.name
        if self.mode != "share" or not c.spec.uses_gpu:
            await self._bring_up(c, op)
            return
        had = name in self.residents
        self.residents.setdefault(name, Resident(budget_mib=self._budget_of(c)))
        try:
            await self._bring_up(c, op)
        except BaseException:
            if not had:
                self.residents.pop(name, None)
            raise

    async def _bring_up(self, c: Component, op: Operation) -> None:
        """c を ready にする(起動 / reload / 取り込み)。"""
        spec = c.spec
        if c.state == "unloaded" and procman.is_alive(c.proc):
            op.add(f"{spec.label} を GPU に戻しています(モデルの読み込みに数分かかります)")
            code = await http_post(spec.base_url + spec.reload_path, timeout=spec.startup_timeout_s)
            if code == 409:
                raise Busy("生成中のエンジンがあるので切り替えられません")
            if code is None or code >= 300:
                self._set(c, "error", f"reload に失敗(HTTP {code})")
                raise SwitchError(f"{spec.label} の reload に失敗しました(HTTP {code})", procman.log_tail(spec.name, 20))
        elif c.state in ("stopped", "error") or c.proc is None:
            adopted = await asyncio.to_thread(procman.adopt_if_running, spec)
            if adopted:
                c.proc, c.external = adopted, True
                op.add(f"{spec.label} は既に動いているので、そのまま使います")
            else:
                budget_env, note = {}, ""
                if self.mode == "share" and spec.uses_gpu:      # share: 利用枠を環境変数でエンジンへ渡す(エンジンはその範囲に収まるよう自分で選ぶ)
                    c.budget_mib = self._budget_of(c)
                    budget_env, note = spec.budget_env(c.budget_mib), f"VRAM の利用枠 {c.budget_mib / 1024:.1f}GiB。"
                try:
                    c.proc, c.external = (procman.start(spec, extra_env=budget_env) if budget_env else procman.start(spec)), False
                except ComponentNotInstalled as e:
                    self._set(c, "error", e.hint)
                    raise SwitchError(e.hint)
                op.add(f"{spec.label} を起動しています({note}モデルの読み込みに数分かかります)")
            self._set(c, "starting")
        elif c.state != "ready":
            self._set(c, "starting")
        await self._wait_health(c)
        self._set(c, "ready")
        if spec.uses_gpu:
            if self.mode == "share":
                r = self.residents.setdefault(spec.name, Resident())
                r.budget_mib, r.last_used = self._budget_of(c), time.time()
            else:
                self.gpu_resident = spec.name

    async def _wait_health(self, c: Component) -> None:
        spec = c.spec
        t0 = time.time()
        while True:
            if not c.external and not procman.is_alive(c.proc):
                code = c.proc.popen.returncode if c.proc and c.proc.popen else "?"
                tail = procman.log_tail(spec.name, 20)
                msg = exit_message(spec.label, spec.name, code, tail)
                c.proc = None
                self._set(c, "error", msg)
                raise SwitchError(msg, tail)
            status, js = await http_get_json(spec.health_url)
            if status == 200 and spec.health_ok(js):
                return
            if time.time() - t0 > spec.startup_timeout_s:
                msg = f"{spec.label} が {spec.startup_timeout_s} 秒以内に準備できませんでした"
                proc, c.proc = c.proc, None
                if proc is not None and not c.external:
                    await asyncio.to_thread(procman.stop, proc)
                self._set(c, "error", msg)
                raise SwitchError(msg, procman.log_tail(spec.name, 20))
            await asyncio.sleep(self.poll_s)
