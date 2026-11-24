import asyncio
import hashlib
import io
import json
import re
import shutil
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import httpx
from PIL import Image

from studio import config, media, mv, mv_audio, mvclip
from studio.handlers import h3
from studio.handlers import mv as mvh
from studio.jobs import Ctx, Job, JobCancelled, JobFailed, JobRunner
from studio.ledger import Ledger
from studio.llm import LlmError
from studio.scheduler import Operation

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def png_bytes(size=(64, 48)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (10, 120, 200)).save(buf, "PNG")
    return buf.getvalue()


def parse_form(req: httpx.Request) -> list:
    """multipart の本文を [(name, filename, content-type, bytes)] にする(順番どおり)。"""
    boundary = re.search(r"boundary=([^;]+)", req.headers["content-type"]).group(1).encode()
    out = []
    for part in req.content.split(b"--" + boundary)[1:-1]:
        head, _, body = part.partition(b"\r\n\r\n")
        name = re.search(rb'name="([^"]*)"', head).group(1).decode()
        fn = re.search(rb'filename="([^"]*)"', head)
        ct = re.search(rb"Content-Type: ([^\r\n]+)", head)
        out.append((name, fn.group(1).decode() if fn else None, ct.group(1).decode() if ct else None, body[:-2]))
    return out


def plan_of(durations, ratios=None) -> list:
    t, out = 0.0, []
    for i, d in enumerate(durations, start=1):
        ratio = (ratios or {}).get(i, 0.8 if i % 2 else 0.0)
        out.append({"index": i, "start": t, "end": t + d, "duration": d, "vocal_ratio": ratio, "kind": "vocal" if ratio >= 0.35 else "instrumental",
                    "boundary_kind": "bar", "boundary_score": 10.0})
        t += d
    return out


class FakeSched:
    def __init__(self):
        self.comps = {"h3": types.SimpleNamespace(spec=types.SimpleNamespace(base_url="http://h3.test"))}

    def new_operation(self, kind, name):
        return Operation(kind, name)

    async def ensure(self, name, op=None):
        op.add("準備完了")


class FakeAudio:
    """mv_audio の normalize / separate / analyze / cut の代わり(道具用の Python も ffmpeg も使わない)。"""

    def __init__(self, durations=(8.0, 6.0, 7.0), has_vocals=True, ratios=None):
        self.plan = {"duration": sum(durations), "bpm": 118.5, "has_vocals": has_vocals, "scenes": plan_of(durations, ratios)}
        self.calls, self.job, self.snap, self.hold = [], None, None, None
        self.error = {}                # 名前 -> 投げる例外

    def _fail(self, name):
        if name in self.error:
            raise self.error[name]

    async def normalize(self, src, dst):
        self.calls.append(("normalize", Path(src), Path(dst)))
        self._fail("normalize")
        Path(dst).write_bytes(b"RIFFsong")
        return Path(dst)

    async def separate(self, wav, out_dir, device="cpu", on_progress=None, **kw):
        self.calls.append(("separate", Path(wav), Path(out_dir), device))
        self._fail("separate")
        if on_progress is not None:
            on_progress("progress 5/10")
            on_progress("noise line")
            if self.job is not None:
                self.snap = (self.job.progress, self.job.message)
        if self.hold is not None:
            try:
                await self.hold.wait()
            except asyncio.CancelledError:
                self.calls.append(("cancelled",))
                raise
        Path(out_dir, "vocals.wav").write_bytes(b"RIFFvocals")
        Path(out_dir, "accompaniment.wav").write_bytes(b"RIFFacc")
        return {"vocals": Path(out_dir, "vocals.wav"), "accompaniment": Path(out_dir, "accompaniment.wav"), "seconds": 1.0, "device": device}

    async def analyze(self, song_wav, vocals_wav, min_s=5.0, max_s=10.0, target_s=8.0, **kw):
        self.calls.append(("analyze", Path(song_wav), Path(vocals_wav), min_s, max_s, target_s))
        self._fail("analyze")
        return json.loads(json.dumps(self.plan))

    async def cut(self, src, start, end, dst, *, rate=0, channels=0, pad_to=0.0):
        self.calls.append(("cut", Path(src), start, end, Path(dst), rate, channels, pad_to))
        self._fail("cut")
        Path(dst).write_bytes(b"RIFFscene")
        return Path(dst)

    def patch(self, test):
        for name in ("normalize", "separate", "analyze", "cut"):
            p = mock.patch.object(mv_audio, name, getattr(self, name))
            p.start()
            test.addCleanup(p.stop)


class SmartChat:
    """管理役の LLM の代わり: 概要の依頼には概要を、batch の依頼には頼まれた番号どおりの演出を返す。"""

    def __init__(self, error=None):
        self.calls, self.error, self.hold = [], error, None

    async def __call__(self, system, user, max_tokens=0, temperature=0):
        self.calls.append(user)
        if self.error:
            raise self.error
        if self.hold is not None:
            await self.hold.wait()
        if "fixed visual appearance" in user:                         # 人物の見た目の抜き出し: 説明をそのまま subject に
            return json.dumps({"subject": user.rsplit("DESCRIPTION: ", 1)[-1].strip(), "face": "", "hair": "", "outfit": "", "style": ""})
        if "MV treatment" in user:
            return json.dumps({"title": "夜明け", "visual_theme": "dawn city", "color_script": "blue to gold", "story_arc": "a", "continuity_rules": ["r"]})
        indexes = json.loads(re.search(r"with indexes (\[[^\]]*\])", user).group(1))
        return json.dumps([{"index": i, "emotion": f"emo{i}", "shot": "close", "camera": "static", "prompt": f"Wide shot of the singer {i}"} for i in indexes])


