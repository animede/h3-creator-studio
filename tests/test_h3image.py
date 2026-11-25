import asyncio
import io
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import httpx
from PIL import Image

from studio.handlers import h3image
from studio.jobs import Ctx, Job, JobCancelled, JobFailed, JobRunner
from studio.ledger import Ledger


def png_bytes(size=(64, 48)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (10, 120, 200)).save(buf, "PNG")
    return buf.getvalue()


class FakeComponent:
    """H3-Image の API の代わり(httpx.MockTransport)。状態の列を順に返す。"""

    def __init__(self, statuses, error=None, reject_assets=False):
        self.statuses, self.error, self.reject_assets = list(statuses), error, reject_assets
        self.assets, self.deleted, self.submitted, self.interrupts = [], [], [], []
        self.polls = 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "POST" and path == "/api/assets":
            if self.reject_assets:
                return httpx.Response(415, json={"detail": "画像として読めません"})
            self.assets.append(req.read())
            return httpx.Response(201, json={"id": f"{len(self.assets):032x}", "kind": "image", "filename": "x", "size": 1})
        if req.method == "DELETE" and path.startswith("/api/assets/"):
            self.deleted.append(path.rsplit("/", 1)[1])
            return httpx.Response(204)
        if req.method == "POST" and path == "/api/jobs":
            self.submitted.append(json.loads(req.content))
            return httpx.Response(202, json={"id": "remote1", "status": "queued", "progress": 0.0})
        if req.method == "POST" and path == "/api/interrupt":
            self.interrupts.append(json.loads(req.content))
            return httpx.Response(200, json={})
        if req.method == "GET" and path == "/api/jobs/remote1":
            self.polls += 1
            st = self.statuses[min(self.polls - 1, len(self.statuses) - 1)]
            if callable(st):
                st = st(self)
            info = {"id": "remote1", "status": st, "progress": 0.5 if st == "running" else 0.0, "error": self.error if st in ("failed",) else None,
                    "image_url": "/outputs/out_1.png" if st == "completed" else None,
                    "generation_seconds": 12.5, "peak_vram_gb": 9.1, "steps_used": 6, "profile": "p"}
            return httpx.Response(200, json=info)
        if req.method == "GET" and path == "/outputs/out_1.png":
            return httpx.Response(200, content=png_bytes((96, 64)))
        return httpx.Response(404, json={"detail": f"想定外: {req.method} {path}"})


