"""画面から MV の音声処理の道具を入れる(studio/mvinstall.py と POST /api/mv/tools/install)。子プロセスは偽物。"""
import io
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from studio import i18n, main, mv_audio, mvinstall

EN_HDR = {"Accept-Language": "en"}


class FakeProc:
    """setup.py の代わり。release() するまで出力の最後で待つ。"""

    def __init__(self, lines=(b">>> mvtools: torch\n", b"done\n"), rc=0):
        self.pid, self.rc = 4321, rc
        self.gate = threading.Event()
        self._lines = list(lines)
        self.stdout = self
        self.killed = False

    def readline(self):
        if self._lines:
            return self._lines.pop(0)
        self.gate.wait(5)
        return b""

    def wait(self):
        return self.rc

    def kill(self):
        self.killed = True
        self.gate.set()

    def release(self):
        self.gate.set()


class InstallerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.log = Path(tmp.name) / "logs" / "mvtools-install.log"
        self.procs, self.calls = [], []

    def popen(self, rc=0):
        def make(argv, **kw):
            self.calls.append((argv, kw))
            p = FakeProc(rc=rc)
            self.procs.append(p)
            return p
        return make

    def wait_done(self, inst):
        inst._thread.join(5)
        self.assertFalse(inst._thread.is_alive())

    def test_command_is_setup_only_mvtools_with_the_studio_python(self):
        argv = mvinstall.command()
        self.assertEqual(argv[0], mvinstall.sys.executable)
        self.assertTrue(argv[1].endswith("setup.py"))
        self.assertEqual(argv[2:], ["--only", "mvtools", "--no-models"])

    def test_start_progress_busy_and_finish(self):
        inst = mvinstall.ToolsInstaller(popen=self.popen(), log_path=self.log)
        self.assertEqual(inst.status(), {"installing": False})
        inst.start()
        self.assertTrue(inst.installing)
        with self.assertRaises(mvinstall.InstallBusy):
            inst.start()
        for _ in range(100):
            if inst.progress_text == "done":
                break
            threading.Event().wait(0.01)
        self.assertEqual(inst.status(), {"installing": True, "progress_text": "done"})
        self.procs[0].release()
        self.wait_done(inst)
        self.assertEqual(inst.status(), {"installing": False, "progress_text": "done"})
        self.assertIn(">>> mvtools: torch", self.log.read_text(encoding="utf-8"))
        self.assertEqual(self.calls[0][1]["stdin"], mvinstall.subprocess.DEVNULL)

    def test_failure_is_reported(self):
        inst = mvinstall.ToolsInstaller(popen=self.popen(rc=1), log_path=self.log)
        inst.start()
        self.procs[0].release()
        self.wait_done(inst)
        self.assertEqual(inst.status()["error"], mvinstall.FAILED_MESSAGE.format(1))

    def test_stop_kills_the_process_tree(self):
        inst = mvinstall.ToolsInstaller(popen=self.popen(), log_path=self.log)
        inst.stop()                                     # 走っていなければ何もしない
        inst.start()
        with mock.patch.object(mvinstall.os, "killpg", create=True) as killpg, mock.patch.object(mvinstall.sys, "platform", "linux"):
            killpg.side_effect = lambda pid, sig: self.procs[0].release()
            inst.stop()
        killpg.assert_called_once()
        self.assertFalse(inst.installing)

    def test_english_messages(self):
        for ja in (mvinstall.BUSY_MESSAGE, mvinstall.FAILED_MESSAGE.format(2), mvinstall.START_FAILED_MESSAGE.format("x")):
            self.assertNotRegex(i18n.tr(ja, "en"), r"[぀-ヿ一-鿿]", ja)


class InstallApiTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.procs = []

        def popen(argv, **kw):
            p = FakeProc()
            self.procs.append(p)
            return p
        saved = getattr(main.app.state, "mvinstall", None)
        self.addCleanup(lambda: setattr(main.app.state, "mvinstall", saved))
        self.inst = main.app.state.mvinstall = mvinstall.ToolsInstaller(popen=popen, log_path=Path(tmp.name) / "i.log")
        self.addCleanup(lambda: [p.release() for p in self.procs])
        self.client = TestClient(main.app)
        missing = mv_audio.MvToolsMissing(f"MV の音声処理の道具(Demucs と librosa)が見つかりません: /x。{mv_audio.MISSING_HINT}")
        patches = [mock.patch.object(mv_audio, "tools_python", side_effect=missing), mock.patch.object(mv_audio, "configured_python", return_value=""),
                   mock.patch.object(main.shutil, "which", return_value="/usr/bin/ffmpeg")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_install_start_conflict_and_status_transitions(self):
        js = self.client.get("/api/mv/tools").json()
        self.assertEqual((js["available"], js["installable"], js["installing"]), (False, True, False))
        js = self.client.post("/api/mv/tools/install").json()
        self.assertTrue(js["installing"])
        r = self.client.post("/api/mv/tools/install")
        self.assertEqual((r.status_code, r.json()["detail"]), (409, mvinstall.BUSY_MESSAGE))
        r = self.client.post("/api/mv/tools/install", headers=EN_HDR)
        self.assertEqual(r.json()["detail"], "The MV audio tools are being installed. Please wait until it finishes")
        self.assertTrue(self.client.get("/api/mv/tools").json()["installing"])
        self.procs[0].release()
        self.inst._thread.join(5)
        with mock.patch.object(mv_audio, "tools_python", return_value=Path("/x/python")):
            js = self.client.get("/api/mv/tools").json()
        self.assertEqual((js["available"], js["hint"], js["installing"]), (True, None, False))

    def test_configured_python_is_not_installable(self):
        with mock.patch.object(mv_audio, "configured_python", return_value="/my/python"):
            self.assertFalse(self.client.get("/api/mv/tools").json()["installable"])


class WebTest(unittest.TestCase):
    """画面: MV タブとショート動画タブに「道具を入れる」ボタンがあり、文言は英語の辞書にある。"""
    WEB = main.REPO_ROOT / "web"

    def test_buttons_and_labels(self):
        html = (self.WEB / "index.html").read_text(encoding="utf-8")
        i18n_js = (self.WEB / "i18n.js").read_text(encoding="utf-8")
        for pre in ("mv", "sh"):
            for suffix in ("-tools", "-tools-text", "-tools-install", "-tools-progress"):
                self.assertIn(f'id="{pre}{suffix}"', html)
        self.assertIn('"道具を入れる(約 1.3GB)": "Install tools (about 1.3GB)"', i18n_js)
        self.assertIn('"入れています…":', i18n_js)
        project = (self.WEB / "project.js").read_text(encoding="utf-8")
        self.assertIn('"/api/mv/tools/install", "POST"', project)
        self.assertIn("setTimeout(reload, 2000)", project)
        for js in ("mv.js", "short.js"):
            self.assertIn("renderToolsBox(", (self.WEB / js).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