class FakeH3:
    """H3 の /api/ref2va の代わり(httpx.MockTransport)。生成の POST は、進捗が何回か読まれるまで返らない。"""

    def __init__(self, video=b"fakevideo", wait_polls=3, wait_interrupt=False, **result):
        self.result = {"job_id": "remote1", "video_url": "/outputs/out_1.mp4", "duration_s": 141 / 24, "total_elapsed_s": 42.5,
                       "num_inference_steps": 4, "seed": 99, "peak_vram_gb": 11.0, **result}
        self.video, self.wait_polls, self.wait_interrupt = video, wait_polls, wait_interrupt
        self.status, self.detail = 200, None
        self.polls, self.interrupts, self.posts, self.downloads, self.seen = 0, [], [], [], []
        self.job = None

    async def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/api/progress":
            self.polls += 1
            if self.polls == 1:
                return httpx.Response(200, json={"job_id": "old000", "phase": "done", "step": 4, "total_steps": 4})
            return httpx.Response(200, json={"job_id": "remote1", "phase": "denoising", "step": 2, "total_steps": 4, "message": ""})
        if req.method == "POST" and path == "/api/interrupt":
            self.interrupts.append(json.loads(req.content))
            return httpx.Response(200, json={"interrupted": True})
        if req.method == "POST" and path == "/api/ref2va":
            self.posts.append(parse_form(req))
            for _ in range(500):
                if self.interrupts if self.wait_interrupt else self.polls >= self.wait_polls:
                    break
                await asyncio.sleep(0.01)
            if self.job is not None:
                self.seen.append((self.job.progress, self.job.message, self.job.remote_id))
            if self.status != 200:
                return httpx.Response(self.status, json={"detail": self.detail})
            return httpx.Response(200, json=self.result)
        if req.method == "GET" and path.startswith("/outputs/"):
            self.downloads.append(path)
            return httpx.Response(200, content=self.video)
        return httpx.Response(404, json={"detail": f"想定外: {req.method} {path}"})


class FfmpegSpy:
    """mv_audio._run_ffmpeg の代わり: 引数を残し、出力先(.part を外した dst)に中身を書く。ffmpeg が無くても動かせる。"""

    def __init__(self):
        self.calls = []                # (argv, dst, what, 一覧ファイルの中身 or None)

    def __call__(self, argv, dst, what):
        listing = Path(argv[argv.index("-i") + 1]) if "concat" in argv else None
        self.calls.append((list(argv), Path(dst), what, listing.read_text(encoding="utf-8") if listing is not None else None))
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_bytes(b"ffmpeg-output")
        return Path(dst)

    def patch(self, test):
        for name, value in (("_run_ffmpeg", self), ("_ffmpeg", lambda: "ffmpeg")):
            p = mock.patch.object(mv_audio, name, value)
            p.start()
            test.addCleanup(p.stop)


class HandlerBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.ledger = Ledger(self.root)
        self.addCleanup(self.ledger.close)
        p = mock.patch.object(h3, "POLL_S", 0.01)
        p.start()
        self.addCleanup(p.stop)
        self.thumb = mock.patch.object(media, "video_thumb", return_value=False)
        self.thumb.start()
        self.addCleanup(self.thumb.stop)
        self.probe = mock.patch.object(media, "probe", return_value={"duration_s": None, "width": None, "height": None})
        self.probe.start()
        self.addCleanup(self.probe.stop)
        self.audio = FakeAudio()
        self.audio.patch(self)
        self.song = self.add("audio", "song.mp3", {"theme": "夜明けの駅", "genre": "Ballad"})
        self.image = self.add("image", "girl.png", {"prompt": "a girl with silver hair"})
        self.chat = SmartChat()
        self.h3 = FakeH3()

    def add(self, kind, name, params):
        p = self.root / "uploads" / name
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(png_bytes() if kind == "image" else b"RIFFfake")
        return self.ledger.add_output(kind, p, "upload", filename=name, params=params, duration_s=30.0 if kind == "audio" else None)

    def ctx(self, llm="default") -> Ctx:
        async def release_before_gpu():
            pass
        return Ctx(FakeSched(), self.ledger, self.root, transport=httpx.MockTransport(self.h3),
                   llm=types.SimpleNamespace(chat=self.chat, release_before_gpu=release_before_gpu) if llm == "default" else llm)

    def project(self, state="analyzing", scenes=None, analysis=True, pid="proj1", song=None, **params) -> str:
        resolved = mv.resolve_params(self.ledger, {"song_id": (song or self.song)["id"], "image_id": self.image["id"], **params})
        if analysis:
            resolved["analysis"] = {"duration": 21.0, "bpm": 118.5, "has_vocals": True, "scenes": 3}
        self.ledger.create_mv_project(pid, state, resolved, scenes if scenes is not None else [], "")
        return pid

    def scenes(self, durations=(8.0, 6.0, 7.0), ratios=None, prompts=True, seeds=True) -> list:
        scenes = mv.make_scenes(mvclip.quantize_scene_plan(plan_of(durations, ratios)))
        for s in scenes:
            if prompts:
                s["prompt"] = f"prompt {s['index']}"
            if seeds:
                s["seed"] = 1000 + s["index"]
        return scenes

    def job(self, kind, pid="proj1", **extra) -> Job:
        return Job(id="job123456789", tab="mv", kind=kind, params=mvh.normalize(kind, {"project_id": pid, **extra}, self.ledger))

    def work(self, pid="proj1") -> Path:
        return mv.work_dir(self.root, pid)

    def proj(self, pid="proj1") -> dict:
        return self.ledger.get_mv_project(pid)


