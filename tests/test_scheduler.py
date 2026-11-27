import asyncio
import types
import unittest
from unittest import mock

from studio import scheduler as S
from studio.components import ComponentSpec
from studio.scheduler import Busy, Scheduler


def spec(name, uses_gpu=True, can_unload=True, port=1):
    return ComponentSpec(
        name=name, label=name, dir=None, port=port, health_path="/h", health_ok=lambda j: True, uses_gpu=uses_gpu,
        can_unload=can_unload, unload_path="/unload" if can_unload else None,
        reload_path="/reload" if can_unload else None, startup_timeout_s=5,
        build_command=lambda d: ["x"])


class Fake:
    """HTTP と procman の代わり。呼ばれた順を calls に残す。"""

    def __init__(self, unload_code=200):
        self.calls = []
        self.unload_code = unload_code

    async def get(self, url, timeout=3.0):
        return 200, {}

    async def post(self, url, timeout=30.0):
        self.calls.append(("post", url.rsplit("/", 1)[-1], url.split(":")[2].split("/")[0]))
        return self.unload_code if url.endswith("/unload") else 200

    def start(self, sp):
        self.calls.append(("start", sp.name))
        return types.SimpleNamespace(spec=sp, popen=types.SimpleNamespace(poll=lambda: None, returncode=None),
                                     external=False, pid=1, job=None)

    def stop(self, proc, grace_s=20):
        self.calls.append(("stop", proc.spec.name))


