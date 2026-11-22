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
        self.assertEqual(cm.exception.message, "生成中の部品があるので切り替えられません")
        self.assertEqual(s.gpu_resident, "h3image")

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
    """止められない外部の部品(ACE)が GPU にいるとき: 空きがあれば同居、無ければ分かる言葉で断る。"""

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
