import asyncio
import json
import tempfile
import types
import unittest
import uuid
from pathlib import Path
from unittest import mock

import httpx

from studio.handlers import ace
from studio.jobs import Ctx, Job, JobCancelled, JobFailed, JobRunner
from studio.ledger import Ledger
from studio.llm import LlmError
from studio.scheduler import Operation

PLAN_RESPONSE = """THEME: 夜明けの駅
CAPTION: A bright J-Pop song with an energetic mood and a clear female vocal.
{"recommended_duration": 60, "parts": {}}

[Verse]
始発を待つホーム
[Chorus]
朝の光が来る
"""


def envelope(data=None, code=200, error=None) -> httpx.Response:
    return httpx.Response(200, json={"data": data, "code": code, "error": error})


def result(**item) -> str:
    return json.dumps([item])


class FakeAce:
    """ACE-Step の API の代わり。query_result は items(status と result の対)を順に返し、最後の値を繰り返す。"""

    def __init__(self, items, format_ok=True):
        self.items, self.format_ok = list(items), format_ok
        self.formats, self.releases, self.cancels, self.polls = [], [], [], 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/format_input":
            self.formats.append(json.loads(req.content))
            if not self.format_ok:
                return envelope(code=500, error="format_sample failed")
            return envelope({"caption": "x", "bpm": 96, "key_scale": "C major", "time_signature": "4", "vocal_language": "ja"})
        if path == "/release_task":
            body = json.loads(req.content)
            self.releases.append(body)
            return envelope({"task_id": body["task_id"], "status": "queued", "queue_position": 1})
        if path == "/query_result":
            self.polls += 1
            it = self.items[min(self.polls - 1, len(self.items) - 1)]
            it = it(self) if callable(it) else it
            return envelope([{"task_id": json.loads(req.content)["task_id_list"][0], **it}])
        if path == "/v1/cancel":
            self.cancels.append(json.loads(req.content))
            return envelope({"status": "cancelling"})
        if path == "/v1/audio":
            return httpx.Response(200, content=b"ID3fake-mp3", headers={"content-type": "audio/mpeg"})
        return httpx.Response(404, json={"detail": f"想定外: {req.method} {path}"})


RUNNING = {"status": 0, "result": result(progress=0.4, stage="denoising")}
DONE = {"status": 1, "result": result(file="/v1/audio?path=%2Ftmp%2Fa.mp3", metas={"bpm": 100, "keyscale": "A minor", "duration": 61.0})}


class FakeLlm:
    def __init__(self, response=PLAN_RESPONSE, error=None):
        self.response, self.error, self.calls = response, error, []

    async def chat(self, system, user, max_tokens=0, temperature=0):
        self.calls.append(user)
        if self.error:
            raise self.error
        return self.response

    async def release_before_gpu(self):
        return False


class AceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.ledger = Ledger(self.root)
        self.addCleanup(self.ledger.close)
        p = mock.patch.object(ace, "POLL_S", 0.01)
        p.start()
        self.addCleanup(p.stop)
        self.sched = types.SimpleNamespace(comps={"ace": types.SimpleNamespace(spec=types.SimpleNamespace(base_url="http://ace.test"))})

    def ctx(self, comp, llm=None):
        return Ctx(self.sched, self.ledger, self.root, transport=httpx.MockTransport(comp), llm=llm or FakeLlm())

    def job(self, **params) -> Job:
        return Job(id="job1", tab="music", kind="ace.simple", params=ace.normalize("ace.simple", {"genre": "J-POP", **params}))

    async def go(self, job, comp, llm=None):
        ctx = self.ctx(comp, llm)
        await ace.prepare(job, ctx)
        await ace.run(job, ctx)

    async def test_happy_path(self):
        comp, llm = FakeAce([RUNNING, DONE]), FakeLlm()
        job = self.job(mood="bright and happy", vocal="female vocal", duration=60)
        await self.go(job, comp, llm)

        self.assertEqual(len(llm.calls), 1)                                   # プランは LLM 1 回
        self.assertEqual(job.result["plan"]["theme"], "夜明けの駅")
        self.assertEqual(comp.formats, [{"prompt": job.result["plan"]["caption"], "lyrics": job.result["plan"]["lyrics"], "temperature": 0.4}])
        body = comp.releases[0]
        self.assertEqual(str(uuid.UUID(body["task_id"])), body["task_id"])    # こちらで決めた UUID
        self.assertEqual(job.remote_id, body["task_id"])
        for k, v in {"thinking": True, "inference_steps": 8, "guidance_scale": 3.0, "audio_duration": 60, "audio_format": "mp3",
                     "vocal_language": "ja", "batch_size": 1, "use_cot_caption": True, "use_cot_language": True,
                     "use_cot_metas": False, "bpm": 96, "key_scale": "C major", "time_signature": "4",
                     "lm_negative_prompt": ace.DEFAULT_NEGATIVE_PROMPT}.items():
            self.assertEqual(body[k], v, k)
        self.assertIn("[Chorus]", body["lyrics"])
        self.assertEqual(body["prompt"], job.result["plan"]["caption"])
        self.assertEqual(comp.polls, 2)

        out = self.ledger.get_output(job.outputs[0])
        self.assertEqual((out["kind"], out["tab"], out["job_id"], out["path"]), ("audio", "music", "job1", "audio/job1.mp3"))
        self.assertEqual((self.root / "audio" / "job1.mp3").read_bytes(), b"ID3fake-mp3")
        self.assertAlmostEqual(out["duration_s"], 61.0)                       # ffprobe が読めなければ ACE の値
        for k, v in {"theme": "夜明けの駅", "genre": "J-POP", "bpm": 100, "key_scale": "A minor", "mood": "bright and happy"}.items():
            self.assertEqual(out["params"][k], v, k)
        self.assertIn("始発を待つホーム", out["params"]["lyrics"])
        self.assertIn("bright J-Pop", out["params"]["caption"])
        self.assertNotIn("plan", out["params"]["result"])

    async def test_lyrics_and_theme_given_skips_the_llm(self):
        comp, llm = FakeAce([DONE]), FakeLlm()
        job = self.job(theme="海", lyrics="[Verse]\n波の音", language="English", model="acestep-v15-turbo")
        await self.go(job, comp, llm)
        self.assertEqual(llm.calls, [])
        self.assertEqual(job.result["plan"]["lyrics"], "[Verse]\n波の音")
        body = comp.releases[0]
        self.assertEqual((body["lyrics"], body["vocal_language"], body["model"]), ("[Verse]\n波の音", "en", "acestep-v15-turbo"))
        self.assertEqual(self.ledger.get_output(job.outputs[0])["params"]["theme"], "海")

    async def test_lyrics_given_without_theme_asks_llm_for_theme_and_caption_only(self):
        llm = FakeLlm("THEME: 朝\nCAPTION: A calm song.")
        job = self.job(lyrics="[Verse]\nあ")
        await ace.prepare(job, self.ctx(FakeAce([DONE]), llm))
        self.assertEqual((job.result["plan"]["theme"], job.result["plan"]["lyrics"]), ("朝", "[Verse]\nあ"))
        self.assertNotIn("complete lyrics", llm.calls[0])

    async def test_instrumental(self):
        comp = FakeAce([DONE])
        job = self.job(instrumental=True, theme="星", vocal="male vocal")
        await self.go(job, comp, FakeLlm())
        body = comp.releases[0]
        self.assertEqual((body["lyrics"], body["vocal_language"], body["instrumental"]), ("[Instrumental]", "", True))
        self.assertEqual(comp.formats[0]["lyrics"], "")
        self.assertNotIn("vocal", body["prompt"])

    async def test_format_input_failure_is_not_fatal(self):
        comp = FakeAce([DONE], format_ok=False)
        job = self.job(theme="a", lyrics="[Verse]\nb")
        await self.go(job, comp)
        body = comp.releases[0]
        self.assertNotIn("bpm", body)
        self.assertNotIn("use_cot_metas", body)                               # ACE の LM に任せる
        self.assertEqual(self.ledger.get_output(job.outputs[0])["params"]["bpm"], 100)   # 生成後の metas

    async def test_llm_failure(self):
        job = self.job()                                                       # 歌詞が無い: LLM が要る
        with self.assertRaises(JobFailed) as cm:
            await ace.prepare(job, self.ctx(FakeAce([DONE]), FakeLlm(error=LlmError("llama-server が途中で終了しました"))))
        self.assertIn("歌詞を作れませんでした", cm.exception.message)
        self.assertIn("llama-server", cm.exception.message)
        job = self.job(lyrics="[Verse]\nあ")                                    # 歌詞があれば、ひな形のキャプションで続ける
        await ace.prepare(job, self.ctx(FakeAce([DONE]), FakeLlm(error=LlmError("x"))))
        self.assertEqual(job.result["plan"]["theme"], "J-POP")
        self.assertTrue(job.result["plan"]["caption"].startswith("A J-POP song"))

    async def test_failure_text(self):
        comp = FakeAce([RUNNING, {"status": 2, "result": result(error="CUDA out of memory")}])
        with self.assertRaises(JobFailed) as cm:
            await self.go(self.job(theme="a", lyrics="[Verse]\nb"), comp)
        self.assertIn("CUDA out of memory", cm.exception.message)
        self.assertEqual(self.ledger.list_outputs(), [])
        with self.assertRaises(JobFailed) as cm:                               # result が文字列のとき
            await self.go(self.job(theme="a", lyrics="x"), FakeAce([{"status": 2, "result": "boom"}]))
        self.assertIn("boom", cm.exception.message)

    async def test_release_rejected_and_cancelled_by_client(self):
        class Rejecting(FakeAce):
            def __call__(self, req):
                if req.url.path == "/release_task":
                    return httpx.Response(429, json={"detail": "Server busy: queue is full"})
                return super().__call__(req)
        with self.assertRaises(JobFailed) as cm:
            await self.go(self.job(theme="a", lyrics="x"), Rejecting([DONE]))
        self.assertIn("Server busy", cm.exception.message)
        with self.assertRaises(JobCancelled):
            await self.go(self.job(theme="a", lyrics="x"), FakeAce([{"status": 2, "result": result(error="cancelled by client")}]))

    async def test_cancel_sends_v1_cancel(self):
        """中止要求 → /v1/cancel(こちらで決めた task_id)→ ACE が status 2(cancelled by client)→ ジョブは cancelled。"""
        comp = FakeAce([RUNNING, lambda c: {"status": 2, "result": result(error="cancelled by client")} if c.cancels else RUNNING])

        class Sched:
            comps = self.sched.comps

            def new_operation(self, kind, name):
                return Operation(kind, name)

            async def ensure(self, name, op=None):
                op.add("準備完了")
        runner = JobRunner(Ctx(Sched(), self.ledger, self.root, transport=httpx.MockTransport(comp), llm=FakeLlm()), ace.HANDLERS, step_poll_s=0.01)
        runner.start()
        self.addAsyncCleanup(runner.stop)
        job = runner.submit("music", "ace.simple", {"genre": "Jazz", "theme": "a", "lyrics": "[Verse]\nb"})
        for _ in range(300):
            if comp.polls >= 1:
                break
            await asyncio.sleep(0.01)
        await runner.cancel(job.id)
        for _ in range(300):
            if job.status == "cancelled":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(job.status, "cancelled")
        self.assertEqual(comp.cancels[0], {"task_id": comp.releases[0]["task_id"]})
        self.assertEqual(self.ledger.list_outputs(), [])

    def test_normalize(self):
        n = ace.normalize
        out = n("ace.simple", {"genre": " Rock ", "theme": " 夜 ", "language": "ko", "duration": "90", "instrumental": 1})
        self.assertEqual((out["genre"], out["theme"], out["language"], out["duration"], out["instrumental"]), ("Rock", "夜", "Korean", 90, True))
        self.assertEqual(n("ace.simple", {})["duration"], -1)
        self.assertEqual((n("ace.simple", {})["genre"], n("ace.simple", {})["language"]), ("J-POP", "Japanese"))
        self.assertEqual(n("ace.simple", {"duration": "auto", "language": "english"})["language"], "English")
        for params in ({"language": "Klingon"}, {"duration": 5}, {"duration": 999}, {"duration": "x"}, {"lyrics": "あ" * 6001},
                       {"model": "a b;c"}):
            with self.assertRaises(ValueError, msg=str(params)):
                n("ace.simple", params)


if __name__ == "__main__":
    unittest.main()
