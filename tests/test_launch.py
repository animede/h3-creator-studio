"""デスクトップのアイコンからの起動(studio.main の --open): URL、既に動いているかの確かめ、準備ができたらブラウザで開く。"""
import unittest
from unittest import mock

import httpx

from studio import main


class LaunchTest(unittest.TestCase):
    def test_url(self):
        self.assertEqual(main.studio_url("127.0.0.1", 8700), "http://127.0.0.1:8700/")
        self.assertEqual(main.studio_url("0.0.0.0", 8702), "http://127.0.0.1:8702/")

    def test_running_studio_needs_our_status(self):
        ok = httpx.Response(200, json={"components": {"h3": {}}}, request=httpx.Request("GET", "http://x"))
        other = httpx.Response(200, json={"status": "ok"}, request=httpx.Request("GET", "http://x"))
        with mock.patch.object(httpx, "get", return_value=ok) as get:
            self.assertTrue(main.running_studio("http://127.0.0.1:8700/"))
            self.assertEqual(get.call_args.args[0], "http://127.0.0.1:8700/api/status")
        with mock.patch.object(httpx, "get", return_value=other):
            self.assertFalse(main.running_studio("http://127.0.0.1:8700/"))          # 同じポートの別のアプリ
        with mock.patch.object(httpx, "get", side_effect=httpx.ConnectError("refused")):
            self.assertFalse(main.running_studio("http://127.0.0.1:8700/"))

    def test_open_when_ready(self):
        opened = []
        answers = iter([False, False, True])
        with mock.patch.object(main, "running_studio", lambda url, timeout: next(answers)), mock.patch.object(main.time, "sleep"):
            self.assertTrue(main.open_when_ready("http://127.0.0.1:8700/", open_browser=opened.append))
        self.assertEqual(opened, ["http://127.0.0.1:8700/"])
        with mock.patch.object(main, "running_studio", lambda url, timeout: False), mock.patch.object(main.time, "sleep"):
            self.assertFalse(main.open_when_ready("http://x/", wait_s=0, open_browser=opened.append))
        self.assertEqual(len(opened), 1)


class VersionTest(unittest.TestCase):
    def test_one_version_everywhere(self):
        import re
        from studio import __version__
        from studio.config import REPO_ROOT
        pyproject = (REPO_ROOT / "pyproject.toml").read_text("utf-8")
        self.assertEqual(re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1), __version__)
        self.assertEqual(main.app.version, __version__)
        self.assertIn(f"v{__version__}", (REPO_ROOT / "README.md").read_text("utf-8").splitlines()[6])     # 最新版の行
        self.assertIn('"version": __version__', (REPO_ROOT / "studio" / "main.py").read_text("utf-8"))
        self.assertIn('$("app-version")', (REPO_ROOT / "web" / "app.js").read_text("utf-8"))


if __name__ == "__main__":
    unittest.main()
