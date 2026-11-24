import asyncio
import tempfile
import types
import unittest
from pathlib import Path

from studio.jobs import Ctx, Handler, JobCancelled, JobFailed, JobRunner
from studio.ledger import Ledger
from studio.scheduler import Operation, SwitchError


class FakeSched:
    """ensure の代わり。steps を 1 つずつ足し、fail があれば SwitchError にする。"""

    def __init__(self, fail: str = None, hold: asyncio.Event = None):
        self.fail, self.hold, self.ensured, self.ops = fail, hold, [], []

    def new_operation(self, kind, name):
        op = Operation(kind, name)
        self.ops.append(op)
        return op

    async def ensure(self, name, op=None):
        self.ensured.append(name)
        op.add("MiniMax-H3-Image を起動しています")
        await asyncio.sleep(0.05)
        if self.hold:
            await self.hold.wait()
        if self.fail:
            raise SwitchError(self.fail, "log line")
        op.add("準備完了")


def make_handler(log: list, run=None, **kw):
    async def default_run(job, ctx):
        log.append(("run", job.status))
        job.progress = 0.5
        job.message = "生成中 50%"

    async def cancel(job, ctx):
        log.append(("cancel", job.id))
    return Handler("h3image", lambda kind, params, ledger: dict(params), run or default_run, cancel, **kw)


