"""サンプル(samples/<id>/ の同梱の見本): 一覧・読み込み(素材のギャラリーへの 1 回だけの登録)・不正なサンプルの扱い・画面の配線。
実物の samples/ の 2 つ(short-harbor-day / short-lighthouse-dawn)を使う。"""
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from studio import main, media, presets, samples
from studio.config import REPO_ROOT
from studio.ledger import Ledger

WEB = REPO_ROOT / "web"
HARBOR, LIGHTHOUSE = "short-harbor-day", "short-lighthouse-dawn"


class LoadSamplesTest(unittest.TestCase):
    def test_the_two_real_bundles_load(self):
        found = {s["id"]: s for s in samples.load_samples()}
        self.assertEqual(sorted(found), [HARBOR, LIGHTHOUSE])
        self.assertEqual({s["tab"] for s in found.values()}, {"short"})
        self.assertEqual(found[HARBOR]["assets"], [])
        self.assertEqual([(a["role"], a["kind"]) for a in found[LIGHTHOUSE]["assets"]], [("scenery", "image"), ("song", "audio")])
        for s in found.values():
            self.assertTrue(s["values"]["story"])
            self.assertTrue((s["dir"] / s["video"]).is_file() and (s["dir"] / s["thumb"]).is_file())

    def test_broken_bundles_are_skipped_with_a_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            good = base / "good"
            good.mkdir()
            (good / "thumb.jpg").write_bytes(b"x")
            (good / "sample.json").write_text(json.dumps({"id": "good", "tab": "short", "title": "良い", "values": {"a": 1}, "thumb": "thumb.jpg"}), encoding="utf-8")
            bad = {
                "wrong-tab": {"id": "wrong-tab", "tab": "nope", "title": "t"},
                "wrong-id": {"id": "other", "tab": "short", "title": "t"},
                "no-file": {"id": "no-file", "tab": "short", "title": "t", "assets": [{"role": "song", "kind": "audio", "file": "missing.mp3"}]},
                "bad-role": {"id": "bad-role", "tab": "short", "title": "t", "assets": [{"role": "song", "kind": "image", "file": "x.png"}]},
                "escape": {"id": "escape", "tab": "short", "title": "t", "thumb": "../good/thumb.jpg"},
                "no-title": {"id": "no-title", "tab": "short"},
            }
            for name, data in bad.items():
                (base / name).mkdir()
                (base / name / "sample.json").write_text(json.dumps(data), encoding="utf-8")
            (base / "broken").mkdir()
            (base / "broken" / "sample.json").write_text("{not json", encoding="utf-8")
            with self.assertLogs("studio.samples", level="WARNING") as logs:
                found = samples.load_samples(base)
            self.assertEqual([s["id"] for s in found], ["good"])
            self.assertEqual(len(logs.output), len(bad) + 1)

    def test_a_missing_directory_gives_an_empty_list(self):
        self.assertEqual(samples.load_samples(Path("/nonexistent/samples")), [])


