import asyncio
import io
import json
import re
import tempfile
import types
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

import httpx
from PIL import Image

from studio import media
from studio.handlers import h3
from studio.jobs import Ctx, Job, JobCancelled, JobFailed, JobRunner
from studio.ledger import Ledger


def png_bytes(size=(64, 48)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (10, 120, 200)).save(buf, "PNG")
    return buf.getvalue()


def parse_form(req: httpx.Request) -> list:
    """multipart の本文を [(name, filename, bytes)] にする(順番どおり)。"""
    if "multipart" not in req.headers["content-type"]:      # ファイルが無いときはふつうのフォーム
        return [(k, None, v.encode()) for k, v in urllib.parse.parse_qsl(req.content.decode())]
    boundary = re.search(r"boundary=([^;]+)", req.headers["content-type"]).group(1).encode()
    out = []
    for part in req.content.split(b"--" + boundary)[1:-1]:
        head, _, body = part.partition(b"\r\n\r\n")
        name = re.search(rb'name="([^"]*)"', head).group(1).decode()
        fn = re.search(rb'filename="([^"]*)"', head)
        out.append((name, fn.group(1).decode() if fn else None, body[:-2]))
    return out


class FakeH3:
    """H3 の API の代わり(httpx.MockTransport)。生成の POST は、進捗が何回か読まれるまで返らない。"""

    def __init__(self, result=None, post_status=200, post_detail=None, wait_polls=3, wait_interrupt=False):
        self.result = result or {"job_id": "remote1", "video_url": "/outputs/out_1.mp4", "duration_s": 5.0,
                                 "total_elapsed_s": 42.5, "num_inference_steps": 8, "seed": 99, "peak_vram_gb": 11.0}
        self.post_status, self.post_detail = post_status, post_detail
        self.wait_polls, self.wait_interrupt = wait_polls, wait_interrupt
        self.polls, self.interrupts, self.posts, self.downloads = 0, [], [], []
        self.job = None
        self.seen = []     # POST が返る直前のジョブの (progress, message)

    async def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/api/progress":
            self.polls += 1
            if self.polls == 1:       # 生成の前: 前回のジョブが残っている
                return httpx.Response(200, json={"job_id": "old000", "phase": "done", "step": 8, "total_steps": 8})
            phase = "encoding" if self.polls == 2 else "denoising"
            return httpx.Response(200, json={"job_id": "remote1", "phase": phase, "step": 2, "total_steps": 8, "message": ""})
        if req.method == "POST" and path == "/api/interrupt":
            self.interrupts.append(json.loads(req.content))
            return httpx.Response(200, json={"interrupted": True})
        if req.method == "POST" and path in ("/api/t2va", "/api/fl2va", "/api/ref2va", "/api/t2i"):
            self.posts.append((path, parse_form(req)))
            for _ in range(500):
                if (self.interrupts if self.wait_interrupt else self.polls >= self.wait_polls):
                    break
                await asyncio.sleep(0.01)
            if self.job is not None:
                self.seen.append((self.job.progress, self.job.message, self.job.remote_id))
            if self.post_status != 200:
                return httpx.Response(self.post_status, json={"detail": self.post_detail})
            return httpx.Response(200, json=self.result)
        if req.method == "GET" and path.startswith("/outputs/"):
            self.downloads.append(path)
            return httpx.Response(200, content=png_bytes((96, 64)) if path.endswith(".png") else b"fakevideo")
        return httpx.Response(404, json={"detail": f"想定外: {req.method} {path}"})