class JobsBase(unittest.IsolatedAsyncioTestCase):
    def make(self, sched, handler, llm=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ledger = Ledger(Path(tmp.name))
        self.addCleanup(ledger.close)
        runner = JobRunner(Ctx(sched, ledger, Path(tmp.name), llm=llm), {"h3image.t2i": handler}, step_poll_s=0.01)
        runner.start()
        self.addAsyncCleanup(runner.stop)
        return runner, ledger

    async def wait_for(self, job, *statuses, timeout=3.0):
        for _ in range(int(timeout / 0.01)):
            if job.status in statuses:
                return
            await asyncio.sleep(0.01)
        self.fail(f"状態が {statuses} にならない: {job.status}")


class JobsTest(JobsBase):

    async def test_happy_path(self):
        log, hold = [], asyncio.Event()
        sched = FakeSched(hold=hold)
        runner, ledger = self.make(sched, make_handler(log))
        job = runner.submit("image", "h3image.t2i", {"prompt": "x"})
        self.assertEqual(job.status, "queued")
        await self.wait_for(job, "preparing")
        await asyncio.sleep(0.1)
        self.assertEqual(job.message, "MiniMax-H3-Image を起動しています")   # ensure の経過が message に写る
        self.assertFalse(runner.is_generating())
        hold.set()
        await self.wait_for(job, "completed")
        self.assertEqual(sched.ensured, ["h3image"])
        self.assertEqual(log, [("run", "running")])
        self.assertEqual(job.steps, ["MiniMax-H3-Image を起動しています", "準備完了"])
        self.assertEqual((job.progress, job.error), (1.0, None))
        self.assertTrue(sched.ops[0].done)
        self.assertFalse(runner.is_generating())
        self.assertEqual(ledger.get_job(job.id)["status"], "completed")   # 終わったジョブは台帳へ
        self.assertEqual(runner.list("image")[0]["id"], job.id)

    async def test_is_generating_while_running(self):
        gate = asyncio.Event()

        async def slow(job, ctx):
            await gate.wait()
        runner, _ = self.make(FakeSched(), make_handler([], slow))
        job = runner.submit("image", "h3image.t2i", {})
        await self.wait_for(job, "running")
        self.assertTrue(runner.is_generating())
        gate.set()
        await self.wait_for(job, "completed")
        self.assertFalse(runner.is_generating())

    async def test_switch_error_fails_job(self):
        log = []
        sched = FakeSched(fail="GPU が空きません")
        runner, ledger = self.make(sched, make_handler(log))
        job = runner.submit("image", "h3image.t2i", {})
        await self.wait_for(job, "failed")
        self.assertEqual((job.error, job.log_tail), ("GPU が空きません", "log line"))
        self.assertEqual(log, [])   # ハンドラは呼ばれない
        self.assertEqual(sched.ops[0].error, "GPU が空きません")
        self.assertTrue(sched.ops[0].done)
        self.assertEqual(ledger.get_job(job.id)["error"], "GPU が空きません")

    async def test_handler_errors(self):
        async def boom(job, ctx):
            raise JobFailed("部品が失敗")
        runner, _ = self.make(FakeSched(), make_handler([], boom))
        job = runner.submit("image", "h3image.t2i", {})
        await self.wait_for(job, "failed")
        self.assertEqual(job.error, "部品が失敗")

        async def cancelled(job, ctx):
            raise JobCancelled()
        runner.handlers["h3image.t2i"] = make_handler([], cancelled)
        job = runner.submit("image", "h3image.t2i", {})
        await self.wait_for(job, "cancelled")

    async def test_cancel_queued(self):
        hold = asyncio.Event()
        runner, ledger = self.make(FakeSched(hold=hold), make_handler([]))
        first = runner.submit("image", "h3image.t2i", {})
        second = runner.submit("image", "h3image.t2i", {})
        await self.wait_for(first, "preparing")
        self.assertEqual(second.status, "queued")
        await runner.cancel(second.id)
        self.assertEqual(second.status, "cancelled")
        hold.set()
        await self.wait_for(first, "completed")
        await asyncio.sleep(0.05)
        self.assertEqual(second.status, "cancelled")   # 取り消したジョブは走らない
        self.assertEqual(ledger.get_job(second.id)["status"], "cancelled")

    async def test_cancel_running_calls_handler(self):
        log, gate = [], asyncio.Event()

        async def run(job, ctx):
            await gate.wait()
            if job.cancel_requested:
                raise JobCancelled()
        runner, _ = self.make(FakeSched(), make_handler(log, run))
        job = runner.submit("image", "h3image.t2i", {})
        await self.wait_for(job, "running")
        await runner.cancel(job.id)
        self.assertEqual(log, [("cancel", job.id)])
        gate.set()
        await self.wait_for(job, "cancelled")

    async def test_unknown_kind_and_list_merges_ledger(self):
        runner, ledger = self.make(FakeSched(), make_handler([]))
        with self.assertRaises(ValueError):
            runner.submit("image", "nope", {})
        job = runner.submit("image", "h3image.t2i", {})
        await self.wait_for(job, "completed")
        runner.jobs.clear()   # 再起動の代わり: メモリを空にしても台帳から出てくる
        self.assertEqual([j["id"] for j in runner.list("image")], [job.id])


class OrderTest(JobsBase):
    """prepare(LLM の仕事)→ LLM を止める → 部品を ensure → run の順。"""

    def setUp(self):
        self.order = []
        order = self.order
        self.llm = types.SimpleNamespace()

        async def release():
            order.append(("release", self.runner.gpu_busy()))
        self.llm.release_before_gpu = release

        class Sched(FakeSched):
            async def ensure(inner, name, op=None):
                order.append(("ensure", self.runner.gpu_busy()))
                await super().ensure(name, op)
        self.sched = Sched()

    def build(self, run=None, prepare=None, **kw):
        async def default_prepare(job, ctx):
            self.order.append(("prepare", self.runner.gpu_busy()))
            job.message = "歌詞を作っています"

        async def default_run(job, ctx):
            self.order.append(("run", self.runner.gpu_busy()))
        self.runner, _ = self.make(self.sched, make_handler([], run or default_run, prepare=prepare or default_prepare, **kw), self.llm)
        return self.runner

    async def test_prepare_runs_before_release_and_ensure(self):
        runner = self.build()
        job = runner.submit("image", "h3image.t2i", {})
        await self.wait_for(job, "completed")
        # GPU を占める印(gpu_busy)は、prepare の間は立たず、LLM を止める前から run の終わりまで立つ
        self.assertEqual(self.order, [("prepare", False), ("release", True), ("ensure", True), ("run", True)])
        self.assertFalse(runner.gpu_busy())
        self.assertEqual(runner.done_counter, 1)

    async def test_prepare_failure_skips_ensure(self):
        async def bad(job, ctx):
            raise JobFailed("歌詞を作れませんでした")
        runner = self.build(prepare=bad)
        job = runner.submit("image", "h3image.t2i", {})
        await self.wait_for(job, "failed")
        self.assertEqual((job.error, self.sched.ensured, self.order), ("歌詞を作れませんでした", [], []))
        self.assertFalse(runner.gpu_busy())
        self.assertEqual(runner.done_counter, 1)

    async def test_cancel_during_prepare(self):
        gate = asyncio.Event()

        async def slow(job, ctx):
            await gate.wait()
        runner = self.build(prepare=slow)
        job = runner.submit("image", "h3image.t2i", {})
        await self.wait_for(job, "preparing")
        await runner.cancel(job.id)
        gate.set()
        await self.wait_for(job, "cancelled")
        self.assertEqual((self.sched.ensured, self.order), ([], []))

    async def test_non_gpu_handler_leaves_llm_alone(self):
        runner = self.build(gpu=False)
        job = runner.submit("image", "h3image.t2i", {})
        await self.wait_for(job, "completed")
        self.assertEqual(self.order, [("prepare", False), ("ensure", False), ("run", False)])


class FinishedHookTest(JobsBase):
    """on_finished: ジョブが終わる(完了・失敗・中止)たびに、後片づけのあとで callback(job) が呼ばれる。"""

    def hooked(self, handler):
        runner, ledger = self.make(FakeSched(), handler)
        seen = []
        runner.on_finished(lambda job: seen.append((job.id, job.status, [j.id for j in runner.running()], runner.current)))
        return runner, seen

    async def test_called_once_per_job_after_the_lane_is_freed(self):
        runner, seen = self.hooked(make_handler([]))
        job = runner.submit("image", "h3image.t2i", {})
        await self.wait_for(job, "completed")
        await asyncio.sleep(0.05)
        self.assertEqual(seen, [(job.id, "completed", [], None)])     # 呼ばれたとき、もうレーンは空き、実行中のジョブは無い

    async def test_failed_and_cancelled_jobs_are_reported_too(self):
        async def boom(job, ctx):
            raise JobFailed("部品が失敗")
        runner, seen = self.hooked(make_handler([], boom))
        failed = runner.submit("image", "h3image.t2i", {})
        await self.wait_for(failed, "failed")

        gate = asyncio.Event()

        async def hold(job, ctx):
            await gate.wait()
            if job.cancel_requested:
                raise JobCancelled()
        runner.handlers["h3image.t2i"] = make_handler([], hold)
        running, queued = runner.submit("image", "h3image.t2i", {}), runner.submit("image", "h3image.t2i", {})
        await self.wait_for(running, "running")
        await runner.cancel(queued.id)            # 順番待ちのまま中止
        await runner.cancel(running.id)           # 走っているものを中止
        gate.set()
        await self.wait_for(running, "cancelled")
        await asyncio.sleep(0.05)
        self.assertEqual({(i, st) for i, st, _, _ in seen}, {(failed.id, "failed"), (queued.id, "cancelled"), (running.id, "cancelled")})
        self.assertEqual(len(seen), 3)

    async def test_callback_may_submit_the_next_job(self):
        """MV の管理役のように、終わった通知の中で次のジョブを入れても、そのまま割り当てられて走る。"""
        order = []

        async def run(job, ctx):
            order.append(job.params["n"])
        runner, _ = self.make(FakeSched(), Handler("h3image", lambda kind, params, ledger: dict(params), run, lambda job, ctx: None))
        jobs = []

        def chain(job):
            if job.params["n"] < 3:
                jobs.append(runner.submit("image", "h3image.t2i", {"n": job.params["n"] + 1}))
        runner.on_finished(chain)
        first = runner.submit("image", "h3image.t2i", {"n": 1})
        for _ in range(300):
            if len(jobs) == 2 and all(j.status == "completed" for j in jobs):
                break
            await asyncio.sleep(0.01)
        self.assertEqual((first.status, order), ("completed", [1, 2, 3]))

    async def test_failing_callback_does_not_break_the_runner_or_other_callbacks(self):
        runner, _ = self.make(FakeSched(), make_handler([]))
        got = []

        def bad(job):
            raise RuntimeError("通知先の失敗")
        runner.on_finished(bad)
        runner.on_finished(lambda job: got.append(job.id))
        with self.assertLogs("studio.jobs", "ERROR"):
            first = runner.submit("image", "h3image.t2i", {})
            await self.wait_for(first, "completed")
            await asyncio.sleep(0.05)
            second = runner.submit("image", "h3image.t2i", {})      # その後も、ふつうに走る
            await self.wait_for(second, "completed")
            await asyncio.sleep(0.05)
        self.assertEqual(got, [first.id, second.id])

    async def test_not_called_while_stopping(self):
        gate = asyncio.Event()

        async def hold(job, ctx):
            await gate.wait()
        runner, seen = self.hooked(make_handler([], hold))
        job = runner.submit("image", "h3image.t2i", {})
        await self.wait_for(job, "running")
        await runner.stop()
        await asyncio.sleep(0.05)
        self.assertEqual((job.status, seen), ("cancelled", []))      # 管理役の停止では、通知しない(台帳が閉じられる途中で呼ばれないように)


if __name__ == "__main__":
    unittest.main()
