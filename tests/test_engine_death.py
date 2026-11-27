"""エンジンの死の見分けと 1 回のやり直し、RAM の少ない PC での CPU の手伝いの停止、同梱 LLM の欄、edit の出力のプロンプト(2026-11-26、Windows の実機の報告)。"""
import asyncio
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import httpx

from studio import jobs as J
from studio import llm as llm_mod
from studio import mv
from studio.config import REPO_ROOT, Settings
from studio.jobs import Ctx, EngineDied, Handler, JobFailed, JobRunner
from studio.ledger import Ledger
from studio.scheduler import Operation
from tests import test_scheduler as TS
from tests.test_short import png_bytes


class DyingSched:
    """ensure の代わり。dead が True の間は engine_died が True(ensure で生き返る = 起動し直した)。"""

    def __init__(self):
        self.ensured, self.dead, self.died_at = [], False, None
        self.stops = []
        self.comps = {"h3": types.SimpleNamespace(spec=types.SimpleNamespace(label="動画(MiniMax-H3)"))}

    def new_operation(self, kind, name):
        return Operation(kind, name)

    async def ensure(self, name, op=None):
        self.ensured.append(name)
        self.dead = False
        await asyncio.sleep(0)

    def engine_died(self, name, since):
        return self.dead

    async def stop(self, name, op=None):
        self.stops.append(name)


class RunnerBase(unittest.IsolatedAsyncioTestCase):
    def make(self, sched, run, component="h3", **ctx):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ledger = Ledger(Path(tmp.name))
        self.addCleanup(ledger.close)

        async def cancel(job, ctx):
            pass
        handler = Handler(component, lambda kind, params, ledger: dict(params), run, cancel)
        runner = JobRunner(Ctx(sched, ledger, Path(tmp.name), **ctx), {"h3.t2va": handler}, step_poll_s=0.01)
        runner.start()
        self.addAsyncCleanup(runner.stop)
        return runner

    async def wait_done(self, job, timeout=3.0):
        for _ in range(int(timeout / 0.01)):
            if job.status in J.TERMINAL:
                return
            await asyncio.sleep(0.01)
        self.fail(f"終わらない: {job.status}")