class H3Test(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.ledger = Ledger(self.root)
        self.addCleanup(self.ledger.close)
        p = mock.patch.object(h3, "POLL_S", 0.01)
        p.start()
        self.addCleanup(p.stop)
        for name, ret in (("probe", {"duration_s": None, "width": None, "height": None}), ("video_thumb", False)):   # ダミーの動画で ffmpeg を呼ばない
            q = mock.patch.object(media, name, return_value=ret)
            q.start()
            self.addCleanup(q.stop)
        self.sched = types.SimpleNamespace(comps={"h3": types.SimpleNamespace(spec=types.SimpleNamespace(base_url="http://h3.test"))})

    def ctx(self, comp):
        return Ctx(self.sched, self.ledger, self.root, transport=httpx.MockTransport(comp))

    def media(self, kind: str, name: str) -> str:
        p = self.root / "uploads" / name
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(png_bytes() if kind == "image" else b"bin-" + name.encode())
        return self.ledger.add_output(kind, p, "upload", filename=name)["id"]

    def job(self, kind, params) -> Job:
        norm = h3.normalize(kind, params, self.ledger)
        return Job(id="job123456789", tab="video", kind=kind, params=norm)

    async def test_t2va_flow(self):
        comp = FakeH3()
        job = self.job("h3.t2va", {"prompt": "猫が歩く", "resolution": "768x512", "seconds": 6, "seed": 5})
        comp.job = job
        await h3.run(job, self.ctx(comp))
        path, form = comp.posts[0]
        d = {n: b.decode() for n, _, b in form}
        self.assertEqual(path, "/api/t2va")
        self.assertEqual((d["prompt"], d["width"], d["height"], d["seconds"], d["seed"]), ("猫が歩く", "768", "512", "6.0", "5"))
        self.assertNotIn("resolution", d)   # 部品のプリセット名ではなく width / height で渡す
        self.assertNotIn("num_inference_steps", d)
        progress, message, remote = comp.seen[0]          # 進捗を読んで、ジョブに写している
        self.assertEqual(remote, "remote1")
        self.assertAlmostEqual(progress, 0.25)
        self.assertIn("2/8", message)
        self.assertEqual(comp.downloads, ["/outputs/out_1.mp4"])
        (rec,) = self.ledger.list_outputs(tab="video")
        self.assertEqual((rec["kind"], rec["path"], rec["duration_s"], rec["job_id"]), ("video", "video/job123456789.mp4", 5.0, "job123456789"))
        self.assertEqual(rec["params"]["result"]["generation_seconds"], 42.5)
        self.assertEqual(rec["params"]["result"]["seed"], 99)
        self.assertEqual(job.outputs, [rec["id"]])
        self.assertFalse(list((self.root / "video").glob("*.part")))

    async def test_fl2va_sends_first_and_last_images(self):
        comp = FakeH3()
        a, b = self.media("image", "first.png"), self.media("image", "last.png")
        await h3.run(self.job("h3.fl2va", {"prompt": "p", "first_image": a, "last_image": b, "seconds": 5}), self.ctx(comp))
        path, form = comp.posts[0]
        self.assertEqual(path, "/api/fl2va")
        self.assertEqual([(n, f) for n, f, _ in form if f], [("image", "first.png"), ("last_image", "last.png")])
        await h3.run(self.job("h3.fl2va", {"prompt": "p", "last_image": b, "seconds": 5}), self.ctx(FakeH3()))   # 最後だけでも通る

    async def test_ref2va_mixed_refs_in_order_audio_length(self):
        comp = FakeH3()
        refs = [self.media("image", "a.png"), self.media("video", "v.mp4"), self.media("audio", "voice.wav"), self.media("image", "b.png")]
        job = self.job("h3.ref2va", {"prompt": "p", "references": refs, "seconds": 0, "vocal_lock": True})
        await h3.run(job, self.ctx(comp))
        path, form = comp.posts[0]
        d = {n: b.decode() for n, f, b in form if not f}
        self.assertEqual(path, "/api/ref2va")
        self.assertEqual([f for n, f, _ in form if n == "references"], ["a.png", "v.mp4", "voice.wav", "b.png"])
        self.assertNotIn("seconds", d)      # 秒数を送らない = 音声の長さに合わせる(部品の仕様)
        self.assertEqual(d["vocal_lock"], "true")
        self.assertNotIn("still", d)
        self.assertEqual(comp.downloads, ["/outputs/out_1.mp4"])
        self.assertEqual(self.ledger.list_outputs()[0]["kind"], "video")

    async def test_ref2va_explicit_seconds_and_size(self):
        comp = FakeH3()
        refs = [self.media("image", "a.png")]
        await h3.run(self.job("h3.ref2va", {"prompt": "p", "references": refs, "seconds": 8, "width": 770, "height": 512}), self.ctx(comp))
        d = {n: b.decode() for n, f, b in comp.posts[0][1] if not f}
        self.assertEqual((d["seconds"], d["width"], d["height"]), ("8.0", "768", "512"))
        self.assertNotIn("vocal_lock", d)

    async def test_ref2i_is_still(self):
        comp = FakeH3(result={"job_id": "remote1", "video_url": "/outputs/m.mp4", "image_url": "/outputs/m.png", "total_elapsed_s": 70})
        refs = [self.media("image", "a.png")]
        job = self.job("h3.ref2i", {"prompt": "p", "references": refs, "resolution": "768x768"})
        await h3.run(job, self.ctx(comp))
        d = {n: b.decode() for n, f, b in comp.posts[0][1] if not f}
        self.assertEqual((comp.posts[0][0], d["still"], d["width"], d["height"]), ("/api/ref2va", "1", "768", "768"))
        self.assertNotIn("seconds", d)
        self.assertEqual(comp.downloads, ["/outputs/m.png"])      # 超短尺の mp4 は取り込まない
        (rec,) = self.ledger.list_outputs(kind="image", tab="video")
        self.assertEqual((rec["path"], rec["width"], rec["height"]), ("image/job123456789.png", 96, 64))

    async def test_t2i_is_still(self):
        comp = FakeH3(result={"job_id": "remote1", "image_url": "/outputs/m.png"})
        await h3.run(self.job("h3.t2i", {"prompt": "p", "resolution": "512x512"}), self.ctx(comp))
        self.assertEqual(comp.posts[0][0], "/api/t2i")
        self.assertEqual(self.ledger.list_outputs()[0]["kind"], "image")

    async def test_cancel_sends_interrupt_with_progress_job_id(self):
        comp = FakeH3(post_status=499, post_detail="生成が中断されました: user", wait_interrupt=True)

        class Sched:
            comps = self.sched.comps

            def new_operation(self, kind, name):
                from studio.scheduler import Operation
                return Operation(kind, name)

            async def ensure(self, name, op=None):
                op.add("準備完了")
        runner = JobRunner(Ctx(Sched(), self.ledger, self.root, transport=httpx.MockTransport(comp)), h3.HANDLERS, step_poll_s=0.01)
        runner.start()
        self.addAsyncCleanup(runner.stop)
        job = runner.submit("video", "h3.t2va", {"prompt": "猫"})
        for _ in range(300):
            if job.remote_id:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(job.remote_id, "remote1")
        await runner.cancel(job.id)
        for _ in range(300):
            if job.status == "cancelled":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(job.status, "cancelled")
        self.assertEqual(comp.interrupts[0], {"job_id": "remote1"})
        self.assertEqual(self.ledger.list_outputs(), [])

    async def test_errors(self):
        with self.assertRaises(JobFailed) as cm:
            await h3.run(self.job("h3.t2va", {"prompt": "p"}), self.ctx(FakeH3(post_status=400, post_detail="seconds must be between 5.0 and 15.0")))
        self.assertIn("seconds must be between", cm.exception.message)
        with self.assertRaises(JobFailed) as cm:
            await h3.run(self.job("h3.t2va", {"prompt": "p"}), self.ctx(FakeH3(post_status=409, post_detail="busy")))
        self.assertIn("別の生成", cm.exception.message)
        with self.assertRaises(JobFailed):       # 結果に動画の URL が無い
            await h3.run(self.job("h3.t2va", {"prompt": "p"}), self.ctx(FakeH3(result={"job_id": "remote1"})))
        with self.assertRaises(JobFailed):       # 台帳から消えた参照
            job = self.job("h3.ref2va", {"prompt": "p", "references": [self.media("image", "a.png")], "seconds": 5})
            self.ledger.delete_output(job.params["references"][0])
            await h3.run(job, self.ctx(FakeH3()))

    def test_normalize_validation(self):
        n = h3.normalize
        img = self.media("image", "a.png")
        aud = self.media("audio", "a.wav")
        vid = self.media("video", "a.mp4")
        bad = [
            ("h3.t2va", {"prompt": ""}),
            ("h3.t2va", {"prompt": "p", "seconds": 2}),
            ("h3.t2va", {"prompt": "p", "seconds": 16}),
            ("h3.t2va", {"prompt": "p", "seconds": 0}),                 # 0 は ref2va だけ
            ("h3.t2va", {"prompt": "p", "resolution": "big"}),
            ("h3.t2va", {"prompt": "p", "seed": "x"}),
            ("h3.fl2va", {"prompt": "p"}),                              # 先頭も最後も無い
            ("h3.fl2va", {"prompt": "p", "first_image": "nope"}),
            ("h3.fl2va", {"prompt": "p", "first_image": aud}),          # 画像ではない
            ("h3.ref2va", {"prompt": "p", "references": [], "seconds": 5}),
            ("h3.ref2i", {"prompt": "p", "references": []}),
            ("h3.ref2va", {"prompt": "p", "references": [img], "seconds": 0}),               # 音声が無い
            ("h3.ref2va", {"prompt": "p", "references": [aud, aud], "seconds": 0}),          # 音声が 2 つ
            ("h3.ref2va", {"prompt": "p", "references": [img] * 10, "seconds": 5}),           # 画像 10 > 9
            ("h3.ref2va", {"prompt": "p", "references": [vid] * 4, "seconds": 5}),            # 動画 4 > 3
            ("h3.ref2va", {"prompt": "p", "references": [aud] * 4, "seconds": 5}),            # 音声 4 > 3
            ("h3.ref2va", {"prompt": "p", "references": ["nope"], "seconds": 5}),
        ]
        for kind, params in bad:
            with self.assertRaises(ValueError, msg=f"{kind} {params}"):
                n(kind, params, self.ledger)
        ok = n("h3.ref2va", {"prompt": " p ", "references": [img] * 9 + [vid] * 3 + [aud] * 3, "seconds": 5, "vocal_lock": 1}, self.ledger)
        self.assertEqual((len(ok["references"]), ok["vocal_lock"], ok["prompt"], ok["width"]), (15, True, "p", None))
        self.assertFalse(n("h3.ref2va", {"prompt": "p", "references": [img], "seconds": 5, "vocal_lock": True}, self.ledger)["vocal_lock"])
        self.assertEqual(n("h3.ref2va", {"prompt": "p", "references": [aud], "seconds": 0}, self.ledger)["seconds"], 0)
        self.assertNotIn("seconds", n("h3.t2i", {"prompt": "p"}, self.ledger))
        self.assertEqual(n("h3.t2va", {"prompt": "p", "seconds": "7.5", "seed": ""}, self.ledger)["seconds"], 7.5)
        self.assertEqual(n("h3.t2va", {"prompt": "p"}, self.ledger)["resolution"], "768x768")


if __name__ == "__main__":
    unittest.main()