class NormalizeTest(HandlerBase):
    def test_validates_project_and_scene(self):
        pid = self.project(scenes=self.scenes())
        self.assertEqual(mvh.normalize("mv.analyze", {"project_id": pid, "junk": 1}, self.ledger), {"project_id": pid})
        self.assertEqual(mvh.normalize("mv.scene", {"project_id": pid, "scene": "2"}, self.ledger), {"project_id": pid, "scene": 2})
        for kind, params, word in (("mv.analyze", {}, "制作が見つかりません"), ("mv.script", {"project_id": "nope"}, "制作が見つかりません"),
                                   ("mv.scene", {"project_id": pid}, "シーンの番号"), ("mv.scene", {"project_id": pid, "scene": "x"}, "シーンの番号"),
                                   ("mv.scene", {"project_id": pid, "scene": 9}, "シーン 9 はありません")):
            with self.assertRaises(ValueError, msg=str(params)) as cm:
                mvh.normalize(kind, params, self.ledger)
            self.assertIn(word, str(cm.exception))

    def test_registered_with_the_declared_lanes(self):
        from studio.handlers import HANDLERS
        for kind, component, gpu in (("mv.analyze", None, False), ("mv.script", None, False), ("mv.scene", "h3", True), ("mv.assemble", None, False)):
            self.assertEqual((HANDLERS[kind].component, HANDLERS[kind].gpu), (component, gpu), kind)
            self.assertIs(HANDLERS[kind], mvh.HANDLERS[kind])
        self.assertIs(HANDLERS["mv.scene"].cancel, h3.cancel)           # シーンの中止は、H3 の interrupt


class AnalyzeTest(HandlerBase):
    async def test_normalize_separate_analyze_and_store(self):
        self.project(analysis=False, min_s=6, max_s=9)
        job = self.job("mv.analyze")
        self.audio.job = job
        await mvh.run_analyze(job, self.ctx())
        work = self.work()
        song, wav = self.ledger.abs_path(self.song["path"]), work / "song.wav"
        self.assertEqual([c[0] for c in self.audio.calls], ["normalize", "separate", "analyze"])
        self.assertEqual(self.audio.calls[0][1:], (song, wav))
        self.assertEqual(self.audio.calls[1][1:], (wav, work, "cpu"))
        self.assertEqual(self.audio.calls[2][1:], (wav, work / "vocals.wav", 6.0, 9.0, 7.5))        # 目標の長さは (最短 + 最長) / 2
        self.assertAlmostEqual(self.audio.snap[0], 0.05 + 0.85 * 0.5)
        self.assertEqual(self.audio.snap[1], "ボーカルを分離しています 50%")                        # 「progress 5/10」の行を割合にして出す
        proj = self.proj()
        self.assertEqual(proj["params"]["analysis"], {"duration": 21.0, "bpm": 118.5, "has_vocals": True, "scenes": 3})
        self.assertEqual(job.result, proj["params"]["analysis"])
        self.assertEqual(len(proj["scenes"]), 3)
        s = proj["scenes"][1]
        self.assertEqual((s["index"], s["start"], s["end"], s["duration"], s["kind"], s["status"], s["prompt"], s["seed"]), (2, 8.0, 14.0, 6.0, "instrumental", "pending", "", None))
        self.assertEqual((s["start_frame"], s["end_frame"], s["frame_count"]), (192, 336, 144))   # コマの格子に載っている
        self.assertEqual(sum(sc["frame_count"] for sc in proj["scenes"]), 21 * 24)
        self.assertFalse(proj["params"]["assembled"])

    async def test_device_comes_from_settings(self):
        self.project(analysis=False)
        for configured, expected in (("cuda", "cuda"), ("", "cpu"), ("AUTO", "auto"), ("tpu", "cpu")):
            self.audio.calls.clear()
            shutil.rmtree(self.work(), ignore_errors=True)          # 変換と分離の済んだ結果があると、分離を飛ばすので
            with mock.patch.object(config.settings, "mv", {"device": configured}):
                await mvh.run_analyze(self.job("mv.analyze"), self.ctx())
            self.assertEqual(self.audio.calls[1][3], expected, configured)

    async def test_song_without_vocals_still_makes_scenes(self):
        self.audio = FakeAudio(has_vocals=False, ratios={1: 0.0, 2: 0.0, 3: 0.0})
        self.audio.patch(self)
        self.project(analysis=False)
        await mvh.run_analyze(self.job("mv.analyze"), self.ctx())
        proj = self.proj()
        self.assertFalse(proj["params"]["analysis"]["has_vocals"])
        self.assertEqual([s["kind"] for s in proj["scenes"]], ["instrumental"] * 3)

    async def test_finished_separation_is_reused_after_a_failure(self):
        self.project(analysis=False)
        self.audio.error["analyze"] = mv_audio.MvAudioError("曲のシーン分割に失敗しました: メモリ不足", "tail")
        with self.assertRaises(JobFailed) as cm:
            await mvh.run_analyze(self.job("mv.analyze"), self.ctx())
        self.assertEqual((cm.exception.message, cm.exception.log_tail), ("曲のシーン分割に失敗しました: メモリ不足", "tail"))
        self.assertEqual([c[0] for c in self.audio.calls], ["normalize", "separate", "analyze"])
        self.audio.calls.clear()
        self.audio.error.clear()
        job = self.job("mv.analyze")
        await mvh.run_analyze(job, self.ctx())                      # 再開: 変換と分離は済んでいるので、分析だけやり直す
        self.assertEqual([c[0] for c in self.audio.calls], ["analyze"])
        self.assertEqual(len(self.proj()["scenes"]), 3)

    async def test_missing_tools_fail_with_the_hint(self):
        self.project(analysis=False)
        self.audio.error["separate"] = mv_audio.MvToolsMissing(f"MV の音声処理の道具(Demucs と librosa)が見つかりません: x。{mv_audio.MISSING_HINT}")
        with self.assertRaises(JobFailed) as cm:
            await mvh.run_analyze(self.job("mv.analyze"), self.ctx())
        self.assertIn("setup --mv を実行してください", cm.exception.message)
        self.assertEqual(self.proj()["scenes"], [])

    async def test_song_gone_fails(self):
        self.project(analysis=False)
        self.ledger.delete_output(self.song["id"])
        with self.assertRaises(JobFailed) as cm:
            await mvh.run_analyze(self.job("mv.analyze"), self.ctx())
        self.assertIn("曲が見つかりません", cm.exception.message)
        self.assertEqual(self.audio.calls, [])

    async def test_no_scenes_fails(self):
        self.project(analysis=False)
        self.audio.plan["scenes"] = []
        with self.assertRaises(JobFailed) as cm:
            await mvh.run_analyze(self.job("mv.analyze"), self.ctx())
        self.assertIn("シーンに分けられませんでした", cm.exception.message)
        self.assertEqual(self.proj()["scenes"], [])

    async def test_cancel_stops_the_running_work(self):
        self.project(analysis=False)
        self.audio.hold = asyncio.Event()
        runner = JobRunner(self.ctx(), {"mv.analyze": mvh.HANDLERS["mv.analyze"]}, step_poll_s=0.01)
        runner.start()
        self.addAsyncCleanup(runner.stop)
        job = runner.submit("mv", "mv.analyze", {"project_id": "proj1"})
        for _ in range(300):
            if any(c[0] == "separate" for c in self.audio.calls):
                break
            await asyncio.sleep(0.01)
        await runner.cancel(job.id)
        for _ in range(300):
            if job.status == "cancelled":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(job.status, "cancelled")
        self.assertIn(("cancelled",), self.audio.calls)             # 分離(の子プロセス)に取り消しが届いた
        self.assertEqual(self.proj()["scenes"], [])

    async def test_cancel_before_it_starts(self):
        self.project(analysis=False)
        job = self.job("mv.analyze")
        job.cancel_requested = True
        with self.assertRaises(JobCancelled):
            await mvh.run_analyze(job, self.ctx())
        self.assertEqual(self.audio.calls, [])