class RetryTest(RunnerBase):
    def setUp(self):
        p = mock.patch.object(J.procman, "log_tail", lambda name, lines=100: "a\nLoading checkpoint shards: 40%\n\nb")
        p.start()
        self.addCleanup(p.stop)

    async def test_a_dead_engine_is_restarted_and_the_job_runs_once_more(self):
        sched, calls = DyingSched(), []

        async def run(job, ctx):
            calls.append(job.status)
            if len(calls) == 1:
                job.progress = 0.4
                sched.dead = True
                raise JobFailed("H3 と通信できません: ConnectError: refused")
            job.progress = 0.9
        job = self.make(sched, run).submit("video", "h3.t2va", {})
        await self.wait_done(job)
        self.assertEqual((job.status, calls, sched.ensured), ("completed", ["running", "running"], ["h3", "h3"]))
        self.assertIn("Loading checkpoint shards", job.log_tail)      # 1 回目の死のログは残す

    async def test_a_second_death_fails_with_the_died_message(self):
        sched, calls = DyingSched(), []

        async def run(job, ctx):
            calls.append(1)
            sched.dead = True
            raise httpx.ConnectError("refused")
        job = self.make(sched, run).submit("video", "h3.t2va", {})
        await self.wait_done(job)
        self.assertEqual((job.status, len(calls)), ("failed", 2))
        self.assertEqual(job.error, "動画(MiniMax-H3) が終了していました(RAM 不足の可能性があります)。ログの最後: a / Loading checkpoint shards: 40% / b")
        self.assertIn("Loading checkpoint shards", job.log_tail)

    async def test_a_live_engine_keeps_the_original_error(self):
        sched = DyingSched()

        async def run(job, ctx):
            raise JobFailed("H3 が動画を返しませんでした")
        job = self.make(sched, run).submit("video", "h3.t2va", {})
        await self.wait_done(job)
        self.assertEqual((job.status, job.error, sched.ensured), ("failed", "H3 が動画を返しませんでした", ["h3"]))

    async def test_cancel_during_the_death_is_not_retried(self):
        sched = DyingSched()

        async def run(job, ctx):
            job.cancel_requested = True
            sched.dead = True
            raise JobFailed("x")
        job = self.make(sched, run).submit("video", "h3.t2va", {})
        await self.wait_done(job)
        self.assertEqual((job.status, sched.ensured), ("failed", ["h3"]))
        self.assertIn("終了していました", job.error)

    async def test_cancel_while_restarting_cancels(self):
        sched, calls = DyingSched(), []
        real_ensure = sched.ensure

        async def ensure(name, op=None):
            await real_ensure(name, op)
            if len(sched.ensured) == 2:
                job.cancel_requested = True       # 起動し直している間に中止が来た
        sched.ensure = ensure

        async def run(job, ctx):
            calls.append(1)
            sched.dead = True
            raise JobFailed("x")
        job = self.make(sched, run).submit("video", "h3.t2va", {})
        await self.wait_done(job)
        self.assertEqual((job.status, len(calls)), ("cancelled", 1))

    async def test_a_death_seen_late_is_still_retried(self):
        # 2026-11-26 の実機: エンジンを止めた直後は、ソケットだけ閉じてプロセスはまだ生きて見えた(OS の片づけに数秒かかる)
        sched, calls, seen = DyingSched(), [], []
        sched.engine_died = lambda name, since: seen.append(1) or (len(seen) >= 4 and len(calls) == 1)

        async def run(job, ctx):
            calls.append(1)
            if len(calls) == 1:
                try:
                    raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
                except httpx.HTTPError as e:
                    raise JobFailed(f"H3 と通信できません: {type(e).__name__}: {e}")
        with mock.patch.object(J, "DEATH_POLL_S", 0.01):
            job = self.make(sched, run).submit("video", "h3.t2va", {})
            await self.wait_done(job)
        self.assertEqual((job.status, len(calls), len(seen), sched.ensured), ("completed", 2, 4, ["h3", "h3"]))

    async def test_waiting_gives_up_and_keeps_the_original_error(self):
        sched, seen = DyingSched(), []
        sched.engine_died = lambda name, since: seen.append(1) or False

        async def run(job, ctx):
            try:
                raise httpx.ReadError("reset")
            except httpx.HTTPError as e:
                raise JobFailed("H3 と通信できません: ReadError: reset") from e
        with mock.patch.object(J, "DEATH_POLL_S", 0.01), mock.patch.object(J, "DEATH_GRACE_S", 0.1):
            job = self.make(sched, run).submit("video", "h3.t2va", {})
            await self.wait_done(job)
        self.assertEqual((job.status, job.error, sched.ensured), ("failed", "H3 と通信できません: ReadError: reset", ["h3"]))
        self.assertGreater(len(seen), 2)

    async def test_other_errors_and_unwatchable_engines_are_checked_once(self):
        for answers, error in (([False, True], JobFailed("H3 が動画を返しませんでした")),       # 通信の途切れではない
                               ([None, True], httpx.ConnectError("refused"))):               # 外部のエンジン(見張れない)
            sched, seen = DyingSched(), []
            sched.engine_died = lambda name, since, a=answers: a[len(seen) - 1] if seen.append(1) is None else None

            async def run(job, ctx, error=error):
                raise error
            with mock.patch.object(J, "DEATH_POLL_S", 0.01):
                job = self.make(sched, run).submit("video", "h3.t2va", {})
                await self.wait_done(job)
            self.assertEqual((job.status, len(seen), sched.ensured), ("failed", 1, ["h3"]))

    def test_transport_error_follows_the_chain(self):
        try:
            try:
                raise httpx.ConnectError("refused")
            except httpx.HTTPError:
                raise JobFailed("H3 と通信できません")
        except JobFailed as e:
            self.assertTrue(J.transport_error(e))
        self.assertTrue(J.transport_error(httpx.RemoteProtocolError("x")))
        self.assertFalse(J.transport_error(JobFailed("x")))
        self.assertFalse(J.transport_error(httpx.HTTPStatusError("x", request=None, response=None)))
        self.assertFalse(J.transport_error(None))

    def test_english(self):
        from studio.i18n import tr
        self.assertEqual(tr(J.died_message("動画(MiniMax-H3)", "x"), "en"), "Video (MiniMax-H3) had exited (possibly out of RAM). Last log lines: x")