class H3ImageTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.ledger = Ledger(self.root)
        self.addCleanup(self.ledger.close)
        p = mock.patch.object(h3image, "POLL_S", 0.01)
        p.start()
        self.addCleanup(p.stop)
        self.sched = types.SimpleNamespace(comps={"h3image": types.SimpleNamespace(spec=types.SimpleNamespace(base_url="http://h3image.test"))})

    def ctx(self, comp):
        return Ctx(self.sched, self.ledger, self.root, transport=httpx.MockTransport(comp))

    def ref(self) -> str:
        p = self.root / "uploads" / "r.png"
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(png_bytes())
        return self.ledger.add_output("image", p, "upload", filename="r.png")["id"]

    def job(self, kind, **params):
        base = {"prompt": "猫", "width": 1024, "height": 768, "seed": 7, "steps": None, "references": []}
        return Job(id="job123456789", tab="image", kind=kind, params={**base, **params})

    async def test_edit_flow(self):
        comp = FakeComponent(["queued", "running", "completed"])
        refs = [self.ref(), self.ref()]
        job = self.job("h3image.edit", references=refs)
        await h3image.run(job, self.ctx(comp))
        self.assertEqual(len(comp.assets), 2)
        body = comp.submitted[0]
        self.assertEqual((body["mode"], body["prompt"], body["width"], body["height"], body["seed"]), ("edit", "猫", 1024, 768, 7))
        self.assertNotIn("steps", body)   # 未指定は送らない
        self.assertEqual([c["asset_id"] for c in body["conditions"]], [f"{1:032x}", f"{2:032x}"])
        self.assertEqual(sorted(comp.deleted), sorted(c["asset_id"] for c in body["conditions"]))   # 参照は後始末される
        out = self.root / "image" / "job123456789.png"
        self.assertTrue(out.is_file())
        self.assertFalse(list((self.root / "image").glob("*.part")))
        (rec,) = self.ledger.list_outputs(tab="image")
        self.assertEqual((rec["path"], rec["width"], rec["height"], rec["job_id"]), ("image/job123456789.png", 96, 64, "job123456789"))
        self.assertEqual(rec["params"]["result"]["generation_seconds"], 12.5)
        self.assertEqual(job.outputs, [rec["id"]])

    async def test_t2i_flow_without_refs(self):
        comp = FakeComponent(["running", "completed"])
        job = self.job("h3image.t2i", steps=3, seed=None)
        await h3image.run(job, self.ctx(comp))
        body = comp.submitted[0]
        self.assertEqual((body["mode"], body["steps"]), ("t2i", 3))
        self.assertNotIn("seed", body)
        self.assertNotIn("conditions", body)
        self.assertEqual(comp.assets, [])
        self.assertEqual(len(job.outputs), 1)

    async def test_failed_reports_component_error_and_cleans_up(self):
        comp = FakeComponent(["running", "failed"], error="CUDA out of memory")
        job = self.job("h3image.edit", references=[self.ref()])
        with self.assertRaises(JobFailed) as cm:
            await h3image.run(job, self.ctx(comp))
        self.assertEqual(cm.exception.message, "CUDA out of memory")
        self.assertEqual(len(comp.deleted), 1)
        self.assertEqual(self.ledger.list_outputs(tab="image"), [])

    async def test_interrupted_is_cancelled(self):
        with self.assertRaises(JobCancelled):
            await h3image.run(self.job("h3image.t2i"), self.ctx(FakeComponent(["running", "interrupted"])))

    async def test_upload_rejected(self):
        comp = FakeComponent(["completed"], reject_assets=True)
        with self.assertRaises(JobFailed) as cm:
            await h3image.run(self.job("h3image.edit", references=[self.ref()]), self.ctx(comp))
        self.assertIn("画像として読めません", cm.exception.message)
        self.assertEqual(comp.submitted, [])

    async def test_missing_reference(self):
        with self.assertRaises(JobFailed):
            await h3image.run(self.job("h3image.edit", references=["nope"]), self.ctx(FakeComponent(["completed"])))

    async def test_cancel_running_via_runner(self):
        """中止要求 → /api/interrupt → エンジンが interrupted を返す → ジョブは cancelled。"""
        comp = FakeComponent(["running", lambda c: "interrupted" if c.interrupts else "running"])

        class Sched:
            comps = self.sched.comps

            def new_operation(self, kind, name):
                from studio.scheduler import Operation
                return Operation(kind, name)

            async def ensure(self, name, op=None):
                op.add("準備完了")
        runner = JobRunner(Ctx(Sched(), self.ledger, self.root, transport=httpx.MockTransport(comp)), h3image.HANDLERS, step_poll_s=0.01)
        runner.start()
        self.addAsyncCleanup(runner.stop)
        job = runner.submit("image", "h3image.t2i", {"prompt": "猫"})
        for _ in range(300):
            if job.status == "running" and job.remote_id:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        await runner.cancel(job.id)
        for _ in range(300):
            if job.status == "cancelled":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(job.status, "cancelled")
        self.assertEqual(comp.interrupts[0], {"job_id": "remote1"})

    def test_normalize(self):
        n = h3image.normalize
        out = n("h3image.t2i", {"prompt": " 猫 ", "width": 1010, "height": 99999, "seed": "", "steps": "6"})
        self.assertEqual((out["prompt"], out["width"], out["height"], out["seed"], out["steps"]), ("猫", 1024, 4096, None, 6))
        self.assertEqual(n("h3image.t2i", {"prompt": "a", "width": 10, "height": 300})["width"], 256)
        for kind, params in [("h3image.t2i", {"prompt": ""}), ("h3image.t2i", {"prompt": "a", "references": ["x"]}),
                             ("h3image.edit", {"prompt": "a", "references": []}),
                             ("h3image.edit", {"prompt": "a", "references": [str(i) for i in range(17)]}),
                             ("h3image.t2i", {"prompt": "a", "steps": 4}), ("h3image.t2i", {"prompt": "a", "seed": "x"})]:
            with self.assertRaises(ValueError, msg=str(params)):
                n(kind, params)
        for steps in ("abc", "3.5", float("inf"), [3]):                     # 数でない値も、英語の例外文ではなく日本語の理由で断る
            with self.assertRaisesRegex(ValueError, "steps は .* のいずれかか、未指定", msg=repr(steps)):
                n("h3image.t2i", {"prompt": "a", "steps": steps})
        with self.assertRaisesRegex(ValueError, "seed は整数"):
            n("h3image.t2i", {"prompt": "a", "seed": float("inf")})            # JSON の Infinity(int にできず OverflowError だった)
        self.assertEqual(len(n("h3image.edit", {"prompt": "a", "references": [str(i) for i in range(16)]})["references"]), 16)
        with self.assertRaises(ValueError):   # 台帳に無い参照
            n("h3image.edit", {"prompt": "a", "references": ["zzz"]}, self.ledger)
        self.assertEqual(n("h3image.edit", {"prompt": "a", "references": [self.ref()]}, self.ledger)["references"][0], self.ledger.list_outputs()[0]["id"])


if __name__ == "__main__":
    unittest.main()