class SchedulerTest(unittest.IsolatedAsyncioTestCase):
    def make(self, fake):
        specs = {"h3": spec("h3", port=11), "h3image": spec("h3image", port=12),
                 "ace": spec("ace", can_unload=False, port=13), "aivis": spec("aivis", uses_gpu=False, can_unload=False, port=14)}
        patches = [mock.patch.object(S, "http_get_json", fake.get), mock.patch.object(S, "http_post", fake.post),
                   mock.patch.object(S.procman, "start", fake.start), mock.patch.object(S.procman, "stop", fake.stop),
                   mock.patch.object(S.procman, "adopt_if_running", lambda sp: None),
                   mock.patch.object(S.procman, "is_alive", lambda p: p is not None),
                   mock.patch.object(S.procman, "log_tail", lambda n, k=20: "")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return Scheduler(specs, monitor=None, poll_s=0.01)

    async def test_unload_then_start(self):
        fake = Fake()
        s = self.make(fake)
        await s.ensure("h3image")
        self.assertEqual(s.gpu_resident, "h3image")
        fake.calls.clear()
        await s.ensure("h3")
        self.assertEqual(fake.calls, [("post", "unload", "12"), ("start", "h3")])
        self.assertEqual((s.comps["h3image"].state, s.comps["h3"].state, s.gpu_resident), ("unloaded", "ready", "h3"))

    async def test_reload_when_unloaded(self):
        fake = Fake()
        s = self.make(fake)
        await s.ensure("h3image")
        await s.ensure("h3")
        fake.calls.clear()
        await s.ensure("h3image")
        self.assertEqual(fake.calls, [("post", "unload", "11"), ("post", "reload", "12")])

    async def test_ace_resident_is_stopped(self):
        fake = Fake()
        s = self.make(fake)
        await s.ensure("ace")
        fake.calls.clear()
        await s.ensure("h3")
        self.assertEqual(fake.calls, [("stop", "ace"), ("start", "h3")])
        self.assertEqual(s.comps["ace"].state, "stopped")

    async def test_unload_409_raises_busy(self):
        fake = Fake(unload_code=409)
        s = self.make(fake)
        await s.ensure("h3image")
        with self.assertRaises(Busy) as cm:
            await s.ensure("h3")
        self.assertEqual(cm.exception.message, "生成中のエンジンがあるので切り替えられません")
        self.assertEqual(s.gpu_resident, "h3image")

    async def test_low_ram_stops_the_engine_it_switches_away_from(self):
        # RAM の少ない PC: 降ろしただけのプロセスが RAM を持ったままで、次のエンジンが読み込みの途中で落ちた(2026-11-28)
        fake = Fake()
        s = self.make(fake)
        s.stop_on_switch = True
        await s.ensure("h3image")
        fake.calls.clear()
        op = s.new_operation("ensure", "h3")
        await s.ensure("h3", op)
        self.assertEqual(fake.calls, [("post", "unload", "12"), ("stop", "h3image"), ("start", "h3")])      # unload を通してから止める
        self.assertEqual((s.comps["h3image"].state, s.comps["h3"].state, s.gpu_resident), ("stopped", "ready", "h3"))
        self.assertIn("RAM が少ないので h3image を止めます(次に使うときに起動し直します)", [x["msg"] for x in op.steps])
        fake.calls.clear()
        await s.ensure("h3image")                                      # 次に使うときは起動し直す(reload ではない)
        self.assertEqual(fake.calls, [("post", "unload", "11"), ("stop", "h3"), ("start", "h3image")])

    async def test_low_ram_does_not_stop_a_busy_engine(self):
        fake = Fake(unload_code=409)                                   # 生成中: unload が断られる
        s = self.make(fake)
        s.stop_on_switch = True
        await s.ensure("h3image")
        with self.assertRaises(Busy):
            await s.ensure("h3")
        self.assertNotIn(("stop", "h3image"), fake.calls)
        self.assertEqual((s.comps["h3image"].state, s.gpu_resident), ("ready", "h3image"))

    async def test_low_ram_stops_engines_left_unloaded(self):
        fake = Fake()
        s = self.make(fake)
        await s.ensure("h3image")
        await s.ensure("h3")                                           # まだ降ろすだけ(h3image は unloaded のまま残る)
        self.assertEqual(s.comps["h3image"].state, "unloaded")
        s.stop_on_switch = True
        fake.calls.clear()
        await s.ensure("ace")                                          # h3 を降ろして止め、残っていた h3image も止める
        self.assertEqual(fake.calls, [("post", "unload", "11"), ("stop", "h3"), ("stop", "h3image"), ("start", "ace")])
        self.assertEqual([s.comps[n].state for n in ("h3", "h3image", "ace")], ["stopped", "stopped", "ready"])

    async def test_aivis_never_touches_resident(self):
        fake = Fake()
        s = self.make(fake)
        await s.ensure("h3")
        fake.calls.clear()
        await s.ensure("aivis")
        self.assertEqual(fake.calls, [("start", "aivis")])
        self.assertEqual(s.gpu_resident, "h3")

    async def test_ensure_resident_is_noop(self):
        fake = Fake()
        s = self.make(fake)
        await s.ensure("h3")
        fake.calls.clear()
        await s.ensure("h3")
        self.assertEqual(fake.calls, [])

    async def test_launch_reports_error(self):
        fake = Fake(unload_code=409)
        s = self.make(fake)
        await s.ensure("h3image")
        op = s.launch("ensure", "h3")
        await asyncio.gather(*s._tasks)
        self.assertTrue(op.done)
        self.assertIn("切り替えられません", op.error)


if __name__ == "__main__":
    unittest.main()


class ExternalCoexistTest(unittest.IsolatedAsyncioTestCase):
    """止められない外部のエンジン(ACE)が GPU にいるとき: 空きがあれば同居、無ければ分かる言葉で断る。"""

    def make(self, fake, free_mib):
        specs = {"h3": spec("h3", port=11), "ace": spec("ace", can_unload=False, port=13)}
        specs["h3"].vram_need_mib = 12000
        patches = [mock.patch.object(S, "http_get_json", fake.get), mock.patch.object(S, "http_post", fake.post),
                   mock.patch.object(S.procman, "start", fake.start), mock.patch.object(S.procman, "stop", fake.stop),
                   mock.patch.object(S.procman, "adopt_if_running", lambda sp: None),
                   mock.patch.object(S.procman, "is_alive", lambda p: p is not None),
                   mock.patch.object(S.procman, "log_tail", lambda n, k=20: "")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        monitor = types.SimpleNamespace(snapshot=lambda: {"gpu": {"total_mib": 97887, "used_mib": 97887 - free_mib}})
        s = Scheduler(specs, monitor=monitor, poll_s=0.01)
        s.gpu_wait_s = 0
        ace = s.comps["ace"]
        ace.proc, ace.external = types.SimpleNamespace(spec=specs["ace"], popen=None, external=True, pid=None, job=None), True
        ace.state, s.gpu_resident = "ready", "ace"
        return s

    async def test_coexists_when_gpu_has_room(self):
        fake = Fake()
        s = self.make(fake, free_mib=60000)
        await s.ensure("h3")
        self.assertEqual(fake.calls, [("start", "h3")])          # ACE は止めない
        self.assertEqual((s.comps["ace"].state, s.comps["h3"].state, s.gpu_resident), ("ready", "ready", "h3"))

    async def test_refuses_with_hint_when_no_room(self):
        fake = Fake()
        s = self.make(fake, free_mib=4000)
        with self.assertRaises(S.SwitchError) as ctx:
            await s.ensure("h3")
        self.assertIn("同居できません", ctx.exception.message)
        self.assertIn("port", ctx.exception.message)
        self.assertEqual(fake.calls, [])

    async def test_a_closed_external_ace_is_forgotten_instead_of_refused(self):
        fake = DeadPorts({"13"})                                   # 外部の ACE は閉じられた(準備確認が応答しない)
        s = self.make(fake, free_mib=4000)
        await s.ensure("h3")
        self.assertEqual(fake.calls, [("start", "h3")])
        ace = s.comps["ace"]
        self.assertEqual((ace.state, ace.external, ace.proc, s.gpu_resident), ("stopped", False, None, "h3"))


class DeadPorts(Fake):
    """指定したポートのエンジンは、もう動いていない(接続できない)。"""

    def __init__(self, dead: set, unload_code=200):
        super().__init__(unload_code)
        self.dead = dead

    async def get(self, url, timeout=3.0):
        return (None, None) if url.split(":")[2].split("/")[0] in self.dead else (200, {})

    async def post(self, url, timeout=30.0):
        code = await super().post(url, timeout)
        return None if url.split(":")[2].split("/")[0] in self.dead else code


class ClosedExternalTest(unittest.IsolatedAsyncioTestCase):
    """外部で動いていたエンジン(取り込んで常駐の記録にあるもの)が閉じられたとき、unload が届かないまま他のエンジンの仕事を失敗させ続けない。"""

    def make(self, fake, mode="exclusive"):
        specs = {"h3": spec("h3", port=11), "h3image": spec("h3image", port=12)}
        patches = [mock.patch.object(S, "http_get_json", fake.get), mock.patch.object(S, "http_post", fake.post),
                   mock.patch.object(S.procman, "start", fake.start), mock.patch.object(S.procman, "stop", fake.stop),
                   mock.patch.object(S.procman, "adopt_if_running", lambda sp: None),
                   mock.patch.object(S.procman, "is_alive", lambda p: p is not None),
                   mock.patch.object(S.procman, "log_tail", lambda n, k=20: "")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        s = Scheduler(specs, monitor=None, poll_s=0.01, mode=mode)
        h3 = s.comps["h3"]
        h3.proc, h3.external, h3.state = types.SimpleNamespace(spec=specs["h3"], popen=None, external=True, pid=None, job=None), True, "ready"
        s.gpu_resident = "h3"
        return s

    async def test_a_closed_external_resident_is_forgotten_and_the_next_component_loads(self):
        fake = DeadPorts({"11"})
        s = self.make(fake)
        await s.ensure("h3image")
        self.assertEqual(fake.calls, [("post", "unload", "11"), ("start", "h3image")])
        h3 = s.comps["h3"]
        self.assertEqual((h3.state, h3.external, h3.proc, s.gpu_resident), ("stopped", False, None, "h3image"))

    async def test_a_live_external_that_does_not_unload_still_fails(self):
        fake = Fake(unload_code=500)                               # 応答はある(動いている)が unload に失敗: 今までどおり失敗にする
        s = self.make(fake)
        with self.assertRaisesRegex(S.SwitchError, "unload に失敗しました"):
            await s.ensure("h3image")
        self.assertEqual((s.comps["h3"].state, s.comps["h3"].external, s.gpu_resident), ("ready", True, "h3"))

    async def test_share_mode_victim_that_was_closed_is_forgotten(self):
        fake = DeadPorts({"11"})
        s = self.make(fake, mode="share")
        s.total_mib = 20 * 1024
        s.residents = {"h3": S.Resident(budget_mib=16 * 1024)}
        s.comps["h3image"].spec.vram_budget_gb = 10                # 2 つは収まらない: h3 を降ろす
        await s.ensure("h3image")
        self.assertEqual(set(s.residents), {"h3image"})
        self.assertEqual(s.comps["h3"].state, "stopped")