class SchedulerDeathTest(unittest.IsolatedAsyncioTestCase):
    make = TS.SchedulerTest.make

    async def test_engine_died_sees_a_death_found_earlier(self):
        fake = TS.Fake()
        s = self.make(fake)
        await s.ensure("h3")
        since = J.time.time()
        self.assertIs(s.engine_died("h3", since), False)
        s.comps["h3"].proc.popen.poll = lambda: 1
        with mock.patch.object(J.procman, "is_alive", lambda p: False):
            s.plan("h3image")                  # 割り当ての見直しが先に見つけても
            self.assertEqual(s.comps["h3"].state, "stopped")
            self.assertIs(s.engine_died("h3", since), True)
            self.assertIsNone(s.engine_died("h3", J.time.time() + 1))     # もう見張るプロセスが無い
        self.assertIsNone(s.engine_died("nope", since))
        s.comps["ace"].proc, s.comps["ace"].external = object(), True
        self.assertIsNone(s.engine_died("ace", since))                    # 外部のエンジンは見張れない


class FreeCpuTest(RunnerBase):
    def test_rule(self):
        gib = 1024 ** 3
        self.assertTrue(J.should_free_cpu("auto", 16 * gib))
        self.assertFalse(J.should_free_cpu("auto", 32 * gib))
        self.assertFalse(J.should_free_cpu("auto", None))
        self.assertTrue(J.should_free_cpu("always", 128 * gib))
        self.assertFalse(J.should_free_cpu("never", 8 * gib))
        self.assertTrue(J.should_free_cpu("bogus", 8 * gib))
        self.assertEqual(Settings({"gpu": {"free_cpu_engines": "Never"}}, REPO_ROOT).free_cpu_engines, "never")
        self.assertEqual(Settings({}, REPO_ROOT).free_cpu_engines, "auto")
        self.assertEqual(Settings({"gpu": {"stop_on_switch": "Always"}}, REPO_ROOT).stop_on_switch, "always")
        self.assertEqual(Settings({}, REPO_ROOT).stop_on_switch, "auto")

    def sched_with_aivis(self, **aivis):
        sched = DyingSched()
        sched.comps["aivis"] = types.SimpleNamespace(proc=object(), external=False, state="ready", **aivis)
        return sched

    async def run_with(self, sched, ram, setting="auto", tts=None, llm=None):
        seen = []

        async def run(job, ctx):
            seen.append(job.message)
        with mock.patch.object(J, "total_ram_bytes", lambda: ram):
            job = self.make(sched, run, free_cpu_engines=setting, tts=tts, llm=llm).submit("video", "h3.t2va", {})
            await self.wait_done(job)
        return job

    def fake_llm(self, idle=True):
        llm = types.SimpleNamespace(stopped=0)

        async def release_before_gpu():
            return False

        async def stop():
            llm.stopped += 1
            return True
        llm.release_before_gpu, llm.stop, llm.idle_stoppable = release_before_gpu, stop, lambda: idle
        return llm

    async def test_low_ram_stops_idle_aivis_and_llm(self):
        sched, llm = self.sched_with_aivis(), self.fake_llm()
        tts = types.SimpleNamespace(in_use=lambda: False)
        job = await self.run_with(sched, 16 * 1024 ** 3, tts=tts, llm=llm)
        self.assertEqual((job.status, sched.stops, llm.stopped), ("completed", ["aivis"], 1))

    async def test_llm_that_became_busy_is_not_stopped(self):
        # 確かめてから止めるまでの間(読み上げの停止を待つ間)に chat が始まった: LLM は止めない(aivis は止める)
        sched, llm = self.sched_with_aivis(), self.fake_llm()
        answers = iter([True, False])
        llm.idle_stoppable = lambda: next(answers, False)
        job = await self.run_with(sched, 16 * 1024 ** 3, llm=llm)
        self.assertEqual((job.status, sched.stops, llm.stopped), ("completed", ["aivis"], 0))

    async def test_busy_or_external_helpers_are_kept(self):
        llm = self.fake_llm(idle=False)
        for sched, tts in ((self.sched_with_aivis(), types.SimpleNamespace(in_use=lambda: True)),
                           (self._external(), None)):
            await self.run_with(sched, 8 * 1024 ** 3, tts=tts, llm=llm)
            self.assertEqual(sched.stops, [])
        self.assertEqual(llm.stopped, 0)

    def _external(self):
        sched = DyingSched()
        sched.comps["aivis"] = types.SimpleNamespace(proc=None, external=True, state="ready")
        return sched

    async def test_enough_ram_or_never_keeps_them(self):
        for ram, setting in ((64 * 1024 ** 3, "auto"), (8 * 1024 ** 3, "never")):
            sched, llm = self.sched_with_aivis(), self.fake_llm()
            await self.run_with(sched, ram, setting, llm=llm)
            self.assertEqual((sched.stops, llm.stopped), ([], 0))
        sched, llm = self.sched_with_aivis(), self.fake_llm()
        await self.run_with(sched, 64 * 1024 ** 3, "always", llm=llm)
        self.assertEqual((sched.stops, llm.stopped), (["aivis"], 1))


