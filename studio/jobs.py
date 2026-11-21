"""スタジオ共通のジョブ実行: キューと worker 1 本(第 1 版は同時実行しない)。

ジョブは「部品を ensure(必要なら載せ替え)→ 種類ごとのハンドラが部品の API を呼ぶ」の順に流れる。
"""
import asyncio
import logging
import time
import uuid
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional

from .ledger import Ledger
from .scheduler import Scheduler, SwitchError

log = logging.getLogger("studio.jobs")

KEEP_JOBS = 200
TERMINAL = ("completed", "failed", "cancelled")


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
    tab: str                      # image / video / music
    kind: str                     # 例: h3image.t2i
    params: dict
    status: str = "queued"        # queued / preparing / running / completed / failed / cancelled
    progress: float = 0.0
    message: str = "順番を待っています"
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


@dataclass
class Handler:
    component: str                                            # ensure する部品名
    normalize: Callable                                       # (kind, params, ledger) -> params(検証・整形済み)。不正なら ValueError
    run: Callable[[Job, Ctx], Awaitable[None]]
    cancel: Callable[[Job, Ctx], Awaitable[None]]
    prepare: Optional[Callable[[Job, Ctx], Awaitable[None]]] = None   # 部品を ensure する前の下ごしらえ(LLM の仕事など)
    gpu: bool = True                                                    # GPU を使う(LLM を止めてから ensure する)


class JobRunner:
    def __init__(self, ctx: Ctx, handlers: dict, step_poll_s: float = 0.3):
        self.ctx, self.handlers, self.step_poll_s = ctx, handlers, step_poll_s
        self.jobs: OrderedDict[str, Job] = OrderedDict()
        self.current: Optional[Job] = None
        self.done_counter = 0                  # 終わったジョブの数(画面が、自分の知らないジョブの完了に気づく目印)
        self._gpu_claimed = False
        self._queue: Optional[asyncio.Queue] = None
        self._worker: Optional[asyncio.Task] = None

    # ---- 起動と停止 ----

    def start(self) -> None:
        self._queue = asyncio.Queue()
        self._worker = asyncio.create_task(self._loop(), name="job-runner")

    async def stop(self) -> None:
        if self._worker:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None

    def is_generating(self) -> bool:
        """GPU で生成している最中か(共有 GPU メモリの警告の判定に使う)。"""
        job = self.current
        return job is not None and job.status == "running"

    def gpu_busy(self) -> bool:
        """GPU を使うジョブが、LLM を止めてから終わるまでの間か(この間は LLM を起こさない)。"""
        return self._gpu_claimed

    # ---- 受付 ----

    def submit(self, tab: str, kind: str, params: dict) -> Job:
        handler = self.handlers.get(kind)
        if handler is None:
            raise ValueError(f"未対応のジョブの種類です: {kind}")
        job = Job(id=uuid.uuid4().hex[:12], tab=tab, kind=kind, params=handler.normalize(kind, params, self.ctx.ledger))
        self.jobs[job.id] = job
        self._trim()
        self._queue.put_nowait(job)
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
            return job
        job.cancel_requested = True
        job.message = "中止しています"
        if job.status == "running":
            try:
                await self.handlers[job.kind].cancel(job, self.ctx)
            except Exception:  # noqa: BLE001  中止の失敗で API を落とさない(ハンドラの監視ループが再送する)
                log.exception("中止の送信に失敗")
        return job

    # ---- worker ----

    async def _loop(self) -> None:
        while True:
            job = await self._queue.get()
            if job.status == "queued":
                await self._execute(job)

    def _finish(self, job: Job, status: str, message: str, error: str = None) -> None:
        job.status, job.message, job.error, job.finished = status, message, error, time.time()
        if status == "completed":
            job.progress = 1.0
        self.done_counter += 1
        try:
            self.ctx.ledger.save_job(job)
        except Exception:  # noqa: BLE001
            log.exception("ジョブを台帳に保存できません")

    async def _execute(self, job: Job) -> None:
        handler = self.handlers[job.kind]
        self.current = job
        job.started = time.time()
        try:
            job.status, job.message = "preparing", "部品を準備しています"
            if handler.prepare:       # LLM を使う下ごしらえは、GPU に部品を載せる前に済ませる
                await handler.prepare(job, self.ctx)
                if job.cancel_requested:
                    raise JobCancelled()
            if handler.gpu:
                self._gpu_claimed = True
                if self.ctx.llm is not None:
                    await self.ctx.llm.release_before_gpu()
            await self._prepare(job, handler.component)
            if job.cancel_requested:
                raise JobCancelled()
            job.status, job.message = "running", "生成を始めます"
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
            self.current = None
            self._gpu_claimed = False

    async def _prepare(self, job: Job, component: str) -> None:
        """scheduler.ensure を走らせ、その経過(Operation の steps)をジョブの message に写す。"""
        op = self.ctx.sched.new_operation("ensure", component)
        task = asyncio.create_task(self.ctx.sched.ensure(component, op))

        def sync() -> None:
            job.steps = [s["msg"] for s in op.steps]
            if job.steps:
                job.message = job.steps[-1]

        try:
            while not task.done():
                await asyncio.wait({task}, timeout=self.step_poll_s)
                sync()
            sync()
            task.result()
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
