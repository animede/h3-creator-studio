"""ギャラリーの動画・画像を直接 AI で拡大する(POST /api/gallery/{id}/upscale と handlers/upscale.py の upscale.output、studio/upscale.py の画像の拡大)。
道具は偽物(子プロセスは Python、実行は差し替え)。台帳は実物。"""
import asyncio
import importlib.util
import io
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient
from PIL import Image

from studio import i18n, main, media, mv_audio, upscale
from studio.handlers import HANDLERS
from studio.handlers import upscale as hu
from studio.jobs import Ctx, Job, JobCancelled, JobFailed
from studio.ledger import Ledger
from tests.test_upscale import JP_RE, fake_status

JOB_IN_USE = "実行中・順番待ちのジョブで使っています"


def png_bytes(size=(64, 48), mode="RGB", fmt="PNG", exif=None) -> bytes:
    buf = io.BytesIO()
    im = Image.new(mode, size, (10, 120, 200) if mode == "RGB" else 1)
    if exif is not None:
        im.save(buf, fmt, exif=exif)
    else:
        im.save(buf, fmt)
    return buf.getvalue()


class Base(unittest.TestCase):
    """台帳(実物)に動画・画像・曲・アップロードの動画を登録しておく。ffprobe とサムネイルは使わない。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.ledger = Ledger(self.root)
        self.addCleanup(self.ledger.close)
        self.probe = {"duration_s": None, "width": None, "height": None, "fps": None}
        for name, kw in (("probe", {"side_effect": lambda path: dict(self.probe)}), ("video_thumb", {"return_value": False})):
            p = mock.patch.object(media, name, **kw)
            p.start()
            self.addCleanup(p.stop)
        self.video = self.add("video", "video/h3_abc.mp4", "夕焼け.mp4", duration_s=5.0, width=1280, height=720, fps=24.0)
        self.upload = self.add("video", "uploads/0f0f.webm", "clip.webm", tab="upload", duration_s=3.0, width=511, height=509, fps=30.0)
        self.image = self.add("image", "image/h3i_1.png", "cat.png", size=(640, 360))
        self.song = self.add("audio", "music/s.wav", "s.wav", duration_s=30.0)
        self.addCleanup(hu._procs.clear)
        self.addCleanup(hu._taken.clear)

    def add(self, kind, rel, filename, tab=None, size=(64, 48), **kw) -> dict:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(png_bytes(size) if kind == "image" else b"fakemedia")
        return self.ledger.add_output(kind, p, tab or kind, filename=filename, params={}, **kw)


class StubRunner:
    """JobRunner の代わり: submit は本物のハンドラの normalize を通して、ジョブを残すだけ(走らせない)。"""

    def __init__(self, ledger):
        self.ledger, self.jobs = ledger, {}

    def submit(self, tab, kind, params):
        handler = HANDLERS.get(kind)
        if handler is None:
            raise ValueError(f"未対応のジョブの種類です: {kind}")
        job = Job(id=f"job{len(self.jobs) + 1}", tab=tab, kind=kind, params=handler.normalize(kind, params, self.ledger))
        self.jobs[job.id] = job
        return job


class ApiTest(Base):
    def setUp(self):
        super().setUp()
        self.runner = StubRunner(self.ledger)
        saved = {k: getattr(main.app.state, k, None) for k in ("ledger", "jobs")}
        self.addCleanup(lambda: [setattr(main.app.state, k, v) for k, v in saved.items()])
        main.app.state.ledger, main.app.state.jobs = self.ledger, self.runner
        for name, kw in (("status", {"return_value": fake_status(realesrgan=True, rtxvsr=True)}), ("max_edge", {"return_value": 3840})):
            p = mock.patch.object(upscale, name, **kw)
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(main.app)

    def post(self, oid, body=None, **kw):
        return self.client.post(f"/api/gallery/{oid}/upscale", json=body, **kw)

    def test_video_accepted_with_auto(self):
        r = self.post(self.video["id"], {"method": "auto", "scale": 2})
        self.assertEqual(r.status_code, 202, r.text)
        self.assertEqual(r.json(), {"id": "job1", "method": "rtxvsr", "scale": 2, "width": 2560, "height": 1440})
        job = self.runner.jobs["job1"]
        self.assertEqual((job.tab, job.kind), ("video", "upscale.output"))
        self.assertEqual(job.params, {"output_id": self.video["id"], "kind": "video", "method": "rtxvsr", "scale": 2, "width": 2560, "height": 1440, "fps": 24.0})

    def test_image_accepted_defaults_and_tab(self):
        r = self.post(self.image["id"], {"method": "realesrgan", "scale": "4"})
        self.assertEqual(r.status_code, 202, r.text)
        self.assertEqual(r.json(), {"id": "job1", "method": "realesrgan", "scale": 4, "width": 2560, "height": 1440})
        job = self.runner.jobs["job1"]
        self.assertEqual((job.tab, job.params["kind"], job.params["fps"]), ("image", "image", None))
        self.assertEqual(self.post(self.image["id"], {"scale": 2}).json()["method"], "rtxvsr")          # 方式を省くと auto
        self.assertEqual(self.runner.jobs["job2"].tab, "image")
        self.post(self.image["id"], {"scale": 2, "tab": "video"})                                          # image / video なら頼まれたタブ
        self.assertEqual(self.runner.jobs["job3"].tab, "video")
        self.post(self.video["id"], {"scale": 2, "tab": "music"})                                          # ほかは生成物の種類
        self.assertEqual(self.runner.jobs["job4"].tab, "video")

    def test_odd_upload_is_floored_to_even_for_video_and_kept_for_images(self):
        r = self.post(self.upload["id"], {"method": "realesrgan", "scale": 3})
        self.assertEqual(r.json(), {"id": "job1", "method": "realesrgan", "scale": 3, "width": 1532, "height": 1526})    # 1533×1527 → 偶数
        self.assertEqual(self.runner.jobs["job1"].params["fps"], 30.0)
        odd = self.add("image", "uploads/odd.png", "odd.png", tab="upload", size=(511, 509))
        self.assertEqual(self.post(odd["id"], {"method": "realesrgan", "scale": 3}).json()["width"], 1533)

    def test_missing_size_and_fps_are_probed(self):
        bare = self.add("video", "uploads/bare.mp4", "bare.mp4", tab="upload")          # ffprobe が無かった時の取り込み: 寸法もコマ率も無い
        self.assertIsNone(bare["width"])
        r = self.post(bare["id"], {"method": "realesrgan", "scale": 2})
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "生成物の大きさ(幅・高さ)が分かりません"))
        self.probe.update(width=640, height=360, fps=None)
        r = self.post(bare["id"], {"method": "realesrgan", "scale": 2})
        self.assertEqual((r.status_code, r.json()["width"]), (202, 1280))
        self.assertEqual(self.runner.jobs[r.json()["id"]].params["fps"], 24.0)                 # コマ率が分からなければ 24

    def test_404_and_400s(self):
        r = self.post("nope", {"scale": 2})
        self.assertEqual((r.status_code, r.json()["detail"]), (404, "その生成物は見つかりません"))
        r = self.post(self.song["id"], {"scale": 2})
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "拡大できるのは動画と画像だけです"))
        for body in ({"scale": 1}, {"scale": "x"}, {"scale": 2.5}, {}, None, {"method": "realesrgan", "scale": 5}):
            r = self.post(self.video["id"], body)
            self.assertEqual(r.status_code, 400, body)
            self.assertIn("倍率", r.json()["detail"], body)
        r = self.post(self.video["id"], {"method": "waifu", "scale": 2})
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "拡大の方式が不明です: waifu"))
        r = self.post(self.video["id"], {"method": "realesrgan", "scale": 4})
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "拡大後の長辺が 3840px を超えます(5120×2880)。大きさを下げてください"))
        with mock.patch.object(upscale, "status", return_value=fake_status(realesrgan=False, rtxvsr=False)):
            r = self.post(self.video["id"], {"method": "auto", "scale": 2})
            self.assertEqual(r.status_code, 400)
            self.assertIn("拡大の方式が入っていません", r.json()["detail"])
        with mock.patch.object(upscale, "status", return_value=fake_status(realesrgan=True, rtxvsr=False, hint_rtx="RTX の条件")):
            r = self.post(self.video["id"], {"method": "rtxvsr", "scale": 2})
            self.assertEqual((r.status_code, r.json()["detail"]), (400, "RTX の条件"))
        (self.root / self.image["path"]).unlink()
        r = self.post(self.image["id"], {"scale": 2})
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "生成物のファイルが見つかりません(ギャラリーから削除されたかもしれません)"))
        self.assertEqual(self.runner.jobs, {})

    def test_english_messages(self):
        en = {"Accept-Language": "en"}
        self.assertEqual(self.post(self.song["id"], {"scale": 2}, headers=en).json()["detail"], "Only videos and images can be upscaled")
        self.assertEqual(self.post("nope", {"scale": 2}, headers=en).json()["detail"], "That output was not found")
        self.assertNotRegex(self.post(self.video["id"], {"method": "realesrgan", "scale": 4}, headers=en).json()["detail"], JP_RE)
        for text in (hu.MSG_NO_FILE, hu.MSG_NO_SIZE, hu.MSG_NOT_FOUND, hu.MSG_REGISTER, "拡大後の幅・高さを正の整数で指定してください",
                     upscale._msg_upscaling_image(upscale.METHODS["realesrgan"]["ja"], 2), upscale._msg_upscaling_image("RTX Video Super Resolution", 4)):
            self.assertNotRegex(i18n.tr(text, "en"), JP_RE, text)
        self.assertEqual(i18n.tr(upscale._msg_upscaling_image(upscale.METHODS["realesrgan"]["ja"], 2), "en"), "Upscaling (Real-ESRGAN (anime) ×2)")

    def test_not_accepted_by_the_generic_jobs_api(self):
        r = self.client.post("/api/jobs", json={"tab": "video", "kind": "upscale.output", "params": {"output_id": self.video["id"]}})
        self.assertEqual(r.status_code, 400)
        self.assertFalse("upscale.output".startswith(main.PUBLIC_JOB_PREFIXES))
        self.assertEqual(self.runner.jobs, {})

    def test_queued_job_protects_its_source_from_deletion(self):
        oid = self.post(self.video["id"], {"method": "realesrgan", "scale": 2}).json()["id"]
        r = self.client.delete(f"/api/gallery/{self.video['id']}")
        self.assertEqual((r.status_code, r.json()["detail"]), (409, JOB_IN_USE))
        self.runner.jobs[oid].status = "completed"
        self.assertEqual(self.client.delete(f"/api/gallery/{self.video['id']}").status_code, 200)


class NormalizeTest(Base):
    def params(self, **kw) -> dict:
        return {"output_id": self.video["id"], "kind": "video", "method": "realesrgan", "scale": 2, "width": 2560, "height": 1440, "fps": 24, **kw}

    def test_shape(self):
        n = hu.normalize("upscale.output", self.params(method="RTXVSR", scale="3", width="3840", fps="29.97"), self.ledger)
        self.assertEqual(n, {"output_id": self.video["id"], "kind": "video", "method": "rtxvsr", "scale": 3, "width": 3840, "height": 1440, "fps": 29.97})
        self.assertEqual(hu.normalize("upscale.output", self.params(fps="x"), self.ledger)["fps"], 24.0)
        img = hu.normalize("upscale.output", self.params(output_id=self.image["id"], kind="image", fps=30), self.ledger)
        self.assertIsNone(img["fps"])
        for bad, needle in ((self.params(output_id="nope"), "その生成物は見つかりません"), (self.params(output_id=""), "その生成物は見つかりません"),
                            (self.params(kind="image"), "拡大できるのは動画と画像だけです"),
                            (self.params(output_id=self.song["id"], kind="audio"), "拡大できるのは動画と画像だけです"),
                            (self.params(method="auto"), "拡大の方式が不明です"), (self.params(scale=1), "倍率"),
                            (self.params(width=0), "拡大後の幅・高さ"), (self.params(height="x"), "拡大後の幅・高さ")):
            with self.assertRaisesRegex(ValueError, needle):
                hu.normalize("upscale.output", bad, self.ledger)

    def test_record_and_registration(self):
        self.assertEqual(hu.record(self.params()), {"output_id": self.video["id"], "method": "realesrgan", "scale": 2})
        h = HANDLERS["upscale.output"]
        self.assertIs(h, hu.HANDLERS["upscale.output"])
        self.assertEqual((h.component, h.gpu, h.cancel, h.record), (None, False, hu.cancel, hu.record))


class RunJobTest(Base):
    """handlers/upscale.py の run(拡大の実行は差し替え)。"""

    def setUp(self):
        super().setUp()
        self.busy = {}
        self.ctx = Ctx(types.SimpleNamespace(_busy=self.busy), self.ledger, self.root)
        self.calls = []
        p = mock.patch.object(mv_audio, "_ffmpeg", return_value="ffmpeg")
        p.start()
        self.addCleanup(p.stop)

    def job(self, rec, method="realesrgan", scale=2, tab=None, **kw) -> Job:
        params = hu.plan(self.ledger, rec, method, scale, None, st=fake_status(realesrgan=True, rtxvsr=True))
        return Job(id=f"j{len(self.calls)}{method}{scale}", tab=tab or rec["kind"], kind="upscale.output",
                   params=hu.normalize("upscale.output", {**params, **kw}, self.ledger))

    def fake_video(self, fail=None):
        """upscale.run_realesrgan / run_rtxvsr の代わり: 引数と、呼ばれた時点の job.message を残し、dst を書く。"""
        def run(job, tool, src, dst, scale, fps, work, ffmpeg, procs, *rest, audio=None):
            self.calls.append({"tool": tool, "src": Path(src), "dst": Path(dst), "scale": scale, "fps": fps, "work": Path(work), "ffmpeg": ffmpeg,
                               "procs": procs, "rest": rest, "audio": audio, "message": job.message})
            Path(work).mkdir(parents=True, exist_ok=True)
            if fail is not None:
                Path(dst).write_bytes(b"half")
                raise fail
            Path(dst).write_bytes(b"upscaled")
            return Path(dst)
        return run

    def fake_image(self, size=(1280, 720)):
        def run(job, tool, src, dst, scale, procs, *rest):
            self.calls.append({"tool": tool, "src": Path(src), "dst": Path(dst), "scale": scale, "procs": procs, "rest": rest})
            Path(dst).write_bytes(png_bytes(size))
            return Path(dst)
        return run

    def go(self, job):
        asyncio.run(hu.run(job, self.ctx))

    def test_video_registers_the_output_next_to_the_videos_without_overwriting(self):
        job = self.job(self.video)
        with mock.patch.object(upscale, "run_realesrgan", self.fake_video()), mock.patch.object(upscale, "realesrgan_exe", return_value=Path("/t/exe")):
            self.go(job)
        (call,) = self.calls
        self.assertEqual((call["tool"], call["src"], call["dst"], call["scale"], call["fps"], call["work"], call["ffmpeg"], call["audio"]),
                         (Path("/t/exe"), self.root / "video/h3_abc.mp4", self.root / "video/h3_abc_x2.mp4", 2, 24.0,
                          self.root / "upscale" / job.id, "ffmpeg", "copy"))
        self.assertIs(call["procs"], hu._procs)
        (oid,) = job.outputs
        rec = self.ledger.get_output(oid)
        self.assertEqual((rec["kind"], rec["path"], rec["filename"], rec["tab"], rec["job_id"], rec["width"], rec["height"], rec["duration_s"], rec["fps"]),
                         ("video", "video/h3_abc_x2.mp4", "夕焼け_x2.mp4", "video", job.id, 2560, 1440, 5.0, 24.0))
        self.assertEqual(rec["params"], {"upscale": {"method": "realesrgan", "scale": 2, "source_output_id": self.video["id"]}, "source_filename": "夕焼け.mp4"})
        self.assertEqual((job.progress, job.message), (0.99, "ギャラリーに登録しています"))
        self.assertFalse((self.root / "upscale" / job.id).exists())                                   # 作業フォルダは消す
        self.assertEqual((hu._taken, (self.root / "video/h3_abc.mp4").read_bytes()), (set(), b"fakemedia"))    # 元は触らない
        # もう一度: 前の拡大を上書きしない。rtxvsr は道具の Python と品質を渡す
        job2 = self.job(self.video, "rtxvsr", 2)
        with mock.patch.object(upscale, "run_rtxvsr", self.fake_video()), mock.patch.object(upscale, "rtxvsr_python", return_value=Path("/t/py")):
            self.go(job2)
        self.assertEqual((self.calls[1]["tool"], self.calls[1]["dst"].name, self.calls[1]["rest"]), (Path("/t/py"), "h3_abc_x2_2.mp4", ("ULTRA",)))
        self.assertEqual(self.ledger.get_output(job2.outputs[0])["params"]["upscale"]["method"], "rtxvsr")

    def test_upload_goes_to_outputs_video_and_reencodes_non_mp4_audio(self):
        job = self.job(self.upload, "realesrgan", 3, tab="video")
        with mock.patch.object(upscale, "run_realesrgan", self.fake_video()):
            self.go(job)
        self.assertEqual((self.calls[0]["dst"], self.calls[0]["audio"], self.calls[0]["fps"]), (self.root / "video/0f0f_x3.mp4", "aac", 30.0))
        rec = self.ledger.get_output(job.outputs[0])
        self.assertEqual((rec["path"], rec["filename"], rec["width"], rec["height"]), ("video/0f0f_x3.mp4", "clip_x3.mp4", 1532, 1526))

    def test_image(self):
        job = self.job(self.image, "rtxvsr", 2)
        with mock.patch.object(upscale, "run_rtxvsr_image", self.fake_image()), mock.patch.object(upscale, "rtxvsr_python", return_value=Path("/t/py")):
            self.go(job)
        (call,) = self.calls
        self.assertEqual((call["tool"], call["src"], call["dst"], call["scale"], call["rest"]),
                         (Path("/t/py"), self.root / "image/h3i_1.png", self.root / "image/h3i_1_x2.png", 2, ("ULTRA",)))
        self.assertIs(call["procs"], hu._procs)
        rec = self.ledger.get_output(job.outputs[0])
        self.assertEqual((rec["kind"], rec["path"], rec["filename"], rec["tab"], rec["width"], rec["height"], rec["duration_s"], rec["fps"]),
                         ("image", "image/h3i_1_x2.png", "cat_x2.png", "image", 1280, 720, None, None))
        self.assertEqual(rec["params"]["upscale"], {"method": "rtxvsr", "scale": 2, "source_output_id": self.image["id"]})
        self.assertTrue((self.root / rec["thumb_path"]).is_file())
        job2 = self.job(self.image, "realesrgan", 2)
        with mock.patch.object(upscale, "run_realesrgan_image", self.fake_image()), mock.patch.object(upscale, "realesrgan_exe", return_value=Path("/t/exe")):
            self.go(job2)
        self.assertEqual((self.calls[1]["tool"], self.calls[1]["dst"].name, self.calls[1]["rest"]), (Path("/t/exe"), "h3i_1_x2_2.png", ()))

    def test_waits_for_the_gpu_engines_and_can_be_cancelled_while_waiting(self):
        self.busy["h3"] = 1
        job = self.job(self.video)

        async def go():
            asyncio.get_running_loop().call_later(0.05, self.busy.clear)                         # 生成が終わる
            with mock.patch.object(upscale, "run_realesrgan", self.fake_video()):
                await hu.run(job, self.ctx)
        with mock.patch.object(upscale, "GPU_WAIT_POLL_S", 0.01):
            asyncio.run(go())
        self.assertEqual(self.calls[0]["message"], "GPU の生成が終わるのを待っています")
        self.assertEqual(len(job.outputs), 1)
        self.busy["h3"] = 1
        job2 = self.job(self.video)

        async def cancel_while_waiting():
            asyncio.get_running_loop().call_later(0.03, lambda: setattr(job2, "cancel_requested", True))
            with mock.patch.object(upscale, "run_realesrgan", self.fake_video()):
                await hu.run(job2, self.ctx)
        with mock.patch.object(upscale, "GPU_WAIT_POLL_S", 0.01), self.assertRaises(JobCancelled):
            asyncio.run(cancel_while_waiting())
        self.assertEqual((len(self.calls), job2.outputs, hu._taken), (1, [], set()))

    def test_failure_and_cancel_leave_nothing(self):
        boom = JobFailed("拡大に失敗しました(Real-ESRGAN(アニメ向け)): boom", "tail")
        job = self.job(self.video)
        with mock.patch.object(upscale, "run_realesrgan", self.fake_video(fail=boom)), self.assertRaises(JobFailed) as cm:
            self.go(job)
        self.assertEqual((cm.exception.message, cm.exception.log_tail), (boom.message, "tail"))
        self.assertFalse((self.root / "video/h3_abc_x2.mp4").exists())
        self.assertFalse((self.root / "upscale" / job.id).exists())
        self.assertEqual((job.outputs, hu._taken, len(self.ledger.list_outputs("video"))), ([], set(), 2))
        # 道具が終わった直後に中止が来た: 書いたファイルも消す
        job2 = self.job(self.video)

        def run_then_cancel(job, *a, **kw):
            out = self.fake_video()(job, *a, **kw)
            job.cancel_requested = True
            return out
        with mock.patch.object(upscale, "run_realesrgan", run_then_cancel), self.assertRaises(JobCancelled):
            self.go(job2)
        self.assertFalse((self.root / "video/h3_abc_x2.mp4").exists())
        self.assertEqual(job2.outputs, [])

    def test_source_gone_before_start(self):
        job = self.job(self.video)
        (self.root / self.video["path"]).unlink()
        with self.assertRaisesRegex(JobFailed, "生成物のファイルが見つかりません"):
            self.go(job)
        job = self.job(self.image)
        self.ledger.delete_output(self.image["id"])
        with self.assertRaisesRegex(JobFailed, "その生成物は見つかりません"):
            self.go(job)

    def test_two_jobs_at_once_pick_different_names(self):
        jobs = [self.job(self.video), self.job(self.video)]
        jobs[1].id = "other"
        started, release = threading.Event(), threading.Event()

        def slow(job, tool, src, dst, *a, **kw):
            self.calls.append(Path(dst))
            if len(self.calls) == 1:
                started.set()
                release.wait(5)
            Path(dst).write_bytes(b"x")
            return Path(dst)

        async def both():
            t1 = asyncio.create_task(hu.run(jobs[0], self.ctx))
            await asyncio.to_thread(started.wait, 5)
            t2 = asyncio.create_task(hu.run(jobs[1], self.ctx))
            await asyncio.sleep(0.05)
            release.set()
            await asyncio.gather(t1, t2)
        with mock.patch.object(upscale, "run_realesrgan", slow):
            asyncio.run(both())
        self.assertEqual(sorted(p.name for p in self.calls), ["h3_abc_x2.mp4", "h3_abc_x2_2.mp4"])


class ImageRunnerTest(unittest.TestCase):
    """studio/upscale.py の画像 1 枚の拡大(引数と、_run を差し替えた流れ、本物の子プロセスの中止)。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.src = self.dir / "uploads" / "a.png"
        self.src.parent.mkdir()
        self.src.write_bytes(png_bytes())
        self.dst = self.dir / "image" / "a_x2.png"
        self.job = Job(id="ji", tab="image", kind="upscale.output", params={})

    def test_argv(self):
        self.assertEqual(upscale.realesrgan_image_argv("/t/realesrgan/realesrgan-ncnn-vulkan", "/o/a.jpg", "/o/image/a_x2.part.png", 3),
                         ["/t/realesrgan/realesrgan-ncnn-vulkan", "-i", "/o/a.jpg", "-o", "/o/image/a_x2.part.png", "-n", "realesr-animevideov3",
                          "-s", "3", "-f", "png", "-m", "/t/realesrgan/models"])
        self.assertEqual(upscale.rtxvsr_image_argv("/t/py", "/o/a.webp", "/o/image/a_x2.part.png", 2, "HIGH"),
                         ["/t/py", str(upscale.RTXVSR_SCRIPT), "--image", "/o/a.webp", "/o/image/a_x2.part.png", "--scale", "2", "--quality", "HIGH"])
        self.assertEqual(upscale.image_part(Path("/o/image/a_x2.png")), Path("/o/image/a_x2.part.png"))

    def fake_run(self, rc=0, write=True):
        calls = []

        def run(job, argv, procs, on_line=None, tick=None, env=None):
            calls.append({"argv": list(argv), "procs": procs, "env": env, "message": job.message})
            out = Path(argv[argv.index("-o") + 1]) if "-o" in argv else Path(argv[argv.index("--image") + 2])
            if write:
                out.write_bytes(b"png")
            return rc, "line\nthe reason" if rc else ""
        return run, calls

    def test_realesrgan_image(self):
        run, calls = self.fake_run()
        procs = {}
        with mock.patch.object(upscale, "_run", run):
            self.assertEqual(upscale.run_realesrgan_image(self.job, "/t/realesrgan-ncnn-vulkan", self.src, self.dst, 2, procs), self.dst)
        (call,) = calls
        self.assertEqual(call["argv"], upscale.realesrgan_image_argv("/t/realesrgan-ncnn-vulkan", self.src, self.dir / "image/a_x2.part.png", 2))
        self.assertIs(call["procs"], procs)
        self.assertEqual(call["message"], "拡大しています(Real-ESRGAN(アニメ向け) ×2)")
        self.assertEqual((self.dst.read_bytes(), (self.dir / "image/a_x2.part.png").exists()), (b"png", False))

    def test_rtxvsr_image_and_failures(self):
        run, calls = self.fake_run()
        with mock.patch.object(upscale, "_run", run):
            upscale.run_rtxvsr_image(self.job, "/t/py", self.src, self.dst, 4, {}, "HIGH")
        self.assertEqual(calls[0]["argv"], upscale.rtxvsr_image_argv("/t/py", self.src, self.dir / "image/a_x2.part.png", 4, "HIGH"))
        self.assertEqual(calls[0]["env"]["PYTHONUNBUFFERED"], "1")
        self.assertEqual(calls[0]["message"], "拡大しています(RTX Video Super Resolution ×4)")
        self.dst.unlink()
        for rc, write in ((1, True), (0, False)):
            run, _ = self.fake_run(rc=rc, write=write)
            with mock.patch.object(upscale, "_run", run), self.assertRaises(JobFailed) as cm:
                upscale.run_rtxvsr_image(self.job, "/t/py", self.src, self.dst, 2, {})
            self.assertTrue(cm.exception.message.startswith("拡大に失敗しました(RTX Video Super Resolution): "), cm.exception.message)
            self.assertFalse(self.dst.exists())
            self.assertFalse((self.dir / "image/a_x2.part.png").exists())

    def test_cancel_kills_the_running_tool(self):
        exe = self.dir / "realesrgan-ncnn-vulkan"
        exe.write_text(f"#!{sys.executable}\nimport sys, time\nopen(sys.argv[4], 'wb').write(b'half')\ntime.sleep(60)\n")
        exe.chmod(0o755)
        result = {}

        def run():
            try:
                upscale.run_realesrgan_image(self.job, exe, self.src, self.dst, 2, hu._procs)
            except Exception as e:  # noqa: BLE001
                result["error"] = e
        with mock.patch.object(upscale, "POLL_S", 0.05):
            t = threading.Thread(target=run)
            t.start()
            deadline = time.monotonic() + 10
            while self.job.id not in hu._procs and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertIn(self.job.id, hu._procs)
            self.job.cancel_requested = True
            asyncio.run(hu.cancel(self.job, None))                   # ギャラリーの拡大の中止の口が、同じ _procs から止める
            t.join(15)
        self.assertFalse(t.is_alive())
        self.assertIsInstance(result.get("error"), JobCancelled)
        self.assertEqual(hu._procs, {})
        self.assertFalse(self.dst.exists())
        self.assertFalse((self.dir / "image/a_x2.part.png").exists())

    def test_prepare_image_input(self):
        work = self.dir / "work"
        self.assertEqual(upscale.prepare_image_input(self.src, work), self.src)                 # PNG はそのまま
        gif = self.dir / "a.gif"
        gif.write_bytes(png_bytes((30, 20), mode="P", fmt="GIF"))
        out = upscale.prepare_image_input(gif, work)
        self.assertEqual(out, work / "input.png")
        with Image.open(out) as im:
            self.assertEqual((im.format, im.size), ("PNG", (30, 20)))
        exif = Image.Exif()
        exif[0x0112] = 6                                                                          # 90° 回して見る写真
        jpg = self.dir / "photo.jpg"
        jpg.write_bytes(png_bytes((40, 30), fmt="JPEG", exif=exif.tobytes()))
        with Image.open(upscale.prepare_image_input(jpg, work)) as im:
            self.assertEqual(im.size, (30, 40))
        plain = self.dir / "plain.jpg"
        plain.write_bytes(png_bytes((40, 30), fmt="JPEG"))
        self.assertEqual(upscale.prepare_image_input(plain, work), plain)