class ScriptTest(HandlerBase):
    async def test_prompts_seeds_and_overview_are_stored(self):
        self.project(state="scripting", scenes=self.scenes(prompts=False, seeds=False), seed=100, character="a girl in a red coat", concept="dawn")
        job = self.job("mv.script")
        await mvh.run_script(job, self.ctx())
        proj = self.proj()
        for s in proj["scenes"]:
            self.assertIn("subject_definitions:", s["prompt"])
            self.assertIn("<Subject 1> is the exact character shown in <Picture 1>: a girl in a red coat.", s["prompt"])
            self.assertEqual(s["emotion"], f"emo{s['index']}")
            self.assertEqual(s["seed"], mvclip.scene_seed(100, s["index"]))
            self.assertEqual("physically performs the supplied vocal" in s["prompt"], s["vocal_ratio"] >= 0.35)       # 歌う区間と歌わない区間
        self.assertEqual(proj["params"]["script"]["title"], "夜明け")
        self.assertEqual(len(self.chat.calls), 3)                       # 人物の見た目 1 回 + 概要 1 回 + 3 シーンの batch 1 回
        self.assertIn("DESCRIPTION: a girl in a red coat", self.chat.calls[0])
        self.assertEqual(proj["params"]["character_facts"], {"subject": "a girl in a red coat"})
        self.assertIn("USER_CONCEPT: dawn", self.chat.calls[1])
        self.assertEqual((job.progress, job.message), (1.0, "演出を書いています 3 / 3 シーン"))
        self.assertNotIn("VERTICAL frame", self.chat.calls[2])          # 16:9 は横長

    async def test_portrait_frame_and_default_seed_and_default_subject(self):
        self.project(state="scripting", scenes=self.scenes(prompts=False, seeds=False), aspect="9:16", character="")
        self.ledger.update_mv_project("proj1", params_patch={"character": ""})
        await mvh.run_script(self.job("mv.script"), self.ctx())
        proj = self.proj()
        base = int(hashlib.sha256(b"proj1").hexdigest()[:8], 16)           # seed 未指定なら、制作の ID から決める(同じ制作は毎回同じ)
        self.assertEqual([s["seed"] for s in proj["scenes"]], [mvclip.scene_seed(base, i) for i in (1, 2, 3)])
        self.assertIn("VERTICAL frame", self.chat.calls[1])
        self.assertIn("Vertical frame composition", proj["scenes"][0]["prompt"])
        self.assertIn("visible in the image", proj["scenes"][0]["prompt"])       # 人物の説明が無い: 中立の文

    async def test_failures_become_job_failed(self):
        self.project(state="scripting", scenes=self.scenes(prompts=False, seeds=False))
        self.chat.error = LlmError("llama-server を起動できません")
        with self.assertRaises(JobFailed) as cm:
            await mvh.run_script(self.job("mv.script"), self.ctx())
        self.assertEqual(cm.exception.message, "llama-server を起動できません")
        with self.assertRaises(JobFailed) as cm:
            await mvh.run_script(self.job("mv.script"), self.ctx(llm=None))
        self.assertIn("LLM が設定されていません", cm.exception.message)
        self.assertTrue(all(not s["prompt"] for s in self.proj()["scenes"]))          # 失敗したら、何も書かない

        async def wrong(system, user, max_tokens=0, temperature=0):          # 番号が合わない答えが続く
            return '{"title": "x"}' if "MV treatment" in user else '[{"index": 9, "prompt": "p"}, {"index": 8, "prompt": "p"}, {"index": 7, "prompt": "p"}]'
        with self.assertRaises(JobFailed) as cm:
            await mvh.run_script(self.job("mv.script"), self.ctx(llm=types.SimpleNamespace(chat=wrong)))
        self.assertIn("番号が合いません", cm.exception.message)

    async def test_cancel_while_the_llm_is_thinking(self):
        self.project(state="scripting", scenes=self.scenes(prompts=False, seeds=False))
        self.chat.hold = asyncio.Event()
        runner = JobRunner(self.ctx(), {"mv.script": mvh.HANDLERS["mv.script"]}, step_poll_s=0.01)
        runner.start()
        self.addAsyncCleanup(runner.stop)
        job = runner.submit("mv", "mv.script", {"project_id": "proj1"})
        for _ in range(300):
            if self.chat.calls:
                break
            await asyncio.sleep(0.01)
        await runner.cancel(job.id)
        for _ in range(300):
            if job.status == "cancelled":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(job.status, "cancelled")
        self.assertTrue(all(not s["prompt"] for s in self.proj()["scenes"]))


