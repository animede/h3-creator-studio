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

    async def release_before_gpu(self):
        self.events.append("release")
        return False


class FakeSched:
    def __init__(self, llm, error=None):
        self.llm, self.error = llm, error
        self.comps = {"h3": types.SimpleNamespace(spec=types.SimpleNamespace(base_url="http://h3.test", label="MiniMax-H3")),
                      "h3image": types.SimpleNamespace(spec=types.SimpleNamespace(base_url="http://h3image.test", label="MiniMax-H3-Image"))}

    async def ensure(self, name, op=None):
        self.llm.events.append(f"ensure {name}")
        if self.error:
            raise self.error


class PlanSched(FakeSched):
    """載せる見通し(plan)を持つ scheduler。action が wait なら、blockers が生成中で載せられない。"""

    def __init__(self, llm, action="ready", blockers=()):
        super().__init__(llm)
        self.action, self.blockers = action, list(blockers)

    def plan(self, name):
        return types.SimpleNamespace(action=self.action, blockers=self.blockers)


class MarkingSched(PlanSched):
    """mark_busy / mark_idle を持つ scheduler: 強化の間だけ、部品に仕事中の印が付く。"""

    def __init__(self, llm, error=None):
        super().__init__(llm)
        self.error, self.marks = error, []

    def mark_busy(self, name):
        self.marks.append(("busy", name))

    def mark_idle(self, name):
        self.marks.append(("idle", name))


class EnhanceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ledger = Ledger(Path(tmp.name))
        self.addCleanup(self.ledger.close)
        self.requests = []

    def runner(self, handler, llm=None, sched_error=None, busy=False, sched=None):
        llm = llm or FakeLlm()

        def record(req: httpx.Request) -> httpx.Response:
            self.requests.append(req)
            return handler(req)
        ctx = Ctx(sched(llm) if sched else FakeSched(llm, sched_error), self.ledger, Path("."), transport=httpx.MockTransport(record), llm=llm)
        return types.SimpleNamespace(ctx=ctx, gpu_busy=lambda: busy), llm

    async def test_video_calls_h3_enhance_after_ensure_then_llm(self):
        runner, llm = self.runner(lambda r: httpx.Response(200, json={"result": "強化された動画プロンプト", "mode": "h3-official"}))
        out = await enhance_prompt(runner, "video", " 猫が走る ", seconds=8, task="ref2va")
        self.assertEqual(out, {"text": "強化された動画プロンプト"})
        # LLM の VRAM を返してから部品を載せ、その後で LLM を起こす(LLM は載った後の空きに合わせて GPU に載る)
        self.assertEqual(llm.events, ["release", "session+", "ensure h3", "llm", "session-"])
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

    async def test_gpu_work_does_not_refuse(self):
        """生成中でも強化できる(管理役の LLM は CPU 専用で動く)。「GPU で生成中」と断らない。"""
        runner, llm = self.runner(lambda r: httpx.Response(200, json={"prompt": "A cat."}), busy=True)
        out = await enhance_prompt(runner, "image", "猫")
        self.assertEqual(out["text"], "A cat.")
        self.assertEqual(llm.events, ["release", "session+", "ensure h3image", "llm", "session-"])

    async def test_refuses_only_when_loading_would_evict_a_busy_component(self):
        runner, llm = self.runner(lambda r: httpx.Response(200, json={"result": "x"}),
                                  sched=lambda l: PlanSched(l, "wait", ["h3image"]))
        with self.assertRaises(EnhanceError) as cm:       # 載っていなくて、載せるには生成中の H3-Image を降ろす必要がある
            await enhance_prompt(runner, "video", "猫")
        self.assertEqual(cm.exception.status, 409)
        self.assertIn("MiniMax-H3-Image", cm.exception.message)
        self.assertIn("生成中", cm.exception.message)
        self.assertEqual((llm.events, self.requests), ([], []))       # LLM も部品も起こさない
        for action in ("ready", "fit", "evict"):                      # 載っている・待たずに載せられるなら、いつもどおり
            runner, llm = self.runner(lambda r: httpx.Response(200, json={"result": "強化"}), sched=lambda l, a=action: PlanSched(l, a))
            self.assertEqual((await enhance_prompt(runner, "video", "猫"))["text"], "強化")
            # 載せるとき(fit / evict)だけ、先に LLM の VRAM を返す。載っていれば(ready)VRAM は要らない
            self.assertEqual(llm.events[:3], ["session+", "ensure h3", "llm"] if action == "ready" else ["release", "session+", "ensure h3"], action)

    async def test_component_is_marked_busy_only_while_enhancing(self):
        """強化の最中に別の生成が、その部品を GPU から降ろさないよう、仕事中の印を付ける(成功でも失敗でも外す)。"""
        made = []

        def factory(llm, error=None):
            made.append(MarkingSched(llm, error))
            return made[-1]
        runner, llm = self.runner(lambda r: httpx.Response(200, json={"result": "強化"}), sched=factory)
        await enhance_prompt(runner, "video", "猫")
        self.assertEqual(made[0].marks, [("busy", "h3"), ("idle", "h3")])
        self.assertEqual(llm.events[0:2], ["session+", "ensure h3"])
        runner, _ = self.runner(lambda r: httpx.Response(502, json={"detail": "LLM に接続できません"}), sched=factory)
        with self.assertRaises(EnhanceError):
            await enhance_prompt(runner, "image", "猫")
        self.assertEqual(made[1].marks, [("busy", "h3image"), ("idle", "h3image")])
        runner, _ = self.runner(lambda r: httpx.Response(200, json={}), llm=FakeLlm(LlmError("起動できません")), sched=factory)
        with self.assertRaises(EnhanceError):
            await enhance_prompt(runner, "video", "猫")
        self.assertEqual(made[2].marks, [("busy", "h3"), ("idle", "h3")])
        refused = []

        def waiting(llm):
            refused.append(MarkingSched(llm))
            refused[-1].action, refused[-1].blockers = "wait", ["h3image"]
            return refused[-1]
        runner, _ = self.runner(lambda r: httpx.Response(200, json={}), sched=waiting)
        with self.assertRaises(EnhanceError):
            await enhance_prompt(runner, "video", "猫")
        self.assertEqual(refused[0].marks, [])                       # 断ったときは、印を付けない

    async def test_errors_have_japanese_reasons(self):
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
