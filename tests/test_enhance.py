import contextlib
import io
import json
import tempfile
import types
import unittest
from pathlib import Path
from urllib.parse import parse_qs

import httpx
from PIL import Image

from studio.enhance import EnhanceError, enhance_prompt
from studio.jobs import Ctx
from studio.ledger import Ledger
from studio.llm import LlmError
from studio.scheduler import SwitchError


class FakeLlm:
    def __init__(self, error=None):
        self.error, self.events = error, []

    @contextlib.asynccontextmanager
    async def session(self):
        self.events.append("session+")
        yield
        self.events.append("session-")

    async def ensure_running(self):
        self.events.append("llm")
        if self.error:
            raise self.error


class FakeSched:
    def __init__(self, llm, error=None):
        self.llm, self.error = llm, error
        self.comps = {"h3": types.SimpleNamespace(spec=types.SimpleNamespace(base_url="http://h3.test", label="MiniMax-H3")),
                      "h3image": types.SimpleNamespace(spec=types.SimpleNamespace(base_url="http://h3image.test", label="MiniMax-H3-Image"))}

    async def ensure(self, name, op=None):
        self.llm.events.append(f"ensure {name}")
        if self.error:
            raise self.error


class EnhanceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ledger = Ledger(Path(tmp.name))
        self.addCleanup(self.ledger.close)
        self.requests = []

    def runner(self, handler, llm=None, sched_error=None, busy=False):
        llm = llm or FakeLlm()

        def record(req: httpx.Request) -> httpx.Response:
            self.requests.append(req)
            return handler(req)
        ctx = Ctx(FakeSched(llm, sched_error), self.ledger, Path("."), transport=httpx.MockTransport(record), llm=llm)
        return types.SimpleNamespace(ctx=ctx, gpu_busy=lambda: busy), llm

    async def test_video_calls_h3_enhance_after_ensure_then_llm(self):
        runner, llm = self.runner(lambda r: httpx.Response(200, json={"result": "強化された動画プロンプト", "mode": "h3-official"}))
        out = await enhance_prompt(runner, "video", " 猫が走る ", seconds=8, task="ref2va")
        self.assertEqual(out, {"text": "強化された動画プロンプト"})
        # 部品を載せてから LLM を起こす(LLM は載った後の空きに合わせて GPU に載る)
        self.assertEqual(llm.events, ["session+", "ensure h3", "llm", "session-"])
        req = self.requests[0]
        self.assertEqual((req.method, req.url.path), ("POST", "/api/prompt/enhance"))
        form = parse_qs(req.content.decode())
        self.assertEqual({k: v[0] for k, v in form.items()},
                         {"text": "猫が走る", "mode": "h3-official", "task": "ref2va", "lang": "en", "seconds": "8"})

    async def test_image_t2i(self):
        runner, _ = self.runner(lambda r: httpx.Response(200, json={"prompt": "A cat.", "aspect_ratio": "16:9", "suggested_width": 1344, "suggested_height": 768}))
        out = await enhance_prompt(runner, "image", "猫")
        self.assertEqual(out["text"], "A cat.")
        self.assertEqual((out["suggested_width"], out["suggested_height"]), (1344, 768))
        self.assertEqual(json.loads(self.requests[0].content), {"prompt": "猫", "mode": "t2i", "conditions": []})

    async def test_image_edit_uploads_and_deletes_references(self):
        p = Path(self.ledger.root) / "uploads" / "r.png"
        p.parent.mkdir()
        buf = io.BytesIO()
        Image.new("RGB", (8, 8)).save(buf, "PNG")
        p.write_bytes(buf.getvalue())
        rec = self.ledger.add_output("image", p, "upload")

        def handler(req):
            if req.url.path == "/api/assets" and req.method == "POST":
                return httpx.Response(201, json={"id": "a" * 32})
            if req.method == "DELETE":
                return httpx.Response(204)
            return httpx.Response(200, json={"prompt": "Make it red."})
        runner, _ = self.runner(handler)
        out = await enhance_prompt(runner, "image", "赤くして", references=[rec["id"]])
        self.assertEqual(out["text"], "Make it red.")
        self.assertEqual(json.loads(self.requests[1].content), {"prompt": "赤くして", "mode": "edit", "conditions": [{"asset_id": "a" * 32}]})
        self.assertEqual([(r.method, r.url.path) for r in self.requests][-1], ("DELETE", "/api/assets/" + "a" * 32))
        with self.assertRaises(EnhanceError) as cm:
            await enhance_prompt(runner, "image", "x", references=["nope"])
        self.assertEqual(cm.exception.status, 400)

    async def test_errors_have_japanese_reasons(self):
        runner, _ = self.runner(lambda r: httpx.Response(200, json={}), busy=True)
        with self.assertRaises(EnhanceError) as cm:                     # 生成中
            await enhance_prompt(runner, "image", "猫")
        self.assertEqual(cm.exception.status, 409)
        runner, _ = self.runner(lambda r: httpx.Response(200, json={}))
        for tab, text in (("music", "x"), ("image", "  ")):
            with self.assertRaises(EnhanceError) as cm:
                await enhance_prompt(runner, tab, text)
            self.assertEqual(cm.exception.status, 400)
        runner, _ = self.runner(lambda r: httpx.Response(200, json={}), sched_error=SwitchError("GPU が空きません"))
        with self.assertRaises(EnhanceError) as cm:
            await enhance_prompt(runner, "video", "猫")
        self.assertIn("GPU が空きません", cm.exception.message)
        runner, _ = self.runner(lambda r: httpx.Response(200, json={}), llm=FakeLlm(LlmError("起動できません")))
        with self.assertRaises(EnhanceError) as cm:
            await enhance_prompt(runner, "video", "猫")
        self.assertEqual(cm.exception.status, 503)

    async def test_component_without_llm_setting(self):
        runner, _ = self.runner(lambda r: httpx.Response(501, json={"detail": "プロンプト拡張には OpenAI 互換の LLM が必要です"}))
        with self.assertRaises(EnhanceError) as cm:
            await enhance_prompt(runner, "image", "猫")
        self.assertEqual(cm.exception.status, 409)
        self.assertIn("管理役から起動", cm.exception.message)
        runner, _ = self.runner(lambda r: httpx.Response(502, json={"detail": "LLM に接続できません"}))
        with self.assertRaises(EnhanceError) as cm:
            await enhance_prompt(runner, "video", "猫")
        self.assertIn("LLM に接続できません", cm.exception.message)


if __name__ == "__main__":
    unittest.main()