def scene_ctx_project(test, **kw) -> str:
    pid = test.project(state="generating", scenes=test.scenes(), **kw)
    work = test.work()
    work.mkdir(parents=True, exist_ok=True)
    (work / "vocals.wav").write_bytes(b"RIFFvocals")
    (work / "song.wav").write_bytes(b"RIFFsong")
    return pid


class SceneTest(HandlerBase):
    async def test_ref2va_request_shape_and_progress_prefix(self):
        spy = FfmpegSpy()
        spy.patch(self)
        scene_ctx_project(self, aspect="1:1", quality="standard")
        job = self.job("mv.scene", scene=2)
        self.h3.job = job
        await mvh.run_scene(job, self.ctx())
        # 切り出し: そのシーンのボーカルを 16kHz モノラルで、最短 5 秒に延ばして
        (cut,) = [c for c in self.audio.calls if c[0] == "cut"]
        self.assertEqual(cut[1:], (self.work() / "vocals.wav", 8.0, 14.0, self.work() / "scene_002_vocals.wav", 16000, 1, 5.0))
        (post,) = self.h3.posts
        fields = {n: b.decode() for n, f, ct, b in post if not f}
        self.assertEqual(fields, {"prompt": "prompt 2", "width": "768", "height": "768", "seed": "1002", "vocal_lock": "true"})         # seconds は送らない
        files = [(n, f, ct, b) for n, f, ct, b in post if f]
        self.assertEqual([(n, f) for n, f, ct, b in files], [("references", "girl.png"), ("references", "scene_002_vocals.wav")])    # 画像、ボーカルの順
        self.assertEqual(files[0][3], (self.root / "uploads" / "girl.png").read_bytes())
        self.assertEqual(files[1][3], b"RIFFscene")
        self.assertTrue(files[0][2].startswith("image/") and files[1][2].startswith("audio/"))
        progress, message, remote = self.h3.seen[0]                      # 生成中の文言は「シーン 2 / 3: 」で始まる
        self.assertEqual((message, remote), ("シーン 2 / 3: 生成中 2/4", "remote1"))
        self.assertAlmostEqual(progress, 0.5)
        self.assertEqual(self.h3.downloads, ["/outputs/out_1.mp4"])

    async def test_clip_is_finished_to_the_exact_frame_count(self):
        spy = FfmpegSpy()
        spy.patch(self)
        scene_ctx_project(self, aspect="9:16", quality="fast")
        job = self.job("mv.scene", scene=3)
        await mvh.run_scene(job, self.ctx())
        (argv, dst, what, _), = spy.calls
        self.assertEqual(dst, self.work() / "scene_003.mp4")
        self.assertIn("シーン 3 / 3", what)
        frames = self.proj()["scenes"][2]["frame_count"]                      # 7.0 秒 = 168 コマ
        self.assertEqual(frames, 168)
        self.assertEqual(argv[argv.index("-frames:v") + 1], "168")
        vf = argv[argv.index("-vf") + 1]
        self.assertTrue(vf.startswith("scale=448:768:flags=lanczos,tpad=stop_mode=clone:stop_duration="), vf)
        pad = float(re.search(r"stop_duration=([\d.]+)", vf).group(1))
        self.assertAlmostEqual(pad, max(0.0, 168 / 24 - 141 / 24) + 0.25, places=3)          # H3 の返した長さ(141 コマ)に足りない分
        self.assertIn("-an", argv)
        self.assertEqual(argv[-1], str(mv_audio._part(dst)))
        self.assertFalse((self.work() / "scene_003_raw.mp4").exists())       # 生の動画は消す
        s = self.proj()["scenes"][2]
        self.assertEqual((s["status"], s["clip"], s["thumb"], s["elapsed_s"], s["peak_vram_gb"], s["seed"], s["error"]),
                         ("done", "scene_003.mp4", None, 42.5, 11.0, 99, None))
        self.assertEqual(job.result, {"scene": 3, "generation_seconds": 42.5, "peak_vram_gb": 11.0, "steps_used": 4, "seed": 99})
        self.assertEqual([x["status"] for x in self.proj()["scenes"]], ["pending", "pending", "done"])        # 他のシーンには触れない

    async def test_thumbnail_is_made_when_possible(self):
        FfmpegSpy().patch(self)
        scene_ctx_project(self)
        self.thumb.stop()
        with mock.patch.object(media, "video_thumb", side_effect=lambda src, dst, width: (Path(dst).write_bytes(b"jpg"), True)[1]) as vt:
            await mvh.run_scene(self.job("mv.scene", scene=1), self.ctx())
        self.assertEqual(vt.call_args.args[0], self.work() / "scene_001.mp4")
        self.assertEqual(self.proj()["scenes"][0]["thumb"], "scene_001.jpg")
        self.thumb.start()

    async def test_unknown_h3_length_pads_generously(self):
        spy = FfmpegSpy()
        spy.patch(self)
        scene_ctx_project(self)
        self.h3.result.pop("duration_s")
        await mvh.run_scene(self.job("mv.scene", scene=1), self.ctx())
        vf = spy.calls[0][0][spy.calls[0][0].index("-vf") + 1]
        self.assertIn("stop_duration=1.000", vf)

    async def test_without_a_seed_h3_chooses_and_the_result_is_recorded(self):
        FfmpegSpy().patch(self)
        scene_ctx_project(self)
        self.ledger.update_mv_project("proj1", scene_patches={1: {"seed": None}})
        await mvh.run_scene(self.job("mv.scene", scene=1), self.ctx())
        fields = {n for n, f, ct, b in self.h3.posts[0] if not f}
        self.assertNotIn("seed", fields)
        self.assertEqual(self.proj()["scenes"][0]["seed"], 99)

    async def test_failures(self):
        FfmpegSpy().patch(self)
        scene_ctx_project(self)
        self.h3.status, self.h3.detail = 400, "seconds must be between 5.0 and 15.0"
        with self.assertRaises(JobFailed) as cm:
            await mvh.run_scene(self.job("mv.scene", scene=1), self.ctx())
        self.assertIn("seconds must be between", cm.exception.message)
        self.h3.status = 409
        with self.assertRaises(JobFailed) as cm:
            await mvh.run_scene(self.job("mv.scene", scene=1), self.ctx())
        self.assertIn("別の生成", cm.exception.message)
        self.h3.status = 200
        self.h3.result.pop("video_url")
        with self.assertRaises(JobFailed) as cm:
            await mvh.run_scene(self.job("mv.scene", scene=1), self.ctx())
        self.assertIn("動画を返しませんでした", cm.exception.message)
        self.assertEqual([s["status"] for s in self.proj()["scenes"]], ["pending"] * 3)          # 記録は Director の仕事。ここでは done にしない

    async def test_missing_inputs_fail_before_calling_h3(self):
        FfmpegSpy().patch(self)
        scene_ctx_project(self)
        (self.work() / "vocals.wav").unlink()
        with self.assertRaises(JobFailed) as cm:
            await mvh.run_scene(self.job("mv.scene", scene=1), self.ctx())
        self.assertIn("分離したボーカルが見つかりません", cm.exception.message)
        (self.work() / "vocals.wav").write_bytes(b"x")
        self.ledger.update_mv_project("proj1", scene_patches={2: {"prompt": ""}})
        with self.assertRaises(JobFailed) as cm:
            await mvh.run_scene(self.job("mv.scene", scene=2), self.ctx())
        self.assertIn("演出がまだありません", cm.exception.message)
        self.ledger.delete_output(self.image["id"])
        with self.assertRaises(JobFailed) as cm:
            await mvh.run_scene(self.job("mv.scene", scene=1), self.ctx())
        self.assertIn("キャラクター画像が見つかりません", cm.exception.message)
        self.audio.error["cut"] = mv_audio.MvAudioError("音声の切り出しに失敗しました(ffmpeg): x", "tail")
        with self.assertRaises(JobFailed) as cm:
            await mvh.run_scene(self.job("mv.scene", scene=3), self.ctx())
        self.assertEqual(cm.exception.log_tail, "tail")
        self.assertEqual(self.h3.posts, [])

    async def test_ffmpeg_failure_is_a_job_failure_and_leaves_no_raw_file(self):
        scene_ctx_project(self)
        mp = mock.patch.object(mv_audio, "_ffmpeg", lambda: "ffmpeg")
        mp.start()
        self.addCleanup(mp.stop)
        err = mv_audio.MvAudioError("シーン 1 / 3 のクリップの整形に失敗しました(ffmpeg): Invalid data", "line1\nInvalid data")
        with mock.patch.object(mv_audio, "_run_ffmpeg", side_effect=err):
            with self.assertRaises(JobFailed) as cm:
                await mvh.run_scene(self.job("mv.scene", scene=1), self.ctx())
        self.assertEqual((cm.exception.message, cm.exception.log_tail), (err.message, "line1\nInvalid data"))
        self.assertFalse((self.work() / "scene_001_raw.mp4").exists())

    async def test_cancel_before_start_and_after_h3_returned(self):
        FfmpegSpy().patch(self)
        scene_ctx_project(self)
        job = self.job("mv.scene", scene=1)
        job.cancel_requested = True
        with self.assertRaises(JobCancelled):
            await mvh.run_scene(job, self.ctx())
        self.assertEqual((self.h3.posts, self.audio.calls), ([], []))
        # H3 が返したあとの中止要求では、できた動画を捨てない(クリップにして残す)
        job = self.job("mv.scene", scene=2)

        async def cancel_on_post(req):
            if req.method == "POST" and req.url.path == "/api/ref2va":
                job.cancel_requested = True
            return await self.h3(req)
        await mvh.run_scene(job, Ctx(FakeSched(), self.ledger, self.root, transport=httpx.MockTransport(cancel_on_post)))
        self.assertEqual(self.proj()["scenes"][1]["status"], "done")

    async def test_cancel_sends_interrupt_to_h3(self):
        FfmpegSpy().patch(self)
        scene_ctx_project(self)
        self.h3 = FakeH3(wait_interrupt=True)
        original = self.h3.__call__

        async def interrupted(req):
            r = await original(req)
            if req.method == "POST" and req.url.path == "/api/ref2va":
                return httpx.Response(499, json={"detail": "生成が中断されました: user"})
            return r
        runner = JobRunner(Ctx(FakeSched(), self.ledger, self.root, transport=httpx.MockTransport(interrupted)), {"mv.scene": mvh.HANDLERS["mv.scene"]},
                           step_poll_s=0.01)
        runner.start()
        self.addAsyncCleanup(runner.stop)
        job = runner.submit("mv", "mv.scene", {"project_id": "proj1", "scene": 1})
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
        self.assertEqual(self.h3.interrupts[0], {"job_id": "remote1"})
        self.assertEqual(self.proj()["scenes"][0]["status"], "pending")