class LlmPanelTest(unittest.IsolatedAsyncioTestCase):
    def make(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return llm_mod.Llm(Settings({"llm": {"tools_dir": str(Path(tmp.name) / "tools")}}, Path(tmp.name)))

    async def test_states_and_idle_stop(self):
        llm = self.make()
        self.assertEqual(llm.panel()["state"], "stopped")
        self.assertFalse(llm.idle_stoppable())
        popen = types.SimpleNamespace(poll=lambda: None)
        llm._proc = llm_mod.procman.Proc(spec=None, popen=popen, pid=None)
        llm._gpu, llm._cpu_only = True, False
        self.assertEqual(llm.panel()["state"], "ready-gpu")
        llm._gpu, llm._cpu_only = False, True
        p = llm.panel()
        self.assertEqual((p["state"], p["can_stop"], p["in_use"]), ("ready-cpu", True, 0))
        self.assertTrue(llm.idle_stoppable())
        llm._users = 1
        self.assertFalse(llm.idle_stoppable())
        llm._users, llm._starting = 0, True
        self.assertEqual(llm.panel()["state"], "starting")
        self.assertFalse(llm.idle_stoppable())


class EditPromptTest(unittest.TestCase):
    def test_edit_outputs_have_no_description(self):
        self.assertEqual(mv.description_prompt({"prompt": "周囲を広げる", "kind": "h3image.edit"}), "")
        self.assertEqual(mv.description_prompt({"prompt": "make it night", "references": ["abc"]}), "")
        self.assertEqual(mv.description_prompt({"prompt": " a girl ", "kind": "h3image.t2i", "references": []}), "a girl")
        self.assertEqual(mv.description_prompt(None), "")

    def test_mv_resolve_skips_the_edit_prompt(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ledger = Ledger(Path(tmp.name))
        self.addCleanup(ledger.close)

        def add(kind, name, params):
            p = Path(tmp.name) / name
            p.write_bytes(png_bytes() if kind == "image" else b"x")
            return ledger.add_output(kind, p, "upload", filename=name, params=params, duration_s=30.0 if kind == "audio" else None)
        song = add("audio", "s.mp3", {"theme": "t"})
        edited = add("image", "e.png", {"prompt": "周囲を広げる", "kind": "h3image.edit", "references": ["x"]})
        plain = add("image", "p.png", {"prompt": "a girl", "kind": "h3image.t2i"})
        r = mv.resolve_params(ledger, {"song_id": song["id"], "image_id": edited["id"]})
        self.assertEqual((r["character"], r["character_source"]), ("", ""))
        self.assertEqual(mv.resolve_params(ledger, {"song_id": song["id"], "image_id": plain["id"]})["character"], "a girl")


class WebTest(unittest.TestCase):
    def test_short_tab_defaults_to_scenery_and_uses_desc_prompt(self):
        web = REPO_ROOT / "web"
        short, mvjs, project = ((web / n).read_text("utf-8") for n in ("short.js", "mv.js", "project.js"))
        self.assertIn('role: role === "person" && !person() ? "person" : "scenery"', short)
        self.assertNotIn("params.prompt", mvjs)
        self.assertIn('p.kind === "h3image.edit"', project)
        for js in (short, mvjs):
            self.assertIn("descPrompt(", js)
        self.assertNotIn("口を合わせる人物の画像を入れて", short)
        self.assertIn('sh("vo-note")', short)
        app = (web / "app.js").read_text("utf-8")
        self.assertIn('post("/api/llm/stop")', app)


if __name__ == "__main__":
    unittest.main()
