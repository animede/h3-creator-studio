"""ジョブの同時実行: share の予算と載せる判定、レーンと公平さ、CPU のレーン。

scheduler は本物(HTTP と procman だけ差し替え)、部品の仕事は run が gate を待つだけの偽物。
"""
import asyncio
import contextlib
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from studio import jobs as jobs_mod
from studio import main
from studio import scheduler as S
from studio.components import ComponentSpec, get_spec
from studio.config import Settings
from studio.jobs import CPU_LANE, Ctx, Handler, JobRunner
from studio.ledger import Ledger
from studio.scheduler import Operation, Scheduler, SwitchError, resolve_gpu_mode

GIB = 1024


def spec(name, gb=None, uses_gpu=True, can_unload=True, port=1):
    sp = ComponentSpec(
        name=name, label=name, dir=None, port=port, health_path="/h", health_ok=lambda j: True, uses_gpu=uses_gpu,
        can_unload=can_unload, unload_path="/unload" if can_unload else None,
        reload_path="/reload" if can_unload else None, startup_timeout_s=5,
        build_command=lambda d: ["x"], vram_budget_gb=gb, vram_need_mib=int((gb or 0) * GIB))
    sp.budget_env = lambda mib, n=name: {"BUDGET_" + n.upper(): str(mib // GIB)}
    return sp


class FakeMonitor:
    def __init__(self, total_gib, used_mib=2048):
        self.gpu = {"total_mib": int(total_gib * GIB), "used_mib": used_mib}

    def snapshot(self):
        return {"gpu": self.gpu}


class Backend:
    """HTTP と procman の代わり。呼ばれた順を calls に、起動時に渡された環境変数を envs に残す。"""

    def __init__(self):
        self.calls, self.envs = [], {}

    async def get(self, url, timeout=3.0):
        return 200, {}

    async def post(self, url, timeout=30.0):
        self.calls.append(("post", url.rsplit("/", 1)[-1], url.split(":")[2].split("/")[0]))
        return 200

    def start(self, sp, extra_env=None):
        self.calls.append(("start", sp.name))
        self.envs[sp.name] = extra_env
        return types.SimpleNamespace(spec=sp, popen=types.SimpleNamespace(poll=lambda: None, returncode=None),
                                     external=False, pid=1, job=None)

    def stop(self, proc, grace_s=20):
        self.calls.append(("stop", proc.spec.name))

    def unloads(self):
        return [c for c in self.calls if c[:2] == ("post", "unload")]


class ShareBase(unittest.IsolatedAsyncioTestCase):
    def patch(self, backend):
        for p in (mock.patch.object(S, "http_get_json", backend.get), mock.patch.object(S, "http_post", backend.post),
                  mock.patch.object(S.procman, "start", backend.start), mock.patch.object(S.procman, "stop", backend.stop),
                  mock.patch.object(S.procman, "adopt_if_running", lambda sp: None),
                  mock.patch.object(S.procman, "is_alive", lambda p: p is not None),
                  mock.patch.object(S.procman, "log_tail", lambda n, k=20: "")):
            p.start()
            self.addCleanup(p.stop)

    def sched(self, total_gib, mode="share", used_mib=2048, **budgets):
        """budgets: 部品名 -> 予算(GiB)。ポートは 11, 12, ... を順に振る。"""
        self.backend = Backend()
        self.patch(self.backend)
        specs = {n: spec(n, gb, port=11 + i) for i, (n, gb) in enumerate(budgets.items())}
        self.monitor = FakeMonitor(total_gib, used_mib)
        s = Scheduler(specs, monitor=self.monitor, poll_s=0.01, mode=mode, total_mib=self.monitor.gpu["total_mib"])
        s.gpu_wait_s = 0
        return s


class ModeTest(unittest.TestCase):
    def test_resolve(self):
        self.assertEqual(resolve_gpu_mode("auto", 97887), "share")
        self.assertEqual(resolve_gpu_mode("auto", 49140), "share")           # 48GB のカード
        self.assertEqual(resolve_gpu_mode("auto", 40 * 1024), "share")        # ちょうど 40GiB
        self.assertEqual(resolve_gpu_mode("auto", 40 * 1024 - 1), "exclusive")
        self.assertEqual(resolve_gpu_mode("auto", 12288), "exclusive")
        self.assertEqual(resolve_gpu_mode("auto", None), "exclusive")         # 総 VRAM が分からない
        self.assertEqual(resolve_gpu_mode("share", None), "exclusive")        # share は予算の計算に総 VRAM が要る
        self.assertEqual(resolve_gpu_mode("share", 16376), "share")           # 明示すれば小さな GPU でも share
        self.assertEqual(resolve_gpu_mode("exclusive", 97887), "exclusive")

    def test_configure_gpu_reads_total_from_monitor(self):
        s = Scheduler({}, monitor=FakeMonitor(95.5))
        self.assertEqual((s.configure_gpu("auto"), s.mode, s.total_mib), ("share", "share", 97792))
        s = Scheduler({}, monitor=FakeMonitor(12))
        self.assertEqual(s.configure_gpu("auto"), "exclusive")
        self.assertEqual(Scheduler({}, monitor=types.SimpleNamespace(snapshot=lambda: {"gpu": None})).configure_gpu("share"), "exclusive")
        self.assertEqual(Scheduler({}).configure_gpu("auto"), "exclusive")    # 監視が無い


class BudgetTest(ShareBase):
    async def test_budget_env_is_passed_only_in_share(self):
        s = self.sched(70, h3=30, h3image=20)
        await s.ensure("h3")
        await s.ensure("h3image")
        self.assertEqual(self.backend.envs, {"h3": {"BUDGET_H3": "30"}, "h3image": {"BUDGET_H3IMAGE": "20"}})
        self.assertEqual({n: r.budget_mib for n, r in s.residents.items()}, {"h3": 30 * GIB, "h3image": 20 * GIB})
        s = self.sched(70, mode="exclusive", h3=30, h3image=20)
        await s.ensure("h3")
        self.assertIsNone(self.backend.envs["h3"])          # exclusive は何も足さない(部品が自分で全体を見て選ぶ)
        self.assertIsNone(s.residents["h3"].budget_mib)

    async def test_default_budgets_follow_total_vram(self):
        specs = {n: spec(n, port=11 + i) for i, n in enumerate(("h3", "h3image", "ace", "other"))}
        specs["other"].vram_need_mib = 7000
        for total_gib, want in ((96, (48, 31, 14)), (80, (40, 28, 14)), (48, (24, 16.8, 14))):
            s = Scheduler(specs, monitor=FakeMonitor(total_gib), mode="share", total_mib=total_gib * GIB)
            self.assertEqual([s._default_budget_mib(s.comps[n]) for n in ("h3", "h3image", "ace")], [int(g * GIB) for g in want])
            self.assertEqual(s._default_budget_mib(s.comps["other"]), 7000)      # 既定の無い部品は、生成に要る量の目安

    async def test_vram_budget_override_wins(self):
        cfg = Settings({"components": {"h3": {"vram_budget_gb": 20}, "ace": {"vram_budget_gb": "bad"}}}, Path("."))
        sp = get_spec("h3", cfg)
        self.assertEqual(sp.vram_budget_gb, 20.0)
        self.assertIsNone(get_spec("ace", cfg).vram_budget_gb)
        s = Scheduler({"h3": sp}, monitor=FakeMonitor(96), mode="share", total_mib=96 * GIB)
        self.assertEqual(s._default_budget_mib(s.comps["h3"]), 20 * GIB)
        self.assertEqual(sp.budget_env(20 * GIB), {"H3_PROFILE": "16gb", "H3_VRAM_LIMIT_GB": "20"})

    async def test_reload_keeps_the_budget_the_process_started_with(self):
        s = self.sched(50, h3=30, h3image=30)
        await s.ensure("h3")
        await s.ensure("h3image")                     # h3 を降ろす(unload)
        self.assertEqual((s.comps["h3"].state, "h3" in s.residents), ("unloaded", False))
        s.comps["h3"].spec.vram_budget_gb = 10        # 設定を変えても、起動済みの部品の予算は変わらない
        self.backend.calls.clear()
        await s.ensure("h3")                          # h3image を降ろして h3 を reload
        self.assertEqual(self.backend.calls, [("post", "unload", "12"), ("post", "reload", "11")])
        self.assertEqual(s.residents["h3"].budget_mib, 30 * GIB)
        self.assertEqual(self.backend.envs["h3"], {"BUDGET_H3": "30"})        # 環境変数は最初の起動のとき 1 回だけ

    async def test_stop_forgets_the_budget(self):
        s = self.sched(70, ace=14)
        s.comps["ace"].spec.can_unload = False
        await s.ensure("ace")
        self.assertEqual(s.comps["ace"].budget_mib, 14 * GIB)
        await s.stop("ace")
        self.assertIsNone(s.comps["ace"].budget_mib)
        self.assertEqual(s.residents, {})


class ShareAdmissionTest(ShareBase):
    async def test_fits_alongside_without_unloading(self):
        s = self.sched(70, a=30, b=30)                # 容量 = 70 - 2 - 1.5 = 66.5GiB ≥ 60
        await s.ensure("a")
        await s.ensure("b")
        self.assertEqual(self.backend.calls, [("start", "a"), ("start", "b")])
        self.assertEqual(list(s.residents), ["a", "b"])
        self.assertEqual((s.comps["a"].state, s.comps["b"].state, s.gpu_resident), ("ready", "ready", "a"))

    async def test_resident_ensure_is_noop_even_while_another_loads(self):
        s = self.sched(70, a=30, b=30)
        await s.ensure("a")
        self.backend.calls.clear()
        async with s._gpu_lock:                        # 別の部品を載せている最中でも、載っている部品の ensure は待たない
            await asyncio.wait_for(s.ensure("a"), 1)
        self.assertEqual(self.backend.calls, [])

    async def test_evicts_idle_least_recently_used_first(self):
        s = self.sched(70, a=25, b=25, c=25)          # 容量 66.5 < 75: 3 つは載らない
        await s.ensure("a")
        await s.ensure("b")
        s.residents["a"].last_used, s.residents["b"].last_used = 200.0, 100.0     # b の方が古い
        self.backend.calls.clear()
        await s.ensure("c")
        self.assertEqual(self.backend.unloads(), [("post", "unload", "12")])      # b だけ降ろす
        self.assertEqual((s.comps["a"].state, s.comps["b"].state, s.comps["c"].state), ("ready", "unloaded", "ready"))
        self.assertEqual(list(s.residents), ["a", "c"])

    async def test_needs_several_victims_in_lru_order(self):
        s = self.sched(70, a=20, b=20, c=20, big=40)
        for n in "abc":
            await s.ensure(n)
        for n, t in (("a", 300.0), ("b", 100.0), ("c", 200.0)):
            s.residents[n].last_used = t
        self.backend.calls.clear()
        await s.ensure("big")                          # 40 + 残り ≤ 66.5 → 2 つ降ろせば足りる(b、c の順)
        self.assertEqual(self.backend.unloads(), [("post", "unload", "12"), ("post", "unload", "13")])
        self.assertEqual(list(s.residents), ["a", "big"])

    async def test_busy_component_is_never_evicted_and_ensure_waits(self):
        s = self.sched(50, a=30, b=30)                # 容量 46.5 < 60
        await s.ensure("a")
        s.mark_busy("a")
        plan = s.plan("b")
        self.assertEqual((plan.action, plan.blockers, plan.victims), ("wait", ["a"], []))
        self.assertIn("a が生成中", plan.reason)
        op = s.new_operation("ensure", "b")
        task = asyncio.create_task(s.ensure("b", op))
        await asyncio.sleep(0.1)
        self.assertFalse(task.done())                  # 仕事中の a は降ろさずに待つ
        self.assertEqual(self.backend.unloads(), [])
        self.assertEqual(op.wait_reason, plan.reason)
        self.assertEqual(sum(1 for st in op.steps if st["msg"] == plan.reason), 1)     # 同じ理由を何度も書かない
        s.mark_idle("a")
        await asyncio.wait_for(task, 2)
        self.assertEqual(self.backend.unloads(), [("post", "unload", "11")])
        self.assertIsNone(op.wait_reason)
        self.assertEqual(list(s.residents), ["b"])

    async def test_claimed_component_holds_its_budget_before_it_is_loaded(self):
        s = self.sched(50, a=30, b=30)
        s.mark_busy("a")                               # 仕事を割り当てた(まだ載っていない)
        self.assertEqual(s.plan("b").action, "wait")   # a の分を空きと見ない
        s.mark_idle("a")
        self.assertEqual(s.plan("b").action, "fit")

    async def test_oversized_budget_loads_alone(self):
        s = self.sched(40, a=10, huge=60)              # 容量 36.5 < 60: 他を全部降ろして 1 つだけ載せる
        await s.ensure("a")
        self.assertEqual(s.plan("huge").action, "evict")
        await s.ensure("huge")
        self.assertEqual(list(s.residents), ["huge"])

    async def test_failed_start_returns_the_reservation(self):
        s = self.sched(70, a=30)
        s.comps["a"].spec.build_command = lambda d: (_ for _ in ()).throw(S.ComponentNotInstalled("a", "入っていません"))
        with mock.patch.object(S.procman, "start", side_effect=S.ComponentNotInstalled("a", "入っていません")):
            with self.assertRaises(SwitchError):
                await s.ensure("a")
        self.assertEqual(s.residents, {})
        self.assertEqual(s.comps["a"].state, "error")

    async def test_dead_resident_does_not_hold_its_budget(self):
        s = self.sched(50, a=30, b=30)
        await s.ensure("a")
        with mock.patch.object(S.procman, "is_alive", lambda p: False):
            self.assertEqual(s.plan("b").action, "fit")        # a のプロセスが落ちていたら、予算を押さえない
        self.assertEqual((s.comps["a"].state, s.residents), ("stopped", {}))

    async def test_baseline_is_measured_at_startup_and_shrinks_capacity(self):
        s = self.sched(50, used_mib=10 * GIB, a=20, b=20)        # 他のアプリが 10GiB 使っている: 容量 50 - 10 - 1.5 = 38.5 < 40
        await s.adopt_running()
        self.assertEqual(s.baseline_mib, 10 * GIB)
        self.assertEqual(s.capacity_mib(), int(38.5 * GIB))
        await s.ensure("a")
        self.assertEqual(s.plan("b").action, "evict")
        s2 = self.sched(50, mode="exclusive", used_mib=10 * GIB, a=20)
        await s2.adopt_running()
        self.assertIsNone(s2.baseline_mib)                       # exclusive は今までどおり、最初の載せ替えまで測らない

    async def test_status_reports_budget_and_busy(self):
        s = self.sched(70, a=30, b=30)
        await s.ensure("a")
        s.mark_busy("a")
        st = s.status()
        self.assertEqual((st["a"]["gpu_resident"], st["a"]["budget_mib"], st["a"]["busy"]), (True, 30 * GIB, True))
        self.assertEqual((st["b"]["gpu_resident"], st["b"]["budget_mib"], st["b"]["busy"]), (False, None, False))
        self.assertEqual(s.resident_status(), [{"name": "a", "label": "a", "budget_mib": 30 * GIB, "busy": True}])

    async def test_listeners_hear_state_changes(self):
        s = self.sched(70, a=30)
        heard = []
        s.watch(lambda: heard.append(1))
        await s.ensure("a")
        n = len(heard)
        self.assertGreater(n, 0)
        s.mark_busy("a")
        s.mark_idle("a")
        self.assertGreater(len(heard), n)

    async def test_gpu_switching_only_for_gpu_components(self):
        s = self.sched(70, a=30)
        s.comps["cpu1"] = S.Component(spec("cpu1", uses_gpu=False, can_unload=False, port=19))
        self.assertFalse(s.gpu_switching())
        op = s.new_operation("ensure", "cpu1")
        self.assertFalse(s.gpu_switching())
        op2 = s.new_operation("ensure", "a")
        self.assertTrue(s.gpu_switching())
        op.done = op2.done = True
        self.assertFalse(s.gpu_switching())


class ShareStartTest(ShareBase):
    """部品を起動するだけの start(常駐者は変えない): share では、降ろさずに載るときだけ起動し、載らなければ「準備」を案内する。"""

    async def test_start_alongside_when_it_fits_and_refuses_when_it_would_evict(self):
        s = self.sched(50, a=20, b=20, c=20)           # 容量 46.5: 2 つまで
        await s.start("a")
        await s.start("b")
        self.assertEqual(list(s.residents), ["a", "b"])
        self.assertEqual(self.backend.envs, {"a": {"BUDGET_A": "20"}, "b": {"BUDGET_B": "20"}})
        with self.assertRaises(SwitchError) as cm:
            await s.start("c")                          # 載せるには a か b を降ろす必要がある: start では降ろさない
        self.assertIn("「準備」", cm.exception.message)
        self.assertEqual(list(s.residents), ["a", "b"])
        await s.ensure("c")                             # 「準備」(ensure)なら、古い方を降ろして載せる
        self.assertEqual(sorted(s.residents), ["b", "c"])

    async def test_exclusive_start_is_unchanged(self):
        s = self.sched(50, mode="exclusive", a=20, b=20)
        await s.start("a")
        with self.assertRaises(SwitchError) as cm:
            await s.start("b")
        self.assertIn("a が GPU を使っています", cm.exception.message)


class PinnedExternalTest(ShareBase):
    """止められない外部の部品(ACE)は、宣言の予算で数える。収まらないときは今までのルール(実際の空きで同居、無ければ断る)。"""

    def external_ace(self, s):
        ace = s.comps["ace"]
        ace.proc, ace.external = types.SimpleNamespace(spec=ace.spec, popen=None, external=True, pid=None, job=None), True
        ace.state = "ready"
        s.residents["ace"] = S.Resident(budget_mib=14 * GIB)

    async def test_counted_by_declared_budget_and_never_evicted(self):
        s = self.sched(70, ace=14, a=30)
        s.comps["ace"].spec.can_unload = False
        self.external_ace(s)
        await s.ensure("a")                            # 14 + 30 ≤ 66.5: 同居
        self.assertEqual(self.backend.calls, [("start", "a")])
        self.assertEqual(list(s.residents), ["ace", "a"])

    async def test_coexists_by_free_vram_when_budgets_do_not_fit(self):
        s = self.sched(50, ace=14, a=40, used_mib=30 * GIB)     # 14 + 40 > 46.5 だが、実際の空きは 20GiB
        s.comps["ace"].spec.can_unload = False
        s.comps["a"].spec.vram_need_mib = 12000
        self.external_ace(s)
        self.assertEqual(s.plan("a").action, "fit")
        await s.ensure("a")
        self.assertEqual(self.backend.calls, [("start", "a")])

    async def test_refuses_with_hint_when_no_room(self):
        s = self.sched(50, ace=14, a=40, used_mib=48 * GIB)
        s.comps["ace"].spec.can_unload = False
        s.comps["a"].spec.vram_need_mib = 12000
        self.external_ace(s)
        self.assertEqual(s.plan("a").action, "refuse")
        with self.assertRaises(SwitchError) as cm:
            await s.ensure("a")
        self.assertIn("同居できません", cm.exception.message)
        self.assertIn("port", cm.exception.message)
        self.assertEqual(self.backend.calls, [])


# ---- JobRunner: レーンと割り当て ----

class FakeSched:
    """scheduler の代わり(plan も mark_busy も持たない): ensure が steps を足すだけ。"""

    def __init__(self):
        self.ensured = []

    def new_operation(self, kind, name):
        return Operation(kind, name)

    async def ensure(self, name, op=None):
        self.ensured.append(name)
        op.add("準備完了")


class RunnerBase(ShareBase):
    def setUp(self):
        self.gates = {}
        self.started, self.active, self.max_active = [], 0, 0
        p = mock.patch.object(jobs_mod, "DISPATCH_POLL_S", 30.0)      # 定期の見直しには頼らない: 通知(投入・終了・部品の状態)だけで進むこと
        p.start()
        self.addCleanup(p.stop)

    def gate(self, name):
        return self.gates.setdefault(name, asyncio.Event())

    def handlers(self, *components, gpu=True):
        async def run(job, ctx):
            self.started.append(job.params.get("name") or job.id)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                if job.params.get("gate"):
                    await self.gate(job.params["gate"]).wait()
            finally:
                self.active -= 1

        async def cancel(job, ctx):
            pass
        return {f"{c or 'cpu'}.go": Handler(c, lambda kind, params, ledger: dict(params), run, cancel, gpu=gpu and c is not None)
                for c in components}

    def runner(self, sched, handlers, llm=None, **kw):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ledger = Ledger(Path(tmp.name))
        self.addCleanup(ledger.close)
        runner = JobRunner(Ctx(sched, ledger, Path(tmp.name), llm=llm), handlers, step_poll_s=0.01, **kw)
        runner.start()
        self.addAsyncCleanup(runner.stop)
        return runner

    async def until(self, cond, what, timeout=3.0):
        for _ in range(int(timeout / 0.01)):
            if cond():
                return
            await asyncio.sleep(0.01)
        self.fail(f"条件が満たされない: {what}")

    async def status(self, job, *statuses):
        await self.until(lambda: job.status in statuses, f"{job.id} が {statuses}(いまは {job.status})")


class ShareRunTest(RunnerBase):
    async def test_two_components_run_at_the_same_time(self):
        s = self.sched(70, a=30, b=30)
        runner = self.runner(s, self.handlers("a", "b"))
        j1 = runner.submit("image", "a.go", {"gate": "g1", "name": "j1"})
        j2 = runner.submit("video", "b.go", {"gate": "g2", "name": "j2"})
        self.assertEqual((j1.lane, j2.lane), ("a", "b"))
        await self.status(j1, "running")
        await self.status(j2, "running")                 # 両方が同時に running
        self.assertEqual(self.max_active, 2)
        self.assertEqual(self.backend.unloads(), [])
        self.assertEqual(list(s.residents), ["a", "b"])
        self.assertEqual([j.id for j in runner.running()], [j1.id, j2.id])
        self.assertIn(runner.current, (j1, j2))
        self.assertTrue(runner.is_generating())
        lanes = runner.lane_status()
        self.assertEqual((lanes["a"]["running"], lanes["b"]["running"], lanes["a"]["queued"]), ([j1.id], [j2.id], 0))
        self.assertEqual(lanes["a"]["running_jobs"][0]["kind"], "a.go")
        self.gate("g1").set()
        self.gate("g2").set()
        await self.status(j1, "completed")
        await self.status(j2, "completed")
        self.assertEqual((runner.running(), runner.current, runner.is_generating(), runner.gpu_busy()), ([], None, False, False))
        self.assertEqual(s._busy, {})                    # 仕事中の印は外れる
        self.assertEqual(runner.done_counter, 2)

    async def test_second_waits_for_busy_first_then_evicts_it(self):
        s = self.sched(50, a=30, b=30)                   # 予算の合計 60 > 容量 46.5
        runner = self.runner(s, self.handlers("a", "b"))
        j1 = runner.submit("image", "a.go", {"gate": "g1", "name": "j1"})
        await self.status(j1, "running")
        j2 = runner.submit("video", "b.go", {"gate": "g2", "name": "j2"})
        await self.until(lambda: "GPU の空きを待っています" in (j2.wait_reason or ""), "j2 が空き待ちになる")
        self.assertEqual(j2.status, "queued")            # 空き待ちの間も queued のまま
        self.assertIn("a が生成中", j2.wait_reason)
        self.assertEqual(j2.message, j2.wait_reason)
        self.assertEqual(self.backend.unloads(), [])      # 仕事中の a は降ろさない
        self.assertEqual(j2.to_dict()["wait_reason"], j2.wait_reason)
        await asyncio.sleep(0.1)
        self.assertEqual((j1.status, j2.status), ("running", "queued"))
        self.gate("g1").set()
        await self.status(j2, "running")                 # a が終わったら、idle の a を降ろして b を載せる
        self.assertEqual(self.backend.unloads(), [("post", "unload", "11")])
        self.assertEqual((j1.status, j2.wait_reason), ("completed", None))
        self.assertEqual(self.max_active, 1)
        self.gate("g2").set()
        await self.status(j2, "completed")

    async def test_lane_serializes_same_component(self):
        s = self.sched(70, a=30, b=30)
        runner = self.runner(s, self.handlers("a", "b"))
        j1 = runner.submit("image", "a.go", {"gate": "g1", "name": "j1"})
        j2 = runner.submit("image", "a.go", {"gate": "g2", "name": "j2"})
        j3 = runner.submit("video", "b.go", {"gate": "g3", "name": "j3"})
        await self.status(j1, "running")
        await self.status(j3, "running")                  # 後ろのジョブが、空いているレーンで先に走る
        self.assertEqual((j2.status, j2.wait_reason), ("queued", "順番を待っています"))
        self.assertEqual(runner.lane_status()["a"]["queued"], 1)
        self.gate("g1").set()
        await self.status(j2, "running")
        self.assertEqual(self.started, ["j1", "j3", "j2"])
        for g in ("g2", "g3"):
            self.gate(g).set()
        await self.status(j2, "completed")

    async def test_cancel_waiting_job_and_it_never_runs(self):
        s = self.sched(50, a=30, b=30)
        runner = self.runner(s, self.handlers("a", "b"))
        j1 = runner.submit("image", "a.go", {"gate": "g1", "name": "j1"})
        await self.status(j1, "running")
        j2 = runner.submit("video", "b.go", {"gate": "g2", "name": "j2"})
        await self.until(lambda: "GPU の空き" in (j2.wait_reason or ""), "j2 が空き待ち")
        await runner.cancel(j2.id)
        self.assertEqual((j2.status, j2.wait_reason), ("cancelled", None))
        self.gate("g1").set()
        await self.status(j1, "completed")
        await asyncio.sleep(0.1)
        self.assertEqual((j2.status, self.started), ("cancelled", ["j1"]))
        self.assertEqual(self.backend.unloads(), [])

    async def test_submit_from_another_thread_wakes_the_dispatcher(self):
        s = self.sched(70, a=30)
        runner = self.runner(s, self.handlers("a"))
        await asyncio.sleep(0.05)                           # dispatcher が最初の見回りを終えて、通知を待つ状態になってから
        job = await asyncio.to_thread(runner.submit, "image", "a.go", {"name": "t"})      # 同期の API ルートは別のスレッドから呼ぶ
        await self.status(job, "completed")


class FairnessTest(RunnerBase):
    """先頭で GPU の空き待ちになったジョブが降ろしたい部品には、後から来た仕事を入れない。関係の無い常駐の部品の仕事は始めてよい。"""

    async def test_waiting_job_blocks_its_victims_but_not_unrelated_residents(self):
        s = self.sched(40, a=20, b=8, c=8, d=20)          # 容量 36.5。a + b + c = 36 は載る。d(20)を載せるには a を空ける必要がある
        runner = self.runner(s, self.handlers("a", "b", "c", "d"))
        await self.status(runner.submit("image", "b.go", {"name": "b0"}), "completed")
        ja = runner.submit("image", "a.go", {"gate": "ga", "name": "ja"})
        await self.status(ja, "running")
        await self.status(runner.submit("image", "c.go", {"name": "c0"}), "completed")
        s.residents["b"].last_used, s.residents["a"].last_used, s.residents["c"].last_used = 100.0, 200.0, 300.0
        self.assertEqual(list(s.residents), ["b", "a", "c"])

        w = runner.submit("video", "d.go", {"gate": "gd", "name": "w"})                # a が終わらないと載らない
        nb = runner.submit("image", "b.go", {"name": "nb"})                              # w が降ろしたい b(idle)
        nc = runner.submit("image", "c.go", {"name": "nc"})                              # w に関係しない c(idle)
        await self.status(nc, "completed")
        self.assertEqual((w.status, nb.status), ("queued", "queued"))
        self.assertIn("a が生成中", w.wait_reason)
        self.assertEqual(nb.wait_reason, "順番を待っています")                            # w より後ろの仕事は、w が降ろしたい部品に入れない
        self.assertEqual(self.started[-1], "nc")
        self.assertEqual(self.backend.unloads(), [])

        self.gate("ga").set()                                                          # a が終わると、w が b・c・a を降ろして載る
        await self.status(w, "running")
        self.assertEqual(len(self.backend.unloads()), 3)
        await self.status(nb, "completed")                                             # w が載った後、b は reload されて同居できる
        self.assertLess(self.started.index("w"), self.started.index("nb"))              # w の方が先
        self.assertEqual(w.status, "running")
        self.gate("gd").set()
        await self.status(w, "completed")

    async def test_later_admissions_wait_behind_a_waiting_job(self):
        s = self.sched(50, a=30, b=30, c=10)              # 容量 46.5: a(30)が仕事中だと b(30)は載らない。c(10)なら載る
        runner = self.runner(s, self.handlers("a", "b", "c"))
        ja = runner.submit("image", "a.go", {"gate": "ga", "name": "ja"})
        await self.status(ja, "running")
        w = runner.submit("video", "b.go", {"gate": "gb", "name": "w"})
        c = runner.submit("music", "c.go", {"name": "c"})                                # 空きに載るが、w を追い越さない
        await self.until(lambda: "GPU の空き" in (w.wait_reason or ""), "w が空き待ち")
        await asyncio.sleep(0.1)
        self.assertEqual((c.status, c.wait_reason), ("queued", "順番を待っています"))
        self.gate("ga").set()
        await self.status(w, "running")
        await self.status(c, "completed")
        self.assertLess(self.started.index("w"), self.started.index("c"))
        self.gate("gb").set()
        await self.status(w, "completed")

    async def test_earlier_job_is_not_blocked_by_a_later_waiting_job(self):
        s = self.sched(50, a=30, b=30)
        runner = self.runner(s, self.handlers("a", "b"))
        j1 = runner.submit("image", "a.go", {"gate": "g1", "name": "j1"})
        await self.status(j1, "running")
        j2 = runner.submit("image", "a.go", {"gate": "g2", "name": "j2"})                 # a の 2 つ目(順番待ち)
        w = runner.submit("video", "b.go", {"name": "w"})                                 # j2 より後ろで、a が空かないと載らない
        await self.until(lambda: "GPU の空き" in (w.wait_reason or ""), "w が空き待ち")
        self.gate("g1").set()
        await self.status(j2, "running")                                                  # a の仕事は w より前に受け付けたので先に走る
        self.assertEqual(w.status, "queued")
        self.gate("g2").set()
        await self.status(w, "completed")
        self.assertEqual(self.started, ["j1", "j2", "w"])


class ExclusiveRunTest(RunnerBase):
    async def test_gpu_jobs_run_one_at_a_time_in_submission_order(self):
        s = self.sched(100, mode="exclusive", a=30, b=30)
        runner = self.runner(s, self.handlers("a", "b"))
        j1 = runner.submit("image", "a.go", {"gate": "g1", "name": "j1"})
        j2 = runner.submit("video", "b.go", {"gate": "g2", "name": "j2"})
        j3 = runner.submit("image", "a.go", {"gate": "g3", "name": "j3"})
        await self.status(j1, "running")
        await self.until(lambda: "GPU の空きを待っています" in (j2.wait_reason or ""), "j2 の待ち理由")
        self.assertEqual((j2.status, j3.status, j3.wait_reason), ("queued", "queued", "順番を待っています"))
        self.gate("g1").set()
        await self.status(j2, "running")
        self.assertEqual((j3.status, list(s.residents), s.gpu_resident), ("queued", ["b"], "b"))   # 受け付けた順(j3 は j2 の後)
        self.gate("g2").set()
        await self.status(j3, "running")
        self.gate("g3").set()
        await self.status(j3, "completed")
        self.assertEqual((self.started, self.max_active), (["j1", "j2", "j3"], 1))
        self.assertEqual(self.backend.envs, {"a": None, "b": None})


class CpuLaneTest(RunnerBase):
    async def test_cpu_jobs_run_beside_a_gpu_job_two_at_a_time(self):
        sched = FakeSched()
        runner = self.runner(sched, {**self.handlers("a"), **self.handlers(None)})
        gpu = runner.submit("image", "a.go", {"gate": "gg", "name": "gpu"})
        c1 = runner.submit("video", "cpu.go", {"gate": "c1", "name": "c1"})
        c2 = runner.submit("video", "cpu.go", {"gate": "c2", "name": "c2"})
        c3 = runner.submit("video", "cpu.go", {"gate": "c3", "name": "c3"})
        self.assertEqual((c1.lane, gpu.lane), (CPU_LANE, "a"))
        for j in (gpu, c1, c2):
            await self.status(j, "running")                                              # GPU の仕事の最中に、CPU の仕事が 2 本走る
        await asyncio.sleep(0.1)
        self.assertEqual((c3.status, c3.wait_reason), ("queued", "順番を待っています"))    # 3 本目は待つ
        self.assertEqual(self.max_active, 3)
        self.assertEqual(sched.ensured, ["a"])                                           # CPU の仕事は部品を ensure しない
        self.assertEqual(runner.lane_status()[CPU_LANE]["queued"], 1)
        self.gate("c1").set()
        await self.status(c3, "running")
        self.assertEqual((sorted(self.started[:3]), self.started[3]), (["c1", "c2", "gpu"], "c3"))
        for g in ("c2", "c3", "gg"):
            self.gate(g).set()
        await self.status(c3, "completed")
        await self.status(gpu, "completed")

    async def test_cpu_job_does_not_claim_the_gpu(self):
        llm = types.SimpleNamespace(release=0)

        async def release():
            llm.release += 1
        llm.release_before_gpu = release
        runner = self.runner(FakeSched(), self.handlers(None), llm=llm)
        job = runner.submit("video", "cpu.go", {"gate": "c"})
        await self.status(job, "running")
        self.assertEqual((runner.gpu_busy(), runner.gpu_pending(), runner.is_generating(), llm.release), (False, False, False, 0))
        self.gate("c").set()
        await self.status(job, "completed")

    async def test_cpu_slots_can_be_changed(self):
        runner = self.runner(FakeSched(), self.handlers(None), cpu_slots=1)
        j1 = runner.submit("video", "cpu.go", {"gate": "c1", "name": "c1"})
        j2 = runner.submit("video", "cpu.go", {"gate": "c2", "name": "c2"})
        await self.status(j1, "running")
        await asyncio.sleep(0.1)
        self.assertEqual(j2.status, "queued")
        self.gate("c1").set()
        await self.status(j2, "running")
        self.gate("c2").set()


class WaitReasonTest(RunnerBase):
    async def test_preparing_shows_the_preparing_reason(self):
        gate = asyncio.Event()

        async def prepare(job, ctx):
            await gate.wait()
        handlers = self.handlers("a")
        handlers["a.go"].prepare = prepare
        runner = self.runner(FakeSched(), handlers)
        job = runner.submit("music", "a.go", {})
        self.assertEqual((job.status, job.wait_reason), ("queued", "順番を待っています"))
        await self.status(job, "preparing")
        self.assertEqual((job.wait_reason, job.message), ("部品を準備しています", "部品を準備しています"))
        gate.set()
        await self.status(job, "completed")
        self.assertIsNone(job.wait_reason)

    async def test_gpu_wait_inside_ensure_is_shown_and_cancel_interrupts_it(self):
        class WaitingSched(FakeSched):
            async def ensure(inner, name, op=None):
                op.wait_reason = "GPU の空きを待っています(a が生成中)"
                op.add(op.wait_reason)
                await asyncio.Event().wait()
        sched = WaitingSched()
        runner = self.runner(sched, self.handlers("b"))
        job = runner.submit("video", "b.go", {})
        await self.until(lambda: job.wait_reason == "GPU の空きを待っています(a が生成中)", "ensure の待ちがジョブに写る")
        self.assertEqual((job.status, job.message), ("preparing", "GPU の空きを待っています(a が生成中)"))
        await runner.cancel(job.id)
        await self.status(job, "cancelled")
        self.assertEqual(self.started, [])
        self.assertIsNone(job.error)

    async def test_job_dict_has_lane_and_wait_reason(self):
        runner = self.runner(FakeSched(), self.handlers("a"))
        job = runner.submit("image", "a.go", {})
        d = job.to_dict()
        self.assertEqual((d["lane"], d["wait_reason"], d["status"]), ("a", "順番を待っています", "queued"))
        await self.status(job, "completed")
        self.assertEqual((runner.list("image")[0]["lane"], runner.list("image")[0]["wait_reason"]), ("a", None))
        runner.jobs.clear()                                                   # 台帳から出てくる過去のジョブも、同じ形
        past = runner.list("image")[0]
        self.assertIn("lane", past)
        self.assertIn("wait_reason", past)


class ComponentActionTest(ShareBase):
    """画面の「準備」(ensure)は、仕事中の部品を降ろさないと載らないとき、待たせずに 409 で断る。"""

    def client(self, s):
        from fastapi.testclient import TestClient
        saved = {k: getattr(main.app.state, k, None) for k in ("sched", "llm")}
        self.addCleanup(lambda: [setattr(main.app.state, k, v) for k, v in saved.items()])
        released = []

        async def release():
            released.append(1)
        main.app.state.sched, main.app.state.llm = s, types.SimpleNamespace(release_before_gpu=release)
        return TestClient(main.app), released

    async def test_ensure_is_refused_while_a_busy_component_holds_the_gpu(self):
        for mode in ("share", "exclusive"):
            s = self.sched(50, mode=mode, a=30, b=30)
            await s.ensure("a")
            s.mark_busy("a")
            ops = dict(s.operations)
            client, released = self.client(s)
            r = await asyncio.to_thread(client.post, "/api/components/b/ensure")
            self.assertEqual(r.status_code, 409, mode)
            self.assertIn("b を GPU に載せられません", r.json()["detail"])
            self.assertIn("a が生成中", r.json()["detail"])
            self.assertEqual((released, s.operations), ([], ops), mode)         # LLM も止めず、操作も始めない
            s.mark_idle("a")
            r = await asyncio.to_thread(client.post, "/api/components/b/ensure")
            self.assertEqual(r.status_code, 200, mode)
            await asyncio.gather(*s._tasks)


class LifespanWiringTest(ShareBase):
    """起動時の配線: GPU の使い方を総 VRAM で決め、LLM は GPU の仕事(走っている・待っている・載せ替え中)のあいだ CPU 専用になる。"""

    @contextlib.asynccontextmanager
    async def booted(self, total_gib, setting="auto"):
        from studio.config import Settings
        monitor = FakeMonitor(total_gib)
        monitor.start = monitor.stop = lambda: None
        self.backend = Backend()
        self.patch(self.backend)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        specs = {n: spec(n, 30, port=11 + i) for i, n in enumerate("ab")}
        # lifespan が差し込む app.state(本物の Llm など)は、終わったら元に戻す。残すと、後のテストが本物の LLM のポート(8701)へ
        # 要求を送ってしまう(動いている管理役の LLM に届いていた。2026-11-24)
        keys, missing = ("sched", "ledger", "tts", "llm", "jobs", "mv", "monitor"), object()
        saved = {k: getattr(main.app.state, k, missing) for k in keys}

        def restore():
            for k, v in saved.items():
                if v is missing:
                    if hasattr(main.app.state, k):
                        delattr(main.app.state, k)
                else:
                    setattr(main.app.state, k, v)
        self.addCleanup(restore)
        with mock.patch.object(main, "Scheduler", lambda: Scheduler(specs, poll_s=0.01)), \
                mock.patch.object(main, "GpuMonitor", lambda **kw: monitor), \
                mock.patch.object(main, "OUTPUTS_DIR", Path(tmp.name)), \
                mock.patch.object(main, "settings", Settings({"gpu": {"mode": setting}}, Path(tmp.name))):
            async with main.lifespan(main.app):
                yield main.app.state

    async def test_big_gpu_shares_and_llm_follows_gpu_work(self):
        async with self.booted(95.5) as state:
            sched, runner, llm = state.sched, state.jobs, state.llm
            self.assertEqual((sched.mode, sched.total_mib), ("share", 97792))          # 総 VRAM 40GiB 以上の auto は share
            self.assertFalse(llm.busy())
            op = sched.new_operation("ensure", "a")                                     # 載せ替え中
            self.assertTrue(llm.busy())
            op.done = True
            self.assertFalse(llm.busy())
            runner._gpu_claimed.add("x")                                                # GPU の仕事が走っている
            self.assertTrue(llm.busy())
            runner._gpu_claimed.clear()
            self.assertFalse(llm.busy())
        self.assertIsNone(runner._dispatcher)                                           # 終了時に dispatcher が止まる

    async def test_small_gpu_is_exclusive_and_setting_wins(self):
        async with self.booted(12) as state:
            self.assertEqual(state.sched.mode, "exclusive")
        async with self.booted(12, "share") as state:
            self.assertEqual(state.sched.mode, "share")                                 # 明示すれば小さな GPU でも share
        async with self.booted(95.5, "exclusive") as state:
            self.assertEqual(state.sched.mode, "exclusive")


class StopTest(RunnerBase):
    async def test_stop_is_not_lost_when_a_wake_arrives_at_the_same_time(self):
        """dispatcher の待ちが通知で終わるのと同時に stop(中止)が来ても、止まる(wait_for は 3.11 で、この中止を握りつぶして固まった)。"""
        s = self.sched(70, a=30)
        runner = self.runner(s, self.handlers("a"))
        for _ in range(20):
            await asyncio.sleep(0.01)                  # dispatcher が通知を待つ状態になってから
            runner.wake()
            await asyncio.sleep(0)                     # 通知で待ちが終わる回に、stop の中止を重ねる
            await asyncio.wait_for(runner.stop(), 2)
            runner.start()                             # 次の周のために起こし直す(addAsyncCleanup の stop も同じ道を通る)
        self.assertTrue(runner._dispatcher is not None and not runner._dispatcher.done())

    async def test_stop_cancels_every_running_job(self):
        s = self.sched(70, a=30, b=30)
        runner = self.runner(s, self.handlers("a", "b"))
        j1 = runner.submit("image", "a.go", {"gate": "g1", "name": "j1"})
        j2 = runner.submit("video", "b.go", {"gate": "g2", "name": "j2"})
        j3 = runner.submit("image", "a.go", {"gate": "g3", "name": "j3"})       # a の順番待ち
        await self.status(j1, "running")
        await self.status(j2, "running")
        await runner.stop()
        self.assertEqual((j1.status, j2.status, j1.message), ("cancelled", "cancelled", "中止しました(管理役を停止)"))
        self.assertEqual(j3.status, "queued")
        self.assertEqual((runner.running(), s._busy), ([], {}))


class StatusPayloadTest(RunnerBase):
    """/api/status に、GPU の使い方・常駐中の部品(予算つき)・レーンごとの状況が載る。"""

    async def test_payload(self):
        s = self.sched(50, a=30, b=30)
        runner = self.runner(s, self.handlers("a", "b"))
        saved = {k: getattr(main.app.state, k, None) for k in ("sched", "monitor", "jobs")}
        self.addCleanup(lambda: [setattr(main.app.state, k, v) for k, v in saved.items()])
        main.app.state.sched, main.app.state.jobs = s, runner
        main.app.state.monitor = types.SimpleNamespace(snapshot=lambda: {"gpu": self.monitor.gpu, "ram": None, "shared_gpu_mib": None, "warning": None})
        j1 = runner.submit("image", "a.go", {"gate": "g1", "name": "j1"})
        await self.status(j1, "running")
        j2 = runner.submit("video", "b.go", {"gate": "g2", "name": "j2"})
        await self.until(lambda: "GPU の空き" in (j2.wait_reason or ""), "j2 が空き待ち")
        st = main.api_status()
        self.assertEqual(st["gpu_mode"], "share")
        self.assertEqual(st["residents"], [{"name": "a", "label": "a", "budget_mib": 30 * GIB, "busy": True}])
        self.assertEqual((st["lanes"]["a"]["running"], st["lanes"]["a"]["queued"]), ([j1.id], 0))
        self.assertEqual((st["lanes"]["b"]["running"], st["lanes"]["b"]["queued"]), ([], 1))
        job = st["lanes"]["a"]["running_jobs"][0]
        self.assertEqual((job["id"], job["kind"], job["status"], job["tab"]), (j1.id, "a.go", "running", "image"))
        self.assertEqual((st["components"]["a"]["gpu_resident"], st["components"]["a"]["budget_mib"], st["components"]["a"]["busy"]),
                         (True, 30 * GIB, True))
        self.gate("g1").set()
        await self.status(j2, "running")
        self.gate("g2").set()
        await self.status(j2, "completed")


class LlmFlagsTest(RunnerBase):
    async def test_gpu_busy_and_pending_follow_the_claim(self):
        s = self.sched(70, a=30)
        released = []

        async def release():
            released.append((runner.gpu_busy(), runner.gpu_pending()))
        runner = self.runner(s, self.handlers("a"), llm=types.SimpleNamespace(release_before_gpu=release))
        j1 = runner.submit("image", "a.go", {"gate": "g1", "name": "j1"})
        j2 = runner.submit("image", "a.go", {"name": "j2"})                   # 順番待ち(GPU の仕事が待っている)
        self.assertTrue(runner.gpu_pending())                                  # 待っているだけでも pending(LLM を GPU 版に戻さない)
        self.assertFalse(runner.gpu_busy())
        await self.status(j1, "running")
        self.assertEqual((runner.gpu_busy(), released), (True, [(True, True)]))   # LLM を止める前から、GPU を占める印が立つ
        self.gate("g1").set()
        await self.status(j2, "completed")
        self.assertEqual((runner.gpu_busy(), runner.gpu_pending()), (False, False))


if __name__ == "__main__":
    unittest.main()
