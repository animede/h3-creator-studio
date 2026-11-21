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


if __name__ == "__main__":
    unittest.main()