class SamplesApiTest(unittest.TestCase):
    """lifespan は使わず、必要な app.state だけを差し替える(台帳は実物、一時のフォルダ)。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.ledger = Ledger(self.root)
        self.addCleanup(self.ledger.close)
        p = mock.patch.object(media, "probe", return_value={"duration_s": 12.5, "width": None, "height": None, "fps": None})      # ffprobe に頼らない
        p.start()
        self.addCleanup(p.stop)
        self.saved = {k: getattr(main.app.state, k, None) for k in ("ledger", "samples")}
        self.addCleanup(lambda: [setattr(main.app.state, k, v) for k, v in self.saved.items()])
        main.app.state.ledger, main.app.state.samples = self.ledger, samples.load_samples()
        self.client = TestClient(main.app)

    def test_list_shape(self):
        r = self.client.get("/api/samples?tab=short")
        self.assertEqual(r.status_code, 200)
        by_id = {s["id"]: s for s in r.json()}
        self.assertEqual(sorted(by_id), [HARBOR, LIGHTHOUSE])
        s = by_id[LIGHTHOUSE]
        self.assertEqual(sorted(s), ["assets", "description", "id", "license", "made", "tab", "thumb_url", "title", "values", "video_url"])
        self.assertEqual((s["tab"], s["video_url"], s["thumb_url"]), ("short", f"/samples/{LIGHTHOUSE}/sample.mp4", f"/samples/{LIGHTHOUSE}/thumb.jpg"))
        self.assertEqual(s["values"]["quality"], "fast")
        a = s["assets"][0]
        self.assertEqual(sorted(a), ["description", "file", "kind", "params", "role", "url"])
        self.assertEqual((a["role"], a["kind"], a["url"]), ("scenery", "image", f"/samples/{LIGHTHOUSE}/image_06065334e62f.png"))
        self.assertNotIn("dir", s)
        self.assertEqual(by_id[HARBOR]["assets"], [])

    def test_list_filters_by_tab(self):
        for tab in ("image", "video", "music", "mv"):
            self.assertEqual(self.client.get(f"/api/samples?tab={tab}").json(), [])
        self.assertEqual(len(self.client.get("/api/samples").json()), 2)
        r = self.client.get("/api/samples?tab=nope")
        self.assertEqual(r.status_code, 400)
        self.assertIn("tab は", r.json()["detail"])

    def test_the_files_are_served_read_only_under_samples(self):
        for url, kind in ((f"/samples/{LIGHTHOUSE}/thumb.jpg", "image/jpeg"), (f"/samples/{LIGHTHOUSE}/sample.mp4", "video/mp4")):
            r = self.client.get(url)
            self.assertEqual((r.status_code, r.headers["content-type"]), (200, kind))
        self.assertEqual(self.client.get("/samples/nope/thumb.jpg").status_code, 404)
        self.assertEqual(self.client.put(f"/samples/{LIGHTHOUSE}/thumb.jpg", content=b"x").status_code, 405)

    def test_load_without_assets_returns_only_values(self):
        r = self.client.post(f"/api/samples/{HARBOR}/load")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["assets"], [])
        self.assertEqual(r.json()["values"]["length_s"], 60.0)
        self.assertEqual(self.ledger.list_outputs(), [])

    def test_load_registers_the_assets_once_and_reuses_them(self):
        r = self.client.post(f"/api/samples/{LIGHTHOUSE}/load")
        self.assertEqual(r.status_code, 200)
        js = r.json()
        self.assertEqual(js["values"]["quality"], "fast")
        self.assertEqual([a["role"] for a in js["assets"]], ["scenery", "song"])
        image, song = (a["output"] for a in js["assets"])
        self.assertEqual((image["kind"], image["tab"], image["filename"]), ("image", "short", "image_06065334e62f.png"))
        self.assertEqual((song["kind"], song["tab"], song["filename"], song["duration_s"]), ("audio", "short", "song_e34168a7e934.mp3", 12.5))
        self.assertEqual(image["params"]["sample"], LIGHTHOUSE)
        self.assertEqual(image["params"]["sample_file"], "image_06065334e62f.png")
        self.assertEqual(image["params"]["prompt"], "a lighthouse at dawn, watercolor")      # asset.params も残る
        self.assertEqual(song["params"]["theme"], "星降る夜に歌う")
        self.assertNotIn("path", image)
        self.assertTrue(image["url"].startswith("/outputs/samples/") and image["thumb_url"])
        self.assertTrue((self.root / "samples" / LIGHTHOUSE / "image_06065334e62f.png").is_file())
        self.assertEqual(len(self.ledger.list_outputs()), 2)

        again = self.client.post(f"/api/samples/{LIGHTHOUSE}/load").json()
        self.assertEqual([a["output"]["id"] for a in again["assets"]], [image["id"], song["id"]])       # 同じ生成物を使う
        self.assertEqual(len(self.ledger.list_outputs()), 2)

    def test_load_registers_again_if_the_gallery_item_was_deleted(self):
        first = self.client.post(f"/api/samples/{LIGHTHOUSE}/load").json()["assets"][0]["output"]
        self.assertTrue(self.ledger.delete_output(first["id"]))
        again = self.client.post(f"/api/samples/{LIGHTHOUSE}/load").json()["assets"]
        self.assertNotEqual(again[0]["output"]["id"], first["id"])
        self.assertEqual(len(self.ledger.list_outputs()), 2)

    def test_sample_derived_items_are_in_the_gallery_and_deletable(self):
        ids = [a["output"]["id"] for a in self.client.post(f"/api/samples/{LIGHTHOUSE}/load").json()["assets"]]
        gallery = self.client.get("/api/gallery").json()
        self.assertEqual({g["id"] for g in gallery}, set(ids))
        with mock.patch.object(main.app.state, "jobs", mock.Mock(jobs={}), create=True):
            r = self.client.post("/api/gallery/delete", json={"ids": ids})
        self.assertEqual(sorted(r.json()["deleted"]), sorted(ids))

    def test_unknown_id_is_404_in_japanese(self):
        r = self.client.post("/api/samples/no-such-sample/load")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["detail"], "そのサンプルは見つかりません")
        self.assertEqual(self.ledger.list_outputs(), [])

    def test_a_broken_asset_file_is_a_japanese_400_and_leaves_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "short-bad"
            shutil.copytree(REPO_ROOT / "samples" / LIGHTHOUSE, base)
            (base / "image_06065334e62f.png").write_bytes(b"not a png")
            data = json.loads((base / "sample.json").read_text(encoding="utf-8"))
            data["id"] = "short-bad"
            (base / "sample.json").write_text(json.dumps(data), encoding="utf-8")
            main.app.state.samples = samples.load_samples(Path(tmp))
            r = self.client.post("/api/samples/short-bad/load")
        self.assertEqual(r.status_code, 400)
        self.assertIn("image_06065334e62f.png を読み込めません", r.json()["detail"])
        self.assertEqual(self.ledger.list_outputs(), [])
        self.assertFalse((self.root / "samples" / "short-bad" / "image_06065334e62f.png").exists())


class SamplesPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (WEB / "index.html").read_text(encoding="utf-8")
        cls.ids = set(re.findall(r'id="([^"]+)"', cls.html))

    def test_every_tab_has_a_hidden_sample_box_inside_its_preset_bar(self):
        for pre in ("im", "vd", "mu", "mv", "sh"):
            self.assertIn(f'<div class="samplebox" id="{pre}-samples" hidden></div>', self.html)

    def test_samples_js_loads_after_presets_and_before_the_tabs(self):
        order = re.findall(r'<script src="([\w.]+)"></script>', self.html)
        self.assertLess(order.index("presets.js"), order.index("samples.js"))
        self.assertLess(order.index("samples.js"), min(order.index(f) for f in ("image.js", "video.js", "music.js", "mv.js", "short.js")))

    def test_samples_js_uses_the_api_and_reloads_the_gallery(self):
        js = (WEB / "samples.js").read_text(encoding="utf-8")
        self.assertIn("function wireSamples({ tab, box, apply })", js)
        self.assertIn("/api/samples?tab=", js)
        self.assertIn("/load", js)
        self.assertIn("loadGallery()", js)
        self.assertIn('v.preload = "none"', js)

    def test_the_quiet_polling_list_has_the_sample_routes(self):
        for path in ("/api/samples", "/samples/", "/samples.js"):
            self.assertIn(f'"{path}"', (REPO_ROOT / "studio" / "main.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