class AssembleTest(HandlerBase):
    def finished_project(self, **kw) -> str:
        scenes = self.scenes()
        for s in scenes:
            s.update(status="done", clip=mv.scene_clip_name(s["index"]))
        pid = self.project(state="assembling", scenes=scenes, **kw)
        self.work().mkdir(parents=True, exist_ok=True)
        for s in scenes:
            (self.work() / s["clip"]).write_bytes(b"clip")
        (self.work() / "song.wav").write_bytes(b"RIFFsong")
        return pid

    async def test_concat_then_mux_the_original_song(self):
        spy = FfmpegSpy()
        spy.patch(self)
        pid = self.finished_project(aspect="4:5", quality="high")
        job = self.job("mv.assemble")
        await mvh.run_assemble(job, self.ctx())
        (concat, mux) = spy.calls
        work = self.work()
        self.assertEqual(concat[3], "".join(f"file '{(work / f'scene_{i:03d}.mp4').as_posix()}'\n" for i in (1, 2, 3)))     # 番号順
        self.assertEqual(concat[0], mvclip.concat_args("ffmpeg", work / "concat.txt", mv_audio._part(work / "video_only.mp4")))
        self.assertIn("-c", concat[0])
        final = self.root / "video" / f"mv_{pid}.mp4"
        self.assertEqual(mux[0], mvclip.mux_args("ffmpeg", work / "video_only.mp4", work / "song.wav", mv_audio._part(final), 21.0))
        self.assertEqual(mux[0][mux[0].index("-t") + 1], "21.000")                                             # 曲の長さまで
        self.assertEqual(mux[1], final)
        self.assertTrue(final.is_file())
        self.assertFalse((work / "concat.txt").exists() or (work / "video_only.mp4").exists())                  # 作業ファイルは消す
        rec = self.ledger.get_output(self.proj()["output_id"])
        self.assertEqual((rec["kind"], rec["tab"], rec["path"], rec["job_id"], rec["filename"]), ("video", "mv", f"video/mv_{pid}.mp4", job.id, "夜明けの駅_mv.mp4"))
        self.assertEqual((rec["width"], rec["height"], rec["duration_s"]), (768, 960, 21.0))
        self.assertEqual(rec["params"], {"project_id": pid, "kind": "mv.assemble", "aspect": "4:5", "quality": "high", "scenes": 3,
                                         "song_id": self.song["id"], "image_id": self.image["id"], "width": 768, "height": 960})
        self.assertEqual(job.outputs, [rec["id"]])
        self.assertTrue(self.proj()["params"]["assembled"])

    async def test_reassembling_does_not_overwrite_the_previous_mv(self):
        FfmpegSpy().patch(self)
        pid = self.finished_project()
        await mvh.run_assemble(self.job("mv.assemble"), self.ctx())
        first = self.ledger.get_output(self.proj()["output_id"])
        await mvh.run_assemble(self.job("mv.assemble"), self.ctx())
        second = self.ledger.get_output(self.proj()["output_id"])
        self.assertEqual((first["path"], second["path"]), (f"video/mv_{pid}.mp4", f"video/mv_{pid}_2.mp4"))
        self.assertTrue(self.ledger.abs_path(first["path"]).is_file() and self.ledger.abs_path(second["path"]).is_file())

    async def test_falls_back_to_the_original_song_file(self):
        spy = FfmpegSpy()
        spy.patch(self)
        self.finished_project()
        (self.work() / "song.wav").unlink()
        await mvh.run_assemble(self.job("mv.assemble"), self.ctx())
        mux = spy.calls[1][0]
        self.assertEqual(mux[mux.index("-i", mux.index("-i") + 1) + 1], str(self.ledger.abs_path(self.song["path"])))      # 2 つめの -i = 曲
        self.ledger.delete_output(self.song["id"])
        with self.assertRaises(JobFailed) as cm:
            await mvh.run_assemble(self.job("mv.assemble"), self.ctx())
        self.assertIn("曲のファイルが見つかりません", cm.exception.message)

    async def test_refuses_unfinished_scenes_and_missing_clips(self):
        FfmpegSpy().patch(self)
        self.finished_project()
        self.ledger.update_mv_project("proj1", scene_patches={2: {"status": "failed"}})
        with self.assertRaises(JobFailed) as cm:
            await mvh.run_assemble(self.job("mv.assemble"), self.ctx())
        self.assertIn("まだ生成できていないシーンがあります", cm.exception.message)
        self.ledger.update_mv_project("proj1", scene_patches={2: {"status": "done"}})
        (self.work() / "scene_002.mp4").unlink()
        with self.assertRaises(JobFailed) as cm:
            await mvh.run_assemble(self.job("mv.assemble"), self.ctx())
        self.assertIn("scene_002.mp4", cm.exception.message)
        self.assertEqual(self.ledger.list_outputs(tab="mv"), [])

    async def test_ffmpeg_failure_cleans_up_and_registers_nothing(self):
        self.finished_project()
        mp = mock.patch.object(mv_audio, "_ffmpeg", lambda: "ffmpeg")
        mp.start()
        self.addCleanup(mp.stop)
        err = mv_audio.MvAudioError("シーンの結合に失敗しました(ffmpeg): x", "tail")
        with mock.patch.object(mv_audio, "_run_ffmpeg", side_effect=err):
            with self.assertRaises(JobFailed) as cm:
                await mvh.run_assemble(self.job("mv.assemble"), self.ctx())
        self.assertEqual(cm.exception.log_tail, "tail")
        self.assertFalse((self.work() / "concat.txt").exists())
        self.assertEqual(self.ledger.list_outputs(tab="mv"), [])
        self.assertIsNone(self.proj()["output_id"])

    async def test_cancel_requested_before_it_starts(self):
        FfmpegSpy().patch(self)
        self.finished_project()
        job = self.job("mv.assemble")
        job.cancel_requested = True
        with self.assertRaises(JobCancelled):
            await mvh.run_assemble(job, self.ctx())