class HelpersTest(unittest.TestCase):
    def test_encode_args_crops_to_even_and_audio_modes(self):
        enc = upscale.encode_args("ffmpeg", "/w/out", "/v/src.mp4", "/v/dst.mp4.part", 24)
        self.assertEqual(enc[enc.index("-vf") + 1], "crop=trunc(iw/2)*2:trunc(ih/2)*2")
        self.assertLess(enc.index("-vf"), enc.index("-c:v"))
        self.assertEqual(enc[enc.index("-c:a") + 1], "copy")
        aac = upscale.encode_args("ffmpeg", "/w/out", "/v/src.webm", "/v/dst.mp4.part", 24, "aac")
        self.assertEqual(aac[aac.index("-c:a"):aac.index("-c:a") + 4], ["-c:a", "aac", "-b:a", "192k"])
        mux = upscale.mux_args("ffmpeg", "/w/v.mp4", "/v/src.mp4", "/v/dst.mp4.part")
        self.assertNotIn("-c:a", mux)
        mux = upscale.mux_args("ffmpeg", "/w/v.mp4", "/v/src.webm", "/v/dst.mp4.part", "aac")
        self.assertEqual(mux[mux.index("-c") + 1], "copy")
        self.assertEqual(mux[mux.index("-c:a"):mux.index("-c:a") + 4], ["-c:a", "aac", "-b:a", "192k"])        # 映像は写し、音声だけ AAC に

    def test_resolve_floors_video_to_even(self):
        st = fake_status()
        self.assertEqual(upscale.resolve(None, "realesrgan", 3, 511, 509, 3840, st=st), {"method": "realesrgan", "scale": 3, "width": 1532, "height": 1526})
        self.assertEqual(upscale.resolve(None, "realesrgan", 3, 511, 509, 3840, st=st, kind="image")["width"], 1533)
        self.assertEqual(upscale.resolve(None, "realesrgan", 2, 511, 509, 3840, st=st)["width"], 1022)
        self.assertEqual(upscale.even_size(1533, 1527), (1532, 1526))

    def test_upscaled_path_skips_taken_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "a.png"
            self.assertEqual(upscale.upscaled_path(dest, 2, {Path(tmp) / "a_x2.png"}), Path(tmp) / "a_x2_2.png")

    def test_wait_gpu_idle_moved_to_upscale(self):
        job = Job(id="w", tab="video", kind="upscale.output", params={})
        asyncio.run(upscale.wait_gpu_idle(job, types.SimpleNamespace(sched=None)))
        job.cancel_requested = True
        with self.assertRaises(JobCancelled):
            asyncio.run(upscale.wait_gpu_idle(job, types.SimpleNamespace(sched=None)))

    def test_stale_part_png_is_removed_at_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp))
            self.addCleanup(ledger.close)
            (Path(tmp) / "image").mkdir()
            (Path(tmp) / "image" / "a_x2.part.png").write_bytes(b"x")
            (Path(tmp) / "image" / "keep.png").write_bytes(b"x")
            self.assertEqual(ledger.remove_stale_parts(), 1)
            self.assertTrue((Path(tmp) / "image" / "keep.png").exists())


