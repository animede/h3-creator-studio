"""スタジオ共通のジョブ実行: レーンごとの順番と、仕事を割り当てる dispatcher。

ジョブは「部品を ensure(必要なら載せ替え)→ 種類ごとのハンドラが部品の API を呼ぶ」の順に流れる。
レーンは部品ごとに 1 本(同じ部品の仕事は 1 つずつ)。GPU を使わない仕事(component=None)は CPU のレーンで、2 本まで同時に走る。
別のレーンの仕事は並行に走るが、GPU に同時に載るかは scheduler の判定による(exclusive なら 1 部品ずつ、share なら予算が収まるかぎり)。
"""
import asyncio
import logging
import time
import uuid
from collections import Counter, OrderedDict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional

from .ledger import Ledger
from .scheduler import Scheduler, SwitchError

log = logging.getLogger("studio.jobs")

KEEP_JOBS = 200
TERMINAL = ("completed", "failed", "cancelled")
CPU_LANE = "cpu"
CPU_SLOTS = 2                         # CPU のレーンで同時に走らせる数
WAIT_ORDER = "順番を待っています"
WAIT_PREPARE = "部品を準備しています"
DISPATCH_POLL_S = 2.0                 # 状態の通知が無くても、この間隔で割り当てを見直す(外部の部品が止まって VRAM が空いた、など)


class JobFailed(Exception):
    """ハンドラが利用者向けの失敗理由を伝えるための例外。"""

    def __init__(self, message: str, log_tail: str = ""):
        super().__init__(message)
        self.message, self.log_tail = message, log_tail


class JobCancelled(Exception):
    """部品が中断(interrupted)を報告した。"""


@dataclass
class Job:
    id: str
    tab: str                      # image / video / music(画面の /api/jobs)、mv / short / edit(制作・編集の管理役が入れるジョブ)
    kind: str                     # 例: h3image.t2i
    params: dict
    status: str = "queued"        # queued / preparing / running / completed / failed / cancelled
    progress: float = 0.0
    message: str = WAIT_ORDER
    lane: str = ""                # 部品の名前(h3 など)か "cpu"。同じレーンの仕事は 1 つずつ走る
    wait_reason: Optional[str] = WAIT_ORDER    # 待っている理由: 順番 / GPU の空き(xx が生成中)/ 部品の準備(走り出したら None)
    error: Optional[str] = None
    log_tail: str = ""
    created: float = field(default_factory=time.time)
    started: Optional[float] = None
    finished: Optional[float] = None
    outputs: list = field(default_factory=list)   # 生成物の ID
    steps: list = field(default_factory=list)     # 載せ替えの経過(scheduler の Operation の文言)
    result: dict = field(default_factory=dict)    # 部品が返した実測(秒数・VRAM など)
    remote_id: Optional[str] = None               # 部品側のジョブ ID(中止に使う)
    cancel_requested: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Ctx:
    """ハンドラに渡す道具一式。transport はテストで httpx.MockTransport を差し込む口。"""
    sched: Scheduler
    ledger: Ledger
    outputs_dir: Path
    transport: object = None
    llm: object = None            # studio.llm.Llm(歌詞づくりなど、LLM を使う prepare が使う)
    tts: object = None            # studio.tts.Tts(ショート動画が、セリフ・ナレーションの読み上げに使う)


@dataclass
class Handler:
    component: Optional[str]                                  # ensure する部品名(レーンもこれ)。GPU を使わない CPU の仕事は None(gpu も False に)
    normalize: Callable                                       # (kind, params, ledger) -> params(検証・整形済み)。不正なら ValueError
    run: Callable[[Job, Ctx], Awaitable[None]]
    cancel: Callable[[Job, Ctx], Awaitable[None]]
    prepare: Optional[Callable[[Job, Ctx], Awaitable[None]]] = None   # 部品を ensure する前の下ごしらえ(LLM の仕事など)
    gpu: bool = True                                                    # GPU を使う(LLM を止めてから ensure する)
    record: Optional[Callable[[dict], dict]] = None                     # 台帳(jobs 表)に残す params を作る。無ければ params そのまま
                                                                        # (編集のジョブは state の写しを持つので、指紋だけを残す。写しはメモリのジョブにだけ)


COMPONENT_NAMES = {"h3": "動画(MiniMax-H3)", "h3image": "画像(MiniMax-H3-Image)", "ace": "音楽(ACE-Step)"}


