import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from studio.ledger import Ledger


def make_png(path: Path, size=(640, 480)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (200, 80, 40)).save(path)
    return path


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ledger = Ledger(self.root)
        self.addCleanup(self.ledger.close)

    def test_add_image_makes_thumb(self):
        rec = self.ledger.add_output("image", make_png(self.root / "image" / "a.png"), "image", job_id="j1", params={"prompt": "x"})
        self.assertEqual((rec["width"], rec["height"]), (640, 480))
        self.assertEqual(rec["path"], "image/a.png")
        thumb = self.root / rec["thumb_path"]
        self.assertTrue(thumb.is_file())
        with Image.open(thumb) as im:
            self.assertEqual((im.format, im.width, im.height), ("JPEG", 320, 240))
        got = self.ledger.get_output(rec["id"])
        self.assertEqual(got["params"], {"prompt": "x"})
        self.assertEqual(got["job_id"], "j1")

    def test_list_filters_newest_first(self):
        a = self.ledger.add_output("image", make_png(self.root / "a.png"), "image")
        b = self.ledger.add_output("image", make_png(self.root / "b.png"), "upload")
        (self.root / "v.mp4").write_bytes(b"x")
        c = self.ledger.add_output("video", self.root / "v.mp4", "video", duration_s=3.0)
        self.assertIsNone(c["thumb_path"])
        self.assertEqual([r["id"] for r in self.ledger.list_outputs()], [c["id"], b["id"], a["id"]])
        self.assertEqual([r["id"] for r in self.ledger.list_outputs(kind="image")], [b["id"], a["id"]])
        self.assertEqual([r["id"] for r in self.ledger.list_outputs(tab="image")], [a["id"]])
        self.assertEqual(len(self.ledger.list_outputs(limit=1)), 1)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg / ffprobe が PATH に無い")
    def test_video_and_audio_thumb_and_probe(self):
        mp4, wav = self.root / "video" / "v.mp4", self.root / "audio" / "a.wav"
        mp4.parent.mkdir()
        wav.parent.mkdir()
        run = lambda *a: subprocess.run(["ffmpeg", "-v", "error", "-y", *a], check=True)
        run("-f", "lavfi", "-i", "testsrc=size=640x360:rate=24:duration=2", "-pix_fmt", "yuv420p", str(mp4))
        run("-f", "lavfi", "-i", "sine=frequency=440:duration=1.5", str(wav))
        v = self.ledger.add_output("video", mp4, "video")
        self.assertEqual((v["width"], v["height"]), (640, 360))
        self.assertAlmostEqual(v["duration_s"], 2.0, delta=0.1)
        with Image.open(self.root / v["thumb_path"]) as im:
            self.assertEqual((im.format, im.width, im.height), ("JPEG", 320, 180))
        a = self.ledger.add_output("audio", wav, "video")
        self.assertIsNone(a["thumb_path"])
        self.assertAlmostEqual(a["duration_s"], 1.5, delta=0.1)
        self.assertTrue(self.ledger.delete_output(v["id"]))
        self.assertFalse((self.root / v["thumb_path"]).exists())

    def test_missing_ffmpeg_is_not_an_error(self):
        from unittest import mock
        (self.root / "v.mp4").write_bytes(b"x")
        with mock.patch("studio.media.shutil.which", return_value=None):
            rec = self.ledger.add_output("video", self.root / "v.mp4", "video")
        self.assertEqual((rec["thumb_path"], rec["duration_s"], rec["width"]), (None, None, None))

    def test_delete_removes_files(self):
        rec = self.ledger.add_output("image", make_png(self.root / "image" / "a.png"), "image")
        self.assertTrue(self.ledger.delete_output(rec["id"]))
        self.assertFalse((self.root / "image" / "a.png").exists())
        self.assertFalse((self.root / rec["thumb_path"]).exists())
        self.assertIsNone(self.ledger.get_output(rec["id"]))
        self.assertFalse(self.ledger.delete_output(rec["id"]))

    def test_rejects_path_outside_root(self):
        with self.assertRaises(ValueError):
            self.ledger.add_output("video", "/etc/hostname", "video")
        with self.assertRaises(ValueError):
            self.ledger.abs_path("../x")

    def test_save_and_get_job(self):
        from studio.jobs import Job
        job = Job(id="abc", tab="image", kind="h3image.t2i", params={"prompt": "p"}, status="completed", finished=5.0)
        self.ledger.save_job(job)
        got = self.ledger.get_job("abc")
        self.assertEqual((got["status"], got["params"], got["progress"]), ("completed", {"prompt": "p"}, 1.0))
        self.assertEqual([j["id"] for j in self.ledger.list_jobs("image")], ["abc"])
        self.assertEqual(self.ledger.list_jobs("music"), [])

    def test_mv_project_crud(self):
        scenes = [{"index": 1, "status": "pending", "prompt": ""}, {"index": 2, "status": "pending", "prompt": ""}]
        rec = self.ledger.create_mv_project("p1", "analyzing", {"song_id": "s1", "aspect": "16:9"}, scenes, "分析します")
        self.assertEqual((rec["state"], rec["message"], rec["error"], rec["output_id"]), ("analyzing", "分析します", None, None))
        self.assertEqual((rec["params"], rec["scenes"]), ({"song_id": "s1", "aspect": "16:9"}, scenes))
        self.assertLessEqual(rec["created_at"], rec["updated_at"])
        # 渡した項目だけが変わる: 列の置き換え、params への混ぜ込み、シーンごとの混ぜ込み
        got = self.ledger.update_mv_project("p1", state="generating", message="シーン 2 / 2", error=None,
                                            params_patch={"analysis": {"duration": 12.0}}, scene_patches={2: {"status": "running", "seed": 7}})
        self.assertEqual((got["state"], got["message"]), ("generating", "シーン 2 / 2"))
        self.assertEqual(got["params"], {"song_id": "s1", "aspect": "16:9", "analysis": {"duration": 12.0}})
        self.assertEqual([(s["status"], s.get("seed")) for s in got["scenes"]], [("pending", None), ("running", 7)])
        self.assertGreaterEqual(got["updated_at"], rec["updated_at"])
        got = self.ledger.update_mv_project("p1", output_id="o1", error="失敗した", scenes=[{"index": 1, "status": "done"}], params={"x": 1})
        self.assertEqual((got["output_id"], got["error"], got["scenes"], got["params"]), ("o1", "失敗した", [{"index": 1, "status": "done"}], {"x": 1}))
        self.assertEqual(self.ledger.get_mv_project("p1"), got)
        self.assertIsNone(self.ledger.update_mv_project("nope", state="failed"))
        with self.assertRaises(ValueError):
            self.ledger.update_mv_project("p1", bogus=1)

    def test_mv_projects_newest_first_and_delete(self):
        from unittest import mock
        with mock.patch("studio.ledger.time.time", side_effect=[100.0, 200.0]):
            self.ledger.create_mv_project("old", "paused", {}, [], "")
            self.ledger.create_mv_project("new", "completed", {}, [], "")
        self.assertEqual([p["id"] for p in self.ledger.list_mv_projects()], ["new", "old"])
        self.assertEqual(len(self.ledger.list_mv_projects(limit=1)), 1)
        self.assertTrue(self.ledger.delete_mv_project("old"))
        self.assertFalse(self.ledger.delete_mv_project("old"))
        self.assertIsNone(self.ledger.get_mv_project("old"))
        self.assertEqual([p["id"] for p in self.ledger.list_mv_projects()], ["new"])

    def test_preset_crud(self):
        from unittest import mock
        with mock.patch("studio.ledger.time.time", side_effect=[100.0, 200.0, 300.0, 400.0]):
            a = self.ledger.create_preset("image", "A", {"prompt": "x", "width": 768, "tags": ["a", 1], "seed": None})
            b = self.ledger.create_preset("music", "あ", {"genre": "Jazz"})
            self.ledger.update_preset(a["id"], values={"prompt": "y"})
        self.assertEqual((a["tab"], a["name"], a["created_at"], a["updated_at"]), ("image", "A", 100.0, 100.0))
        self.assertEqual(a["values"], {"prompt": "x", "width": 768, "tags": ["a", 1], "seed": None})
        self.assertEqual(self.ledger.get_preset(b["id"])["name"], "あ")                 # 日本語もそのまま入る
        got = self.ledger.get_preset(a["id"])
        self.assertEqual((got["name"], got["values"], got["created_at"], got["updated_at"]), ("A", {"prompt": "y"}, 100.0, 300.0))    # 値だけ変わる
        got = self.ledger.update_preset(a["id"], name="A2")
        self.assertEqual((got["name"], got["values"]), ("A2", {"prompt": "y"}))                                                   # 名前だけ変わる
        self.assertEqual([p["id"] for p in self.ledger.list_presets()], [a["id"], b["id"]])                                       # 作った順
        self.assertEqual([p["id"] for p in self.ledger.list_presets("music")], [b["id"]])
        self.assertEqual(self.ledger.list_presets("mv"), [])
        self.assertIsNone(self.ledger.update_preset("nope", name="x"))
        self.assertTrue(self.ledger.delete_preset(a["id"]))
        self.assertFalse(self.ledger.delete_preset(a["id"]))
        self.assertIsNone(self.ledger.get_preset(a["id"]))
        self.assertEqual([p["id"] for p in self.ledger.list_presets()], [b["id"]])

    def test_presets_survive_reopening_the_ledger(self):
        made = self.ledger.create_preset("video", "V", {"seconds": 5})
        self.ledger.close()
        self.ledger = Ledger(self.root)                                                 # 既存の台帳を開き直しても表がある(IF NOT EXISTS)
        self.addCleanup(self.ledger.close)
        self.assertEqual(self.ledger.get_preset(made["id"])["values"], {"seconds": 5})


if __name__ == "__main__":
    unittest.main()