class RtxScriptArgsTest(unittest.TestCase):
    """scripts/upscale_rtxvsr.py の引数(torch・nvidia-vfx は main の中で読むので、ここでは要らない)。"""

    def setUp(self):
        spec = importlib.util.spec_from_file_location("upscale_rtxvsr", upscale.RTXVSR_SCRIPT)
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)

    def parse(self, argv: list):
        with mock.patch.object(sys, "argv", ["upscale_rtxvsr.py", *argv]):
            return self.mod.parse_args()

    def test_image_flag(self):
        a = self.parse(upscale.rtxvsr_image_argv("py", "/o/a.png", "/o/a_x2.part.png", 3, "HIGH")[2:])
        self.assertEqual((a.image, a.input, a.output, a.scale, a.quality), (True, "/o/a.png", "/o/a_x2.part.png", 3, "HIGH"))
        a = self.parse(upscale.rtxvsr_argv("py", "/v/a.mp4", "/w/v.mp4", 2)[2:])
        self.assertEqual((a.image, a.input, a.output, a.scale), (False, "/v/a.mp4", "/w/v.mp4", 2))

    def test_missing_input_fails_with_a_reason(self):
        with mock.patch.object(sys, "argv", ["x", "--image", "/nonexistent/a.png", "/tmp/o.png"]), self.assertRaises(SystemExit) as cm, \
                mock.patch.object(sys, "stderr", io.StringIO()) as err:
            self.mod.main()
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("入力の画像がありません", err.getvalue())


if __name__ == "__main__":
    unittest.main()