class JobRunner:
    def __init__(self, ctx: Ctx, handlers: dict, step_poll_s: float = 0.3, cpu_slots: int = CPU_SLOTS):
        self.ctx, self.handlers, self.step_poll_s, self.cpu_slots = ctx, handlers, step_poll_s, cpu_slots
        self.jobs: OrderedDict[str, Job] = OrderedDict()
        self.current: Optional[Job] = None     # 互換: いちばん最近に走り始めたジョブ(同時に複数走るので、全部は running())
        self.done_counter = 0                  # 終わったジョブの数(画面が、自分の知らないジョブの完了に気づく目印)
        self._gpu_claimed: set = set()         # GPU を占めているジョブの ID(LLM を止めてから終わるまで)
        self._tasks: dict = {}                 # 割り当て済み(準備中・実行中)のジョブの ID -> タスク。始めた順
        self._reserved: dict = {}              # ジョブの ID -> 載せるために降ろす部品(載せ終わるまで、後の仕事をそこに入れない)
        self._finished_hooks: list = []        # ジョブが終わったときに呼ぶ関数(on_finished)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._wake: Optional[asyncio.Event] = None
        self._dispatcher: Optional[asyncio.Task] = None
        self._stopping = False

    # ---- 起動と停止 ----

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._wake, self._stopping = asyncio.Event(), False
        self._dispatcher = asyncio.create_task(self._dispatch_loop(), name="job-dispatcher")
        watch = getattr(self.ctx.sched, "watch", None)
        if watch is not None:
            watch(self.wake)       # 部品が ready になった・仕事中の印が外れた、で割り当てを見直す

    async def stop(self) -> None:
        self._stopping = True
        tasks = [t for t in (self._dispatcher, *self._tasks.values()) if t is not None]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._dispatcher = None

    def wake(self) -> None:
        """割り当てを見直させる(別のスレッド、たとえば同期の API ルートからも呼べる)。"""
        loop = self._loop
        if loop is None or self._wake is None:
            return
        try:
            on_loop = asyncio.get_running_loop() is loop
        except RuntimeError:
            on_loop = False
        if on_loop:
            self._wake.set()
        else:
            loop.call_soon_threadsafe(self._wake.set)

    def on_finished(self, callback: Callable[[Job], None]) -> None:
        """ジョブが終わった(完了・失敗・中止のどれでも)ときに callback(job) を呼ぶ関数を登録する。MV の管理役が、次の仕事を入れるのに使う。
        呼ぶのは、そのジョブの後片づけ(レーンを空ける・仕事中の印を外す)が済んだ直後のイベントループなので、callback の中で submit してよい。
        callback は普通の関数(コルーチンではない)。例外は記録するだけで、他の callback やジョブには響かない。管理役の停止中は呼ばない。"""
        self._finished_hooks.append(callback)

    def _notify_finished(self, job: Job) -> None:
        for callback in list(self._finished_hooks):
            try:
                callback(job)
            except Exception:  # noqa: BLE001  通知先の失敗で、ジョブの実行を止めない
                log.exception("ジョブ完了の通知に失敗: %s", job.id)

    def _is_gpu(self, job: Job) -> bool:
        handler = self.handlers.get(job.kind)
        return bool(handler and handler.gpu and handler.component)

    def is_generating(self) -> bool:
        """GPU で生成している最中か(共有 GPU メモリの警告の判定に使う)。GPU を使うジョブのどれかが走っていれば True。"""
        return any(j.status == "running" and self._is_gpu(j) for j in list(self.jobs.values()))

    def gpu_busy(self) -> bool:
        """GPU を使うジョブが、LLM を止めてから終わるまでの間にあるか(この間、LLM は CUDA を隠して CPU だけで動く)。"""
        return bool(self._gpu_claimed)

    def gpu_pending(self) -> bool:
        """GPU を使う仕事が、走っているか順番を待っているか(待っている間も、LLM を GPU 版に戻して行き来させない)。"""
        return self.gpu_busy() or any(j.status == "queued" and self._is_gpu(j) for j in list(self.jobs.values()))

    def running(self):
        """割り当て済みで終わっていないジョブ(準備中と実行中)。始めた順。"""
        return [self.jobs[jid] for jid in list(self._tasks) if jid in self.jobs]

    def lane_status(self) -> dict:
        """レーンごとの、走っているジョブと順番待ちの数(画面の状態バー用)。"""
        lanes = {(h.component or CPU_LANE): {"running": [], "queued": 0, "running_jobs": []} for h in self.handlers.values()}
        for j in list(self.jobs.values()):
            lane = lanes.setdefault(j.lane or CPU_LANE, {"running": [], "queued": 0, "running_jobs": []})
            if j.id in self._tasks:
                lane["running"].append(j.id)
                lane["running_jobs"].append({"id": j.id, "tab": j.tab, "kind": j.kind, "status": j.status,
                                             "progress": j.progress, "message": j.message})
            elif j.status == "queued":
                lane["queued"] += 1
        return lanes

    # ---- 受付 ----

    def submit(self, tab: str, kind: str, params: dict) -> Job:
        handler = self.handlers.get(kind)
        if handler is None:
            raise ValueError(f"未対応のジョブの種類です: {kind}")
        comps = getattr(self.ctx.sched, "comps", None)
        if handler.component and isinstance(comps, dict) and handler.component not in comps:
            # studio.toml で無効にした部品のジョブは受け付けない(受け付けると、載せる段で KeyError になる)
            label = COMPONENT_NAMES.get(handler.component, handler.component)
            raise ValueError(f"{label}の部品は無効になっているため使えません(studio.toml の [components.{handler.component}] の enabled)")
        job = Job(id=uuid.uuid4().hex[:12], tab=tab, kind=kind, params=handler.normalize(kind, params, self.ctx.ledger),
                  lane=handler.component or CPU_LANE)
        self.jobs[job.id] = job
        self._trim()
        self.wake()
        return job

    def _trim(self) -> None:
        for jid in [j.id for j in self.jobs.values() if j.status in TERMINAL][:max(0, len(self.jobs) - KEEP_JOBS)]:
            del self.jobs[jid]

    def get(self, job_id: str) -> Optional[Job]:
        return self.jobs.get(job_id)

    def list(self, tab: str = None, limit: int = 50) -> list:
        """新しい順。メモリにあるものを優先し、足りなければ台帳(過去のジョブ)で埋める。"""
        mem = sorted((j for j in self.jobs.values() if not tab or j.tab == tab), key=lambda j: j.created, reverse=True)
        out = [j.to_dict() for j in mem[:limit]]
        if len(out) < limit:
            seen = {d["id"] for d in out}
            out += [d for d in self.ctx.ledger.list_jobs(tab, limit) if d["id"] not in seen][:limit - len(out)]
        return out

    async def cancel(self, job_id: str) -> Optional[Job]:
        job = self.jobs.get(job_id)
        if job is None or job.status in TERMINAL:
            return job
        if job.status == "queued":
            self._finish(job, "cancelled", message="中止しました")
            self.wake()          # 空き待ちのジョブが消えると、後ろの仕事が進められるかもしれない
            return job
        job.cancel_requested = True
        job.message = "中止しています"
        if job.status == "running":
            try:
                await self.handlers[job.kind].cancel(job, self.ctx)
            except Exception:  # noqa: BLE001  中止の失敗で API を落とさない(ハンドラの監視ループが再送する)
                log.exception("中止の送信に失敗")
        return job

    # ---- 割り当て ----

    async def _dispatch_loop(self) -> None:
        while not self._stopping:
            self._wake.clear()
            try:
                self._dispatch()
            except Exception:  # noqa: BLE001  割り当ての失敗で、走っているジョブまで止めない
                log.exception("ジョブの割り当てに失敗")
            try:
                async with asyncio.timeout(DISPATCH_POLL_S):      # wait_for は、通知と同時の中止(stop)を握りつぶすことがある(3.11)
                    await self._wake.wait()
            except TimeoutError:
                pass

    def _dispatch(self) -> None:
        """受け付けた順に見て、始められるものから始める(後ろのジョブが、空いているレーンで先に走ってよい)。

        始める条件: そのレーンが空いていて(部品ごとに 1 つ、CPU は cpu_slots 本)、GPU の部品は待たずに載せられること。
        GPU の空き待ちのジョブがあれば、それより後ろの仕事は、そのジョブが降ろしたい部品に入れない
        (後から来た仕事に追い越され続けないように)。すでに載っていて、降ろされない部品の仕事は、そのまま始めてよい。
        """
        plan_of = getattr(self.ctx.sched, "plan", None)
        load = Counter(self.jobs[jid].lane for jid in self._tasks if jid in self.jobs)
        blocked = set().union(*self._reserved.values())       # 載せている最中のジョブが降ろす部品
        waiting = False                                       # GPU の空きを待っているジョブが、前に居る
        for job in list(self.jobs.values()):
            handler = self.handlers.get(job.kind)
            if job.status != "queued" or handler is None:
                continue
            if load[job.lane] >= (self.cpu_slots if job.lane == CPU_LANE else 1):
                self._wait(job, WAIT_ORDER)
                continue
            plan = None
            if plan_of is not None and handler.component and handler.gpu:
                try:
                    plan = plan_of(handler.component)
                except Exception:  # noqa: BLE001  調べられなくても始める(ensure が、分かる言葉で失敗させる)
                    log.exception("載せる見通しを調べられません: %s", handler.component)
                if plan is not None and plan.action == "wait":
                    self._wait(job, plan.reason)
                    blocked |= plan.needs
                    waiting = True
                    continue
                if plan is not None and (handler.component in blocked or (waiting and plan.action != "ready")):
                    self._wait(job, WAIT_ORDER)
                    continue
            if plan is not None:
                blocked |= set(plan.victims)
            self._start(job, handler, plan)
            load[job.lane] += 1

    @staticmethod
    def _wait(job: Job, reason: str) -> None:
        job.wait_reason = job.message = reason           # 画面は message を出すので、待ちの理由もそこに写す

    def _start(self, job: Job, handler: Handler, plan) -> None:
        job.status, job.message, job.wait_reason = "preparing", WAIT_PREPARE, WAIT_PREPARE
        job.started = time.time()
        if handler.component and handler.gpu:
            mark = getattr(self.ctx.sched, "mark_busy", None)
            if mark is not None:
                mark(handler.component)               # 仕事中の部品は降ろされず、予算も押さえたままになる
        if plan is not None and plan.victims:
            self._reserved[job.id] = set(plan.victims)
        self._tasks[job.id] = asyncio.create_task(self._execute(job, handler), name=f"job-{job.id}")
        self.current = job

    def _finish(self, job: Job, status: str, message: str, error: str = None) -> None:
        job.status, job.message, job.error, job.finished = status, message, error, time.time()
        job.wait_reason = None
        if status == "completed":
            job.progress = 1.0
        self.done_counter += 1
        try:
            handler = self.handlers.get(job.kind)
            self.ctx.ledger.save_job(job, handler.record(job.params) if handler is not None and handler.record else None)
        except Exception:  # noqa: BLE001
            log.exception("ジョブを台帳に保存できません")
        if self._finished_hooks and not self._stopping:
            asyncio.get_running_loop().call_soon(self._notify_finished, job)      # 後片づけ(_execute の finally)が済んでから呼ぶ

    async def _execute(self, job: Job, handler: Handler) -> None:
        try:
            if handler.prepare:       # LLM を使う下ごしらえは、GPU に部品を載せる前に済ませる
                await handler.prepare(job, self.ctx)
                if job.cancel_requested:
                    raise JobCancelled()
            if handler.gpu:
                self._gpu_claimed.add(job.id)
                if self.ctx.llm is not None:
                    await self.ctx.llm.release_before_gpu()
            if handler.component:
                await self._prepare(job, handler.component)
                if job.cancel_requested:
                    raise JobCancelled()
            if self._reserved.pop(job.id, None):
                self.wake()           # 載せ終わった: 降ろす予定で待たせていた部品の仕事も始められる
            job.status, job.message, job.wait_reason = "running", "生成を始めます", None
            await handler.run(job, self.ctx)
            self._finish(job, "completed", "完了しました")
        except JobCancelled:
            self._finish(job, "cancelled", "中止しました")
        except SwitchError as e:
            job.log_tail = e.log_tail
            self._finish(job, "failed", "失敗しました", e.message)
        except JobFailed as e:
            job.log_tail = e.log_tail
            self._finish(job, "failed", "失敗しました", e.message)
        except asyncio.CancelledError:
            self._finish(job, "cancelled", "中止しました(管理役を停止)")
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("ジョブ %s が失敗", job.id)
            self._finish(job, "failed", "失敗しました", f"{type(e).__name__}: {e}")
        finally:
            self._gpu_claimed.discard(job.id)
            self._reserved.pop(job.id, None)
            self._tasks.pop(job.id, None)
            if handler.component and handler.gpu:
                idle = getattr(self.ctx.sched, "mark_idle", None)
                if idle is not None:
                    idle(handler.component)
            if self.current is job:
                still = self.running()
                self.current = still[-1] if still else None
            self.wake()

    async def _prepare(self, job: Job, component: str) -> None:
        """scheduler.ensure を走らせ、その経過(Operation の steps)をジョブの message に写す。"""
        op = self.ctx.sched.new_operation("ensure", component)
        task = asyncio.create_task(self.ctx.sched.ensure(component, op))

        def sync() -> None:
            job.steps = [s["msg"] for s in op.steps]
            if job.steps:
                job.message = job.steps[-1]
            job.wait_reason = getattr(op, "wait_reason", None) or WAIT_PREPARE

        try:
            while not task.done():
                await asyncio.wait({task}, timeout=self.step_poll_s)
                sync()
                if job.cancel_requested and getattr(op, "wait_reason", None) and not task.done():
                    task.cancel()         # GPU の空き待ちの間なら、止めても何も残らない
                    await asyncio.gather(task, return_exceptions=True)
                    raise JobCancelled()
            sync()
            task.result()
        except JobCancelled:
            raise
        except SwitchError as e:
            op.error, op.log_tail = e.message, e.log_tail
            op.add(f"失敗: {e.message}")
            raise
        except asyncio.CancelledError:
            task.cancel()
            raise
        except Exception as e:  # noqa: BLE001
            op.error = f"{type(e).__name__}: {e}"
            raise
        finally:
            op.done = True
