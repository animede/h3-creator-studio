"""「使い方」の窓の API(GET /api/help)と、説明の画像(/docs/img/)の配信。lifespan は使わない(ファイルを読むだけの口)。"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from studio import main
from studio.config import REPO_ROOT


class HelpApiTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.md = self.dir / "USAGE.md"
        p = mock.patch.object(main, "HELP_MD", self.md)
        p.start()
        self.addCleanup(p.stop)
        self.client = TestClient(main.app)

    def test_returns_the_file_as_utf8_with_mtime(self):
        text = "# 使い方\n\n## 画像タブ\n\n日本語の説明 **太字**\n"
        self.md.write_text(text, encoding="utf-8")
        r = self.client.get("/api/help")
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertEqual(sorted(j), ["markdown", "mtime"])
        self.assertEqual(j["markdown"], text)
        self.assertEqual(j["mtime"], self.md.stat().st_mtime)

    def test_reads_the_file_on_every_request(self):
        self.md.write_text("# 1", encoding="utf-8")
        self.assertEqual(self.client.get("/api/help").json()["markdown"], "# 1")
        self.md.write_text("# 2", encoding="utf-8")
        self.assertEqual(self.client.get("/api/help").json()["markdown"], "# 2")

    def test_404_with_a_japanese_detail_when_missing(self):
        r = self.client.get("/api/help")
        self.assertEqual(r.status_code, 404)
        self.assertIn("見つかりません", r.json()["detail"])


class HelpImagesTest(unittest.TestCase):
    def test_the_images_are_served_read_only_under_docs_img(self):
        client = TestClient(main.app)
        name = "screen-mv.png"
        self.assertTrue((REPO_ROOT / "docs" / "img" / name).is_file())
        r = client.get(f"/docs/img/{name}")
        self.assertEqual((r.status_code, r.headers["content-type"]), (200, "image/png"))
        self.assertEqual(client.get("/docs/img/nope.png").status_code, 404)
        self.assertEqual(client.put(f"/docs/img/{name}", content=b"x").status_code, 405)
        self.assertEqual(client.delete(f"/docs/img/{name}").status_code, 405)

    def test_quiet_polling_list_has_the_help_paths(self):
        for p in ("/api/help", "/help.js", "/docs/img/"):
            self.assertIn(p, main._QuietPolling._QUIET)
