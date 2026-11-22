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

    def __init__(self, items, format_ok=True, models=None, slots=None, init_error=None):
        self.items, self.format_ok = list(items), format_ok
        self.models, self.slots, self.init_error = models, slots, init_error     # models=None: 一覧の無い古い ACE
        self.formats, self.releases, self.cancels, self.inits, self.polls = [], [], [], [], 0

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
        if path == "/v1/models" and self.models is not None:
            return envelope({"models": self.models, "default_model": "acestep-v15-xl-sft"})
        if path == "/v1/residency" and self.slots is not None:
            return envelope({"slots": self.slots})
        if path == "/v1/init":
            self.inits.append(json.loads(req.content))
            return envelope(code=500, error=self.init_error) if self.init_error else envelope({"slot": self.inits[-1]["slot"]})
        if path == "/v1/audio":
            return httpx.Response(200, content=b"ID3fake-mp3", headers={"content-type": "audio/mpeg"})
        return httpx.Response(404, json={"detail": f"想定外: {req.method} {path}"})


def model(name, loaded=True, **extra):
    return {"name": name, "is_loaded": loaded, **extra}


SLOTS = [{"slot": 1, "model": "acestep-v15-xl-sft", "loaded": True, "on_gpu": True},
         {"slot": 2, "model": None, "loaded": False, "on_gpu": False}]
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

    async def test_old_server_ignores_client_task_id(self):
        """2026-11-20 以前の ACE はこちらの task_id を無視して自分の ID を返す。その ID で問い合わせないと永久に「生成中」。"""
        class OldAce(FakeAce):
            def __call__(self, req):
                if req.url.path == "/release_task":
                    self.releases.append(json.loads(req.content))
                    return envelope({"task_id": "server-made-id", "status": "queued", "queue_position": 1})
                if req.url.path == "/query_result":
                    asked = json.loads(req.content)["task_id_list"][0]
                    if asked != "server-made-id":
                        return envelope([{"task_id": asked, "result": "[]", "status": 0}])   # 知らない ID はずっと 0
                return super().__call__(req)
        comp = OldAce([DONE])
        job = self.job(lyrics="[Verse]\nla", theme="t")
        await self.go(job, comp)
        self.assertEqual(job.remote_id, "server-made-id")
        self.assertEqual(len(job.outputs), 1)      # 状態はランナーが付けるので、ここでは取り込めたことを見る

    async def test_cancel_on_old_server_without_v1_cancel_stops_waiting(self):
        """/v1/cancel の無い古い ACE では、サーバは最後まで作るがこちらは待つのをやめて cancelled にする。"""
        class NoCancelAce(FakeAce):
            def __call__(self, req):
                if req.url.path == "/v1/cancel":
                    return httpx.Response(404, json={"detail": "Not Found"})
                return super().__call__(req)
        comp = NoCancelAce([RUNNING, RUNNING, RUNNING])
        job = self.job(lyrics="[Verse]\nla", theme="t")
        ctx = self.ctx(comp)
        await ace.prepare(job, ctx)
        job.cancel_requested = True
        with self.assertRaises(ace.JobCancelled):
            await ace.run(job, ctx)

    # ---- 作り方・詳細設定 ----

    async def test_manual_caption_skips_the_llm_caption(self):
        """書いたキャプションは作り直さない(整えるだけ)。テーマと歌詞が揃っていれば LLM は呼ばない。"""
        comp, llm = FakeAce([DONE]), FakeLlm()
        job = self.job(theme="海", lyrics="[Verse]\n波", caption="  A calm\nsea song  ", caption_auto=True)
        await self.go(job, comp, llm)
        self.assertEqual(llm.calls, [])
        self.assertEqual(comp.releases[0]["prompt"], "A calm sea song.")
        self.assertEqual(self.ledger.get_output(job.outputs[0])["params"]["caption"], "A calm sea song.")

    async def test_manual_caption_with_missing_lyrics_asks_llm_for_theme_and_lyrics_only(self):
        llm = FakeLlm("THEME: 朝焼け\nCAPTION: A different caption.\n{\"recommended_duration\": 60}\n[Verse]\nあ")
        job = self.job(caption="My own caption.")
        await ace.prepare(job, self.ctx(FakeAce([DONE]), llm))
        self.assertEqual(len(llm.calls), 1)
        self.assertNotIn("CAPTION:", llm.calls[0])                  # キャプションは頼まない
        self.assertEqual(job.result["plan"]["caption"], "My own caption.")
        self.assertEqual((job.result["plan"]["theme"], job.result["plan"]["lyrics"]), ("朝焼け", "[Verse]\nあ"))

    async def test_caption_auto_makes_only_the_caption_when_theme_and_lyrics_are_given(self):
        llm = FakeLlm("A mellow song with a warm mood and a clear female vocal")
        job = self.job(theme="海", lyrics="[Verse]\n波", caption_auto=True, mood="calm and gentle")
        await ace.prepare(job, self.ctx(FakeAce([DONE]), llm))
        self.assertEqual(len(llm.calls), 1)
        self.assertIn("Theme/Concept: 海", llm.calls[0])
        self.assertIn("波", llm.calls[0])                           # 歌詞を参考にする
        self.assertEqual(job.result["plan"]["caption"], "A mellow song with a warm mood and a clear female vocal.")
        self.assertEqual(job.result["plan"]["lyrics"], "[Verse]\n波")
        job = self.job(theme="海", lyrics="[Verse]\n波", caption_auto=False)        # 自動オフ: ひな形
        llm = FakeLlm()
        await ace.prepare(job, self.ctx(FakeAce([DONE]), llm))
        self.assertEqual(llm.calls, [])
        self.assertTrue(job.result["plan"]["caption"].startswith("A J-POP song"))

    async def test_thinking_off_asks_for_a_detailed_caption_separately(self):
        llm = FakeLlm()
        job = self.job(theme="海", lyrics="[Verse]\n波", caption_auto=True, thinking=False)
        await ace.prepare(job, self.ctx(FakeAce([DONE]), llm))
        self.assertIn("3-5 complete English prose sentences", llm.calls[0])

    async def test_advanced_settings_reach_release_task(self):
        comp = FakeAce([DONE], models=[model("acestep-v15-xl-sft")])
        job = self.job(theme="a", lyrics="[Verse]\nb", model="acestep-v15-xl-sft", steps=30, seed=1234, negative_prompt="muddy",
                       shift="3", infer_method="sde", thinking=False)
        await self.go(job, comp)
        body = comp.releases[0]
        for k, v in {"model": "acestep-v15-xl-sft", "inference_steps": 30, "seed": 1234, "lm_negative_prompt": "muddy",
                     "shift": 3.0, "infer_method": "SDE", "thinking": False, "guidance_scale": 7.0}.items():
            self.assertEqual(body[k], v, k)
        self.assertEqual(job.result["seed"], 1234)
        self.assertEqual(self.ledger.get_output(job.outputs[0])["params"]["seed"], 1234)
        comp = FakeAce([DONE])                                                   # 指定なし: turbo の 8 ステップ、ガイダンス 3
        await self.go(self.job(theme="a", lyrics="[Verse]\nb"), comp)
        for k in ("model", "shift", "infer_method"):
            self.assertNotIn(k, comp.releases[0])
        self.assertEqual((comp.releases[0]["inference_steps"], comp.releases[0]["guidance_scale"]), (8, 3.0))
        comp = FakeAce([DONE], models=[model("acestep-v15-base")])               # turbo 以外は 50 ステップ
        await self.go(self.job(theme="a", lyrics="[Verse]\nb", model="acestep-v15-base", batch_size=4), comp)
        self.assertEqual(comp.releases[0]["inference_steps"], 50)

    async def test_batch_registers_every_file(self):
        two = {"status": 1, "result": json.dumps([{"file": "/v1/audio?path=a.mp3", "metas": {"bpm": 90}}, {"file": "/v1/audio?path=b.mp3"}])}
        comp = FakeAce([two])
        job = self.job(theme="a", lyrics="[Verse]\nb", batch_size=2)
        await self.go(job, comp)
        self.assertEqual(comp.releases[0]["batch_size"], 2)
        self.assertEqual(len(job.outputs), 2)
        names = sorted(self.ledger.get_output(o)["path"] for o in job.outputs)
        self.assertEqual(names, ["audio/job1_1.mp3", "audio/job1_2.mp3"])
        self.assertTrue(all((self.root / n).exists() for n in names))
        self.assertEqual(self.job(batch_size=1).params["batch_size"], 1)
        self.assertEqual(ace.clamp_batch("acestep-v15-xl-sft", 4), 2)            # SFT は 2 まで

    async def test_model_not_loaded_is_loaded_into_a_non_primary_slot(self):
        comp = FakeAce([DONE], models=[model("acestep-v15-xl-sft"), model("acestep-v15-turbo", loaded=False, vram_ok=True)], slots=SLOTS)
        job = self.job(theme="a", lyrics="[Verse]\nb", model="acestep-v15-turbo")
        await self.go(job, comp)
        self.assertEqual(comp.inits, [{"model": "acestep-v15-turbo", "slot": 2}])
        self.assertEqual(comp.releases[0]["model"], "acestep-v15-turbo")
        comp = FakeAce([DONE], models=[model("acestep-v15-turbo")], slots=SLOTS)  # 読込済みなら入れ替えない
        await self.go(self.job(theme="a", lyrics="[Verse]\nb", model="acestep-v15-turbo"), comp)
        self.assertEqual(comp.inits, [])

    async def test_model_that_does_not_fit_vram_is_refused(self):
        comp = FakeAce([DONE], models=[model("acestep-v15-xl-sft", loaded=False, vram_ok=False, min_vram_gb=16)], slots=SLOTS)
        with self.assertRaises(JobFailed) as cm:
            await self.go(self.job(theme="a", lyrics="[Verse]\nb", model="acestep-v15-xl-sft"), comp)
        self.assertIn("載りません", cm.exception.message)
        self.assertIn("16GB", cm.exception.message)
        self.assertEqual((comp.inits, comp.releases), ([], []))

    async def test_model_init_failure_and_single_slot(self):
        loaded_false = [model("acestep-v15-turbo", loaded=False)]
        comp = FakeAce([DONE], models=loaded_false, slots=SLOTS, init_error="out of memory")
        with self.assertRaises(JobFailed) as cm:
            await self.go(self.job(theme="a", lyrics="x", model="acestep-v15-turbo"), comp)
        self.assertIn("out of memory", cm.exception.message)
        comp = FakeAce([DONE], models=loaded_false, slots=SLOTS[:1])
        with self.assertRaises(JobFailed) as cm:
            await self.go(self.job(theme="a", lyrics="x", model="acestep-v15-turbo"), comp)
        self.assertIn("スロットが 1 本", cm.exception.message)
        self.assertEqual(comp.inits, [])

    def test_pick_slot_and_default_steps(self):
        slots = [{"slot": 1, "model": "a", "loaded": True, "on_gpu": True}, {"slot": 2, "model": "b", "loaded": True, "on_gpu": False},
                 {"slot": 3, "model": None, "loaded": False, "on_gpu": False}]
        self.assertEqual(ace.pick_slot(slots, "x"), 3)          # 未読込のスロット
        self.assertEqual(ace.pick_slot(slots, "b"), 2)          # すでに入っているスロット
        self.assertIsNone(ace.pick_slot(slots[:1], "x"))
        self.assertEqual((ace.default_steps("acestep-v15-xl-turbo"), ace.default_steps("acestep-v15-sft"), ace.default_steps(None)), (8, 50, 8))

    async def test_list_models(self):
        comp = FakeAce([], models=[model("acestep-v15-xl-sft"), model("acestep-v15-turbo", loaded=False, vram_ok=False, min_vram_gb=24)])
        out = await ace.list_models("http://ace.test", httpx.MockTransport(comp))
        self.assertEqual(out["default_model"], "acestep-v15-xl-sft")
        by = {m["name"]: m for m in out["models"]}
        self.assertEqual((by["acestep-v15-xl-sft"]["is_loaded"], by["acestep-v15-xl-sft"]["vram_ok"], by["acestep-v15-xl-sft"]["is_default"]), (True, True, True))
        self.assertEqual((by["acestep-v15-turbo"]["is_loaded"], by["acestep-v15-turbo"]["vram_ok"]), (False, False))

        def down(req):
            raise httpx.ConnectError("refused")
        out = await ace.list_models("http://ace.test", httpx.MockTransport(down))
        self.assertEqual((out["models"], out["default_model"], out["note"]), ([], None, "ACE が起動していません"))

    # ---- おまかせ(sample_query) ----

    async def test_omakase_sends_sample_query_and_uses_no_llm(self):
        comp, llm = FakeAce([RUNNING, DONE]), FakeLlm()
        job = Job(id="job1", tab="music", kind="ace.omakase",
                  params=ace.normalize("ace.omakase", {"query": " 雨の夜に聴く切ないバラード ", "language": "ja", "duration": 90}))
        ctx = self.ctx(comp, llm)
        await ace.prepare(job, ctx)
        self.assertEqual(job.result["plan"]["theme"], "雨の夜に聴く切ないバラード")      # 生成中の画面に出る
        await ace.run(job, ctx)
        self.assertEqual((llm.calls, comp.formats), ([], []))                    # LLM も format_input も使わない
        body = comp.releases[0]
        for k, v in {"sample_query": "雨の夜に聴く切ないバラード", "prompt": "", "lyrics": "", "thinking": True, "use_cot_caption": True,
                     "use_cot_language": True, "vocal_language": "ja", "audio_duration": 90, "batch_size": 1}.items():
            self.assertEqual(body[k], v, k)
        self.assertNotIn("use_cot_metas", body)
        out = self.ledger.get_output(job.outputs[0])
        self.assertEqual((out["params"]["theme"], out["params"]["query"], out["params"]["kind"]), ("雨の夜に聴く切ないバラード",) * 2 + ("ace.omakase",))
        self.assertEqual(out["params"]["bpm"], 100)

    async def test_omakase_instrumental_and_params(self):
        comp = FakeAce([DONE])
        job = Job(id="job1", tab="music", kind="ace.omakase",
                  params=ace.normalize("ace.omakase", {"query": "朝のカフェ", "instrumental": True, "thinking": False, "batch_size": 2}))
        await self.go(job, comp)
        body = comp.releases[0]
        self.assertIn("instrumental", body["sample_query"])
        self.assertEqual((body["instrumental"], body["vocal_language"], body["thinking"], body["batch_size"]), (True, "", True, 2))
        with self.assertRaises(ValueError):
            ace.normalize("ace.omakase", {"query": "  "})

    def test_normalize(self):
        n = ace.normalize
        out = n("ace.simple", {"genre": " Rock ", "theme": " 夜 ", "language": "ko", "duration": "90", "instrumental": 1})
        self.assertEqual((out["genre"], out["theme"], out["language"], out["duration"], out["instrumental"]), ("Rock", "夜", "Korean", 90, True))
        self.assertEqual(n("ace.simple", {})["duration"], -1)
        self.assertEqual((n("ace.simple", {})["genre"], n("ace.simple", {})["language"]), ("J-POP", "Japanese"))
        self.assertEqual(n("ace.simple", {"duration": "auto", "language": "english"})["language"], "English")
        out = n("ace.simple", {"steps": "30", "seed": "7", "shift": "auto", "infer_method": "auto", "batch_size": "2", "thinking": False})
        self.assertEqual((out["steps"], out["seed"], out["shift"], out["infer_method"], out["batch_size"], out["thinking"]), (30, 7, None, None, 2, False))
        self.assertEqual((n("ace.simple", {})["thinking"], n("ace.simple", {})["caption_auto"], n("ace.simple", {})["negative_prompt"]),
                         (True, None, ace.DEFAULT_NEGATIVE_PROMPT))
        for params in ({"language": "Klingon"}, {"duration": 5}, {"duration": 999}, {"duration": "x"}, {"lyrics": "あ" * 6001},
                       {"model": "a b;c"}, {"steps": 0}, {"batch_size": 5}, {"seed": -1}, {"shift": 9}, {"infer_method": "RK4"}):
            with self.assertRaises(ValueError, msg=str(params)):
                n("ace.simple", params)


if __name__ == "__main__":
    unittest.main()