# ---- 本物の ffmpeg で(小さい動画) ----

def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *map(str, args)], check=True)


def frames_of(path) -> int:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames", "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path)],
                       capture_output=True, check=True)
    return int(r.stdout.decode().strip().split(",")[0])


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg / ffprobe が PATH に無い")
class EndToEndTest(HandlerBase):
    """実物の Director・JobRunner・4 つのハンドラ・ffmpeg を、偽の道具用 Python・LLM・H3 の上で通しで動かす。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        raw = Path(cls._tmp.name) / "raw.mp4"       # H3 の出力に似せた、24fps・141 コマ・音声つき
        ffmpeg("-f", "lavfi", "-i", "testsrc=size=96x64:rate=24", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000", "-frames:v", 141,
               "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", raw)
        cls.raw_mp4 = raw.read_bytes()
        cls.song_wav = Path(cls._tmp.name) / "song.wav"
        ffmpeg("-f", "lavfi", "-i", "sine=frequency=330:sample_rate=44100:duration=40", "-ac", 2, cls.song_wav)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        super().setUp()
        self.thumb.stop()                  # サムネイルも本物の ffmpeg で
        self.probe.stop()
        self.audio.plan = FakeAudio(durations=(8.0, 5.0, 7.0)).plan         # 8 秒・5 秒・7 秒(H3 の生の 141 コマ = 5.9 秒に対し、足す・切る・足す)
        real_normalize = mv_audio.normalize_sync

        async def normalize(src, dst):                    # 正規化と切り出しは本物の ffmpeg(分離と分析だけが偽)
            return real_normalize(self.song_wav, dst)
        p = mock.patch.object(mv_audio, "normalize", normalize)
        p.start()
        self.addCleanup(p.stop)
        real_cut = mv_audio.cut_sync

        async def cut(src, start, end, dst, *, rate=0, channels=0, pad_to=0.0):
            return real_cut(src, start, end, dst, rate, channels, pad_to)
        q = mock.patch.object(mv_audio, "cut", cut)
        q.start()
        self.addCleanup(q.stop)
        self.h3 = FakeH3(video=self.raw_mp4)
        # 曲の分離の結果は、44.1kHz の曲そのもの(vocals.wav として使う)
        original = self.audio.separate

        async def separate(wav, out_dir, device="cpu", on_progress=None, **kw):
            await original(wav, out_dir, device, on_progress)
            shutil.copy(wav, Path(out_dir) / "vocals.wav")
            return {"vocals": Path(out_dir) / "vocals.wav", "accompaniment": Path(out_dir) / "accompaniment.wav", "seconds": 1.0, "device": device}
        r = mock.patch.object(mv_audio, "separate", separate)
        r.start()
        self.addCleanup(r.stop)

    async def run_all(self):
        from studio.handlers import HANDLERS
        runner = JobRunner(self.ctx(), HANDLERS, step_poll_s=0.01)
        runner.start()
        self.addAsyncCleanup(runner.stop)
        director = mv.Director(runner, self.ledger, self.root)
        return runner, director

    async def until_state(self, director, pid, *states, timeout=60.0):
        for _ in range(int(timeout / 0.02)):
            v = director.view(pid)
            if v["state"] in states:
                return v
            await asyncio.sleep(0.02)
        self.fail(f"状態が {states} にならない: {director.view(pid)['state']} {director.view(pid)['message']} {director.view(pid)['error']}")

    async def test_whole_pipeline_makes_an_mv_of_the_exact_length(self):
        runner, director = await self.run_all()
        pid = director.create({"song_id": self.song["id"], "image_id": self.image["id"], "aspect": "16:9", "quality": "fast", "seed": 7})["id"]
        view = await self.until_state(director, pid, "completed", "failed")
        self.assertEqual((view["state"], view["error"]), ("completed", None))
        # 8 / 5 / 7 秒 = 192 + 120 + 168 コマ。クリップも最終の動画も、その枚数ちょうど(足りないクリップは複製で足し、長いクリップは切る)
        scenes = self.ledger.get_mv_project(pid)["scenes"]
        self.assertEqual([s["frame_count"] for s in scenes], [192, 120, 168])
        for s in scenes:
            self.assertEqual(frames_of(self.work(pid) / s["clip"]), s["frame_count"])
            self.assertEqual(s["seed"], 99)                                    # H3 が返した seed
            self.assertTrue((self.work(pid) / s["thumb"]).is_file())
        out = self.ledger.get_output(view["output"]["id"])
        self.assertEqual(frames_of(self.ledger.abs_path(out["path"])), 480)
        self.assertEqual((out["width"], out["height"], out["tab"], out["kind"]), (768, 448, "mv", "video"))
        self.assertTrue(out["thumb_path"] and self.ledger.abs_path(out["thumb_path"]).is_file())
        self.assertEqual(len(self.h3.posts), 3)
        form = lambda post: {n: b.decode() for n, f, ct, b in post if not f}
        self.assertEqual([form(p)["prompt"] for p in self.h3.posts], [s["prompt"] for s in scenes])
        self.assertIn("subject_definitions:", scenes[0]["prompt"])
        names = sorted(p.name for p in self.work(pid).iterdir())        # 作業フォルダに、中間ファイルを残さない
        self.assertFalse([n for n in names if n in ("concat.txt", "video_only.mp4") or n.endswith("_raw.mp4") or n.endswith(".part")], names)
        # シーンのやり直し(演出を書き換えて)→ 自動で結合し直し、新しい MV が増える
        director.retry_scene(pid, 2, prompt="a brand new direction", seed=555)
        view2 = await self.until_state(director, pid, "completed", "failed")
        self.assertEqual(view2["state"], "completed")
        self.assertEqual(len(self.h3.posts), 4)
        self.assertEqual((form(self.h3.posts[3])["prompt"], form(self.h3.posts[3])["seed"]), ("a brand new direction", "555"))
        self.assertNotEqual(view2["output"]["id"], view["output"]["id"])
        self.assertEqual(frames_of(self.ledger.abs_path(view2["output"]["path"])), 480)
        self.assertTrue(self.ledger.abs_path(view["output"]["path"]).is_file())          # 前の MV も残る
        self.assertEqual(len(self.ledger.list_outputs(kind="video", tab="mv")), 2)
        await director.delete(pid)
        self.assertFalse(self.work(pid).exists())
        self.assertTrue(self.ledger.abs_path(view2["output"]["path"]).is_file())          # 作業フォルダを消しても、最終の MV は残る


if __name__ == "__main__":
    unittest.main()
