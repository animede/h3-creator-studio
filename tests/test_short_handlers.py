import hashlib
import io
import json
import re
import tempfile
import types
import unittest
import wave
from pathlib import Path
from urllib.parse import parse_qsl
from unittest import mock

import httpx
from PIL import Image

from studio import mvsub, media, mv, mv_audio, mvclip, mvscript, short
from studio.handlers import h3
from studio.handlers import mv as mvh
from studio.handlers import short as sh
from studio.jobs import Ctx, Job, JobCancelled, JobFailed
from studio.ledger import Ledger
from studio.llm import LlmError
from studio.scheduler import Operation
from studio.tts import TtsError

VISION_JA = "長い銀髪、青い瞳、青いセーラー服の少女。アニメ調"
VISION_REPLY = json.dumps({"subject": "a young anime-style girl", "face": "blue eyes", "hair": "long silver hair", "outfit": "blue sailor uniform",
                           "style": "anime cel shading", "identity_anchor": "A young anime girl", "description_ja": VISION_JA}, ensure_ascii=False)
OUTLINE = {"title": "朝の駅", "setting": "station", "characters": "a girl", "arc": "wait", "places": ["station"], "continuity_rules": ["same coat"]}


def png_bytes(size=(64, 48)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (10, 120, 200)).save(buf, "PNG")
    return buf.getvalue()


def wav_bytes(seconds: float, rate: int = 24000, channels: int = 1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * channels * round(seconds * rate))
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


class FakeSched:
    def __init__(self):
        self.comps = {"h3": types.SimpleNamespace(spec=types.SimpleNamespace(base_url="http://h3.test"))}

    def new_operation(self, kind, name):
        return Operation(kind, name)

    async def ensure(self, name, op=None):
        op.add("準備完了")


class FakeAudio:
    """mv_audio の normalize / separate / analyze / cut の代わり(道具用の Python も ffmpeg も使わない)。"""

    def __init__(self, durations=(8.0, 6.0, 7.0)):
        t, scenes = 0.0, []
        for i, d in enumerate(durations, start=1):
            scenes.append({"index": i, "start": t, "end": t + d, "duration": d, "vocal_ratio": 0.8 if i % 2 else 0.0,
                           "kind": "vocal" if i % 2 else "instrumental", "boundary_kind": "bar"})
            t += d
        self.plan = {"duration": sum(durations), "bpm": 118.5, "has_vocals": True, "scenes": scenes}
        self.calls, self.error = [], {}

    def _fail(self, name):
        if name in self.error:
            raise self.error[name]

    async def normalize(self, src, dst):
        self.calls.append(("normalize", Path(src), Path(dst)))
        self._fail("normalize")
        Path(dst).write_bytes(b"RIFFnormalized:" + Path(src).read_bytes()[-8:])
        return Path(dst)

    async def separate(self, wav, out_dir, device="cpu", on_progress=None, **kw):
        self.calls.append(("separate", Path(wav), Path(out_dir), device))
        self._fail("separate")
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


class FakeTts:
    """Tts.synthesize_wav の代わり: 文ごとに、決めた秒数(既定は 1 文字 0.5 秒)の無音の WAV を返す。"""

    def __init__(self, seconds=None, rate=24000):
        self.seconds, self.rate, self.calls, self.error, self.job, self.snaps = seconds or {}, rate, [], None, None, []

    async def synthesize_wav(self, text, style_id, speed=1.0):
        self.calls.append((text, style_id, speed))
        if self.job is not None:
            self.snaps.append((self.job.progress, self.job.message))
        if self.error is not None:
            raise self.error
        rate = self.rate(text) if callable(self.rate) else self.rate
        return wav_bytes(self.seconds.get(text, len(text) * 0.5), rate)


class FakeH3:
    """H3 の /api/t2va・/api/ref2va の代わり(httpx.MockTransport)。受けたフォームを posts に残す。"""

    def __init__(self, video=b"fakevideo", **result):
        self.result = {"job_id": "remote1", "video_url": "/outputs/out_1.mp4", "duration_s": 141 / 24, "total_elapsed_s": 42.5,
                       "num_inference_steps": 4, "seed": 99, "peak_vram_gb": 11.0, **result}
        self.video, self.posts, self.downloads = video, [], []

    async def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/api/progress":
            return httpx.Response(200, json={"job_id": None, "phase": "idle"})
        if req.method == "POST" and path in ("/api/t2va", "/api/ref2va"):
            if "multipart" in req.headers.get("content-type", ""):
                form = parse_form(req)
            else:       # ファイルが無いと、httpx は urlencoded で送る(t2va)
                form = [(k, None, None, v.encode()) for k, v in parse_qsl(req.content.decode())]
            self.posts.append((path, form))
            return httpx.Response(200, json=self.result)
        if req.method == "GET" and path.startswith("/outputs/"):
            self.downloads.append(path)
            return httpx.Response(200, content=self.video)
        return httpx.Response(404, json={"detail": f"想定外: {req.method} {path}"})


class FfmpegSpy:
    """mv_audio._run_ffmpeg の代わり: 引数を残し、出力先(.part を外した dst)に中身を書く。"""

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


class SmartChat:
    """管理役の LLM の chat の代わり(人物の見た目の抜き出しだけに答える)。"""

    def __init__(self):
        self.calls = []

    async def __call__(self, system, user, max_tokens=0, temperature=0):
        self.calls.append(user)
        if "ONE compact Japanese line" in user:
            return "長い銀髪の少女。アニメ調"
        if "fixed visual appearance" in user:
            return json.dumps({"subject": user.rsplit("DESCRIPTION: ", 1)[-1].strip(), "face": "", "hair": "", "outfit": "", "style": ""})
        raise AssertionError("想定外の依頼: " + user[:80])


class FakeScript:
    """shortscript の story_outline / plan_scenes / generate_scenario の代わり(受けた引数を残し、決めた結果を返す)。"""

    def __init__(self):
        self.calls = {"story_outline": [], "plan_scenes": [], "generate_scenario": []}
        self.outline, self.plan, self.error = dict(OUTLINE), None, {}
        self.refs = {}                 # シーン番号 -> refs(無ければ []、speech は [0])

    async def story_outline(self, chat, story, **kw):
        self.calls["story_outline"].append((chat, story, kw))
        if "story_outline" in self.error:
            raise self.error["story_outline"]
        return self.outline

    async def plan_scenes(self, chat, outline, total_s, min_s, max_s):
        self.calls["plan_scenes"].append((chat, outline, total_s, min_s, max_s))
        if "plan_scenes" in self.error:
            raise self.error["plan_scenes"]
        if self.plan is not None:
            return self.plan
        n = round(total_s / 7.5)
        return [{"duration": total_s / n, "place": f"place {i}", "summary": f"summary {i}"} for i in range(1, n + 1)]

    async def generate_scenario(self, chat, scenes, outline, character, sceneries, **kw):
        self.calls["generate_scenario"].append((chat, scenes, outline, character, sceneries, kw))
        if "generate_scenario" in self.error:
            raise self.error["generate_scenario"]
        if kw.get("on_progress"):
            kw["on_progress"](len(scenes), len(scenes))
        return {"overview": outline, "character": character, "scenes": [
            {"index": s["index"], "emotion": f"emo{s['index']}", "place": f"loc{s['index']}", "refs": self.refs.get(s["index"], [0] if kw["timeline"] == "speech" else []),
             "prompt": f"prompt {s['index']}"} for s in scenes]}

    def patch(self, test):
        for name in self.calls:
            p = mock.patch.object(sh.shortscript, name, getattr(self, name))
            p.start()
            test.addCleanup(p.stop)


class HandlerBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.ledger = Ledger(self.root)
        self.addCleanup(self.ledger.close)
        for p in (mock.patch.object(h3, "POLL_S", 0.01), mock.patch.object(media, "video_thumb", return_value=False),
                  mock.patch.object(media, "probe", return_value={"duration_s": None, "width": None, "height": None})):
            p.start()
            self.addCleanup(p.stop)
        self.audio = FakeAudio()
        self.audio.patch(self)
        self.ff = FfmpegSpy()
        self.ff.patch(self)
        self.script = FakeScript()
        self.script.patch(self)
        self.song = self.add("audio", "song.mp3", {"theme": "夜明けの駅", "genre": "Ballad"})
        self.girl = self.add("image", "girl.png", {"prompt": "a girl with silver hair"})
        self.garden = self.add("image", "garden.png", {"prompt": "a quiet garden"})
        self.blank = self.add("image", "blank.png", {})
        self.chat, self.tts, self.h3 = SmartChat(), FakeTts(), FakeH3()
        self.vision_calls = []

    def add(self, kind, name, params):
        p = self.root / "uploads" / name
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(png_bytes() if kind == "image" else b"RIFFfake")
        return self.ledger.add_output(kind, p, "upload", filename=name, params=params, duration_s=30.0 if kind == "audio" else None)

    def ctx(self, llm="default", tts="default") -> Ctx:
        async def chat_image(system, user, image_bytes, mime="image/png", max_tokens=0, temperature=0):
            self.vision_calls.append(image_bytes)
            return VISION_REPLY
        return Ctx(FakeSched(), self.ledger, self.root, transport=httpx.MockTransport(self.h3),
                   llm=types.SimpleNamespace(chat=self.chat, chat_image=chat_image) if llm == "default" else llm,
                   tts=self.tts if tts == "default" else tts)

    def project(self, timeline="music", state="analyzing", scenes=None, analysis=True, pid="proj1", **params) -> str:
        base = {"music": {"song_id": self.song["id"], "story": "雨の街"}, "free": {"story": "雨の街の物語"},
                "speech": {"narration": "朝です。駅に着きました。", "speaker_id": 5, "images": [{"id": self.girl["id"], "role": "person"}]}}[timeline]
        resolved = short.resolve_params(self.ledger, {**base, **params})
        if analysis:
            resolved["analysis"] = {"duration": 21.0, "scenes": 3}
        self.ledger.create_mv_project(pid, state, resolved, scenes if scenes is not None else [], "", kind="short")
        return pid

    def scenes(self, timeline="music", durations=(8.0, 6.0, 7.0), refs=None, prompts=True) -> list:
        t, plan = 0.0, []
        for i, d in enumerate(durations, start=1):
            plan.append({"index": i, "start": t, "end": t + d, "duration": d, "vocal_ratio": 1.0 if timeline == "speech" else 0.0,
                         "kind": "free" if timeline == "free" else "vocal", "boundary_kind": "bar"})
            t += d
        scenes = short.make_scenes(mvclip.quantize_scene_plan(plan), timeline, 0 if timeline == "speech" else None)
        for s in scenes:
            if prompts:
                s["prompt"], s["seed"] = f"prompt {s['index']}", 1000 + s["index"]
            if refs is not None:
                s["refs"] = list((refs or {}).get(s["index"], []))
        return scenes

    def job(self, kind, pid="proj1", **extra) -> Job:
        return Job(id="job123456789", tab="short", kind=kind, params=sh.normalize(kind, {"project_id": pid, **extra}, self.ledger))

    def work(self, pid="proj1") -> Path:
        return short.work_dir(self.root, pid)

    def proj(self, pid="proj1") -> dict:
        return self.ledger.get_mv_project(pid)

    def form(self, post) -> dict:
        """(パス, フォーム)のフォームの値の辞書(ファイルは除く)。"""
        return {n: body.decode() for n, fn, ct, body in post[1] if fn is None}

    def files(self, post) -> list:
        return [(n, fn, body) for n, fn, ct, body in post[1] if fn is not None]


class NormalizeTest(HandlerBase):
    def test_validates_project_scene_and_kind(self):
        pid = self.project(scenes=self.scenes())
        self.assertEqual(sh.normalize("short.analyze", {"project_id": pid, "junk": 1}, self.ledger), {"project_id": pid})
        self.assertEqual(sh.normalize("short.scene", {"project_id": pid, "scene": "2"}, self.ledger), {"project_id": pid, "scene": 2})
        for kind, params, word in (("short.analyze", {}, "ショート動画の制作が見つかりません"), ("short.plan", {"project_id": "nope"}, "ショート動画の制作が見つかりません"),
                                   ("short.scene", {"project_id": pid}, "シーンの番号"), ("short.scene", {"project_id": pid, "scene": 9}, "シーン 9 はありません")):
            with self.assertRaises(ValueError, msg=str(params)) as cm:
                sh.normalize(kind, params, self.ledger)
            self.assertIn(word, str(cm.exception))

    def test_a_job_kind_must_match_the_project_kind(self):
        self.ledger.create_mv_project("m1", "analyzing", {"song_id": self.song["id"]}, [], "")             # kind = mv
        with self.assertRaises(ValueError) as cm:
            sh.normalize("short.analyze", {"project_id": "m1"}, self.ledger)
        self.assertIn("ショート動画の制作が見つかりません", str(cm.exception))
        pid = self.project()
        with self.assertRaises(ValueError) as cm:
            mvh.normalize("mv.analyze", {"project_id": pid}, self.ledger)
        self.assertIn("MV の制作が見つかりません", str(cm.exception))
        self.assertEqual(mvh.normalize("mv.analyze", {"project_id": "m1"}, self.ledger), {"project_id": "m1"})

    def test_registered_with_the_declared_lanes(self):
        from studio.handlers import HANDLERS
        for kind, component, gpu in (("short.analyze", None, False), ("short.plan", None, False), ("short.script", None, False), ("short.scene", "h3", True),
                                     ("short.assemble", None, False)):
            self.assertEqual((HANDLERS[kind].component, HANDLERS[kind].gpu), (component, gpu), kind)
            self.assertIs(HANDLERS[kind], sh.HANDLERS[kind])
        self.assertIs(HANDLERS["short.scene"].cancel, h3.cancel)
        self.assertIn("mv.scene", HANDLERS)


class AnalyzeSpeechTest(HandlerBase):
    async def test_synthesizes_sentence_by_sentence_and_places_the_narration(self):
        self.project("speech", analysis=False)
        job = self.job("short.analyze")
        self.tts.job = job
        await sh.run_analyze(job, self.ctx())
        self.assertEqual(self.tts.calls, [("朝です。", 5, 1.0), ("駅に着きました。", 5, 1.0)])           # 文ごと、話者を渡して
        self.assertEqual([round(job_p, 2) for job_p, _ in self.tts.snaps], [0.0, 0.45])
        self.assertEqual(self.tts.snaps[1][1], "読み上げを合成しています 2 / 2 文")
        work = self.work()
        raw, narration = work / "narration_raw.wav", work / "narration.wav"
        self.assertEqual(self.audio.calls[0], ("normalize", raw, narration))                              # 44.1kHz にそろえる(合成の周波数が何でも)
        self.assertFalse(raw.exists())
        data = narration.read_bytes()
        self.assertEqual((work / "song.wav").read_bytes(), data)                                          # song.wav と vocals.wav は、読み上げそのもの
        self.assertEqual((work / "vocals.wav").read_bytes(), data)
        self.assertEqual([c[0] for c in self.audio.calls], ["normalize"])                                 # Demucs も分析の道具も使わない
        info = json.loads((work / "sentences.json").read_text("utf-8"))
        self.assertEqual(info["duration"], 6.0)
        self.assertEqual(info["sentences"], [{"index": 1, "text": "朝です。", "start": 0.0, "end": 2.0, "duration": 2.0},
                                             {"index": 2, "text": "駅に着きました。", "start": 2.0, "end": 6.0, "duration": 4.0}])

    async def test_scenes_are_built_on_sentence_boundaries_with_speech_refs(self):
        text = "".join(f"文{i}です。" for i in range(1, 9))                  # 5 文字 × 0.5 = 2.5 秒の文が 8 つ = 20 秒
        self.project("speech", analysis=False, narration=text, min_s=5, max_s=10)
        job = self.job("short.analyze")
        await sh.run_analyze(job, self.ctx())
        proj = self.proj()
        self.assertEqual(proj["params"]["analysis"], {"duration": 20.0, "sentences": 8, "scenes": 3})
        self.assertEqual(job.result, proj["params"]["analysis"])
        scenes = proj["scenes"]
        self.assertEqual(len(scenes), 3)
        self.assertEqual(sum(s["frame_count"] for s in scenes), 20 * 24)
        for s in scenes:
            self.assertEqual((s["speech"], s["refs"], s["vocal_ratio"], s["kind"], s["status"], s["prompt"]), (True, [0], 1.0, "vocal", "pending", ""))
            self.assertEqual(round(s["end"] * 2) / 2 % 2.5, 0)                         # 切れ目は文の境界(2.5 秒の倍数)
            self.assertTrue(5.0 <= s["duration"] <= 10.0, s["duration"])
        self.assertEqual("".join(s["text"] for s in scenes), text)                      # どの文も 1 つのシーンに入る
        self.assertFalse(proj["params"]["assembled"])

    async def test_a_finished_synthesis_is_reused_but_a_changed_narration_is_not(self):
        self.project("speech", analysis=False)
        await sh.run_analyze(self.job("short.analyze"), self.ctx())
        self.assertEqual(len(self.tts.calls), 2)
        self.audio.calls.clear()
        await sh.run_analyze(self.job("short.analyze"), self.ctx())                                       # 失敗後の再開など: 合成し直さない
        self.assertEqual((len(self.tts.calls), self.audio.calls), (2, []))
        self.ledger.update_mv_project("proj1", params_patch={"narration": "新しい文です。"})
        await sh.run_analyze(self.job("short.analyze"), self.ctx())
        self.assertEqual(len(self.tts.calls), 3)
        self.assertEqual(json.loads((self.work() / "sentences.json").read_text("utf-8"))["sentences"][0]["text"], "新しい文です。")
        self.ledger.update_mv_project("proj1", params_patch={"speaker_id": 9})                              # 話者が変われば合成し直す
        await sh.run_analyze(self.job("short.analyze"), self.ctx())
        self.assertEqual(self.tts.calls[-1][1], 9)

    async def test_failures_are_reported_in_japanese(self):
        self.project("speech", analysis=False)
        self.tts.error = TtsError("AivisSpeech を起動できません: 時間切れ", 503)
        with self.assertRaises(JobFailed) as cm:
            await sh.run_analyze(self.job("short.analyze"), self.ctx())
        self.assertEqual(cm.exception.message, "AivisSpeech を起動できません: 時間切れ")
        self.assertEqual((self.proj()["scenes"], self.proj()["params"].get("analysis")), ([], None))
        self.tts.error = None
        with self.assertRaises(JobFailed) as cm:
            await sh.run_analyze(self.job("short.analyze"), self.ctx(tts=None))
        self.assertIn("音声合成(AivisSpeech)が設定されていません", cm.exception.message)
        self.tts.rate = lambda text: 24000 if text == "朝です。" else 44100                                                  # 文ごとに形式が違う
        with self.assertRaises(JobFailed) as cm:
            await sh.run_analyze(self.job("short.analyze"), self.ctx())
        self.assertIn("形式が一致しません", cm.exception.message)
        self.tts.rate = 24000
        self.audio.error["normalize"] = mv_audio.MvAudioError("音声の変換に失敗しました(ffmpeg): x", "tail")
        with self.assertRaises(JobFailed) as cm:
            await sh.run_analyze(self.job("short.analyze"), self.ctx())
        self.assertEqual((cm.exception.message, cm.exception.log_tail), ("音声の変換に失敗しました(ffmpeg): x", "tail"))
        self.assertFalse((self.work() / "narration_raw.wav").exists())
        self.assertFalse((self.work() / "sentences.json").exists())                                           # 途中の失敗では完了の印を残さない

    async def test_bad_wav_from_the_engine_fails(self):
        self.project("speech", analysis=False)

        async def junk(text, style_id, speed=1.0):
            return b"not a wav"
        self.tts.synthesize_wav = junk
        with self.assertRaises(JobFailed) as cm:
            await sh.run_analyze(self.job("short.analyze"), self.ctx())
        self.assertIn("不正な WAV", cm.exception.message)

    async def test_a_cancel_before_start_does_nothing(self):
        self.project("speech", analysis=False)
        job = self.job("short.analyze")
        job.cancel_requested = True
        with self.assertRaises(JobCancelled):
            await sh.run_analyze(job, self.ctx())
        self.assertEqual(self.tts.calls, [])


class AnalyzeMusicFreeTest(HandlerBase):
    async def test_music_reuses_the_mv_analysis_and_adds_the_short_keys(self):
        self.project("music", analysis=False, min_s=6, max_s=9)
        job = self.job("short.analyze")
        await sh.run_analyze(job, self.ctx())
        work, wav = self.work(), self.work() / "song.wav"
        self.assertEqual([c[0] for c in self.audio.calls], ["normalize", "separate", "analyze"])
        self.assertEqual(self.audio.calls[0][1:], (self.ledger.abs_path(self.song["path"]), wav))
        self.assertEqual(self.audio.calls[1][1:], (wav, work, "cpu"))
        self.assertEqual(self.audio.calls[2][1:], (wav, work / "vocals.wav", 6.0, 9.0, 7.5))
        proj = self.proj()
        self.assertEqual(proj["params"]["analysis"], {"duration": 21.0, "bpm": 118.5, "has_vocals": True, "scenes": 3})
        self.assertEqual(len(proj["scenes"]), 3)
        for s in proj["scenes"]:
            self.assertEqual((s["speech"], s["refs"], s["place"], s["status"], s["prompt"]), (False, [], "", "pending", ""))
        self.assertEqual(sum(s["frame_count"] for s in proj["scenes"]), 21 * 24)
        self.assertFalse(proj["params"]["assembled"])

    async def test_music_failure_messages_come_from_the_shared_analysis(self):
        self.project("music", analysis=False)
        self.audio.plan["scenes"] = []
        with self.assertRaises(JobFailed) as cm:
            await sh.run_analyze(self.job("short.analyze"), self.ctx())
        self.assertIn("シーンに分けられませんでした", cm.exception.message)
        self.ledger.delete_output(self.song["id"])
        with self.assertRaises(JobFailed) as cm:
            await sh.run_analyze(self.job("short.analyze"), self.ctx())
        self.assertIn("曲が見つかりません", cm.exception.message)

    async def test_the_mv_analysis_still_writes_mv_scenes(self):
        self.ledger.create_mv_project("mvp", "analyzing", mv.resolve_params(self.ledger, {"song_id": self.song["id"], "image_id": self.girl["id"]}), [], "")
        await mvh.run_analyze(Job(id="j", tab="mv", kind="mv.analyze", params={"project_id": "mvp"}), self.ctx())
        scenes = self.ledger.get_mv_project("mvp")["scenes"]
        self.assertEqual(len(scenes), 3)
        self.assertNotIn("refs", scenes[0])                                              # MV のシーンには、ショート動画の項目は無い
        self.assertTrue((mv.work_dir(self.root, "mvp") / "song.wav").is_file())

    async def test_free_only_records_the_length(self):
        self.project("free", analysis=False, length_s=90)
        job = self.job("short.analyze")
        await sh.run_analyze(job, self.ctx())
        proj = self.proj()
        self.assertEqual(proj["params"]["analysis"], {"duration": 90.0, "scenes": 0})
        self.assertEqual((proj["scenes"], self.audio.calls, self.tts.calls), ([], [], []))
        self.assertEqual(job.result, {"duration": 90.0, "scenes": 0})
        self.assertEqual(short.next_step(proj), ("plan",))


class PlanTest(HandlerBase):
    async def test_plans_scenes_on_the_frame_grid(self):
        self.project("free", analysis=True, length_s=60, images=[{"id": self.girl["id"], "role": "person"}, {"id": self.garden["id"], "role": "scenery"}], min_s=5, max_s=10)
        self.ledger.update_mv_project("proj1", params_patch={"analysis": {"duration": 60.0, "scenes": 0}})
        self.script.plan = [{"duration": 7.0, "place": "駅", "summary": "待つ"}, {"duration": 9.125, "place": "車内", "summary": "揺れる"},
                            {"duration": 6.0, "place": "海", "summary": "見る"}]
        job = self.job("short.plan")
        ctx = self.ctx()
        await sh.run_plan(job, ctx)
        ((chat, outline, total_s, min_s, max_s),) = self.script.calls["plan_scenes"]
        self.assertIs(chat, ctx.llm.chat)
        self.assertEqual((outline, total_s, min_s, max_s), (OUTLINE, 60.0, 5.0, 10.0))
        proj = self.proj()
        self.assertEqual([(s["index"], s["place"], s["summary"], s["kind"], s["boundary_kind"], s["vocal_ratio"]) for s in proj["scenes"]],
                         [(1, "駅", "待つ", "free", "plan", 0.0), (2, "車内", "揺れる", "free", "plan", 0.0), (3, "海", "見る", "free", "plan", 0.0)])
        self.assertEqual([s["frame_count"] for s in proj["scenes"]], [168, 219, 144])                      # コマの格子に載る(9.125 秒 → 219 コマ)
        self.assertEqual([s["start_frame"] for s in proj["scenes"]], [0, 168, 387])
        for s in proj["scenes"]:
            self.assertEqual((s["speech"], s["refs"], s["status"], s["prompt"]), (False, [], "pending", ""))
        self.assertEqual(proj["params"]["analysis"], {"duration": 531 / 24, "scenes": 3})                  # 実際のシーンの合計に直す
        self.assertEqual(job.result, proj["params"]["analysis"])
        self.assertEqual(proj["params"]["outline"], OUTLINE)
        self.assertFalse(proj["params"]["assembled"])
        self.assertEqual(short.next_step(proj), ("script",))

    async def test_outline_gets_the_person_facts_the_story_and_the_scenery_descriptions(self):
        self.project("free", length_s=45, story="朝の駅", images=[{"id": self.garden["id"], "role": "scenery"}, {"id": self.girl["id"], "role": "person"},
                                                                {"id": self.blank["id"], "role": "scenery", "description": "夕方の街"}])
        await sh.run_plan(self.job("short.plan"), self.ctx())
        ((chat, story, kw),) = self.script.calls["story_outline"]
        self.assertEqual(story, "朝の駅")
        self.assertEqual(kw["character"], {"subject": "a girl with silver hair"})                           # 画像のプロンプトから見た目を抜き出したもの
        self.assertEqual(kw["sceneries"], ["a quiet garden", "夕方の街"])                                    # 景色・物だけ(人物は含めない)
        self.assertEqual((kw["song_concept"], kw["narration"]), ("", ""))
        self.assertEqual(self.proj()["params"]["character_facts"], {"subject": "a girl with silver hair"})

    async def test_character_is_made_from_the_image_when_the_description_is_empty(self):
        self.project("free", images=[{"id": self.blank["id"], "role": "person"}])
        self.assertEqual(self.proj()["params"]["character"], "")
        await sh.run_plan(self.job("short.plan"), self.ctx())
        self.assertEqual(len(self.vision_calls), 1)
        p = self.proj()["params"]
        self.assertEqual((p["character"], p["character_source"]), (VISION_JA, "vision"))
        self.assertEqual(self.script.calls["story_outline"][0][2]["character"], {"subject": VISION_JA})

    async def test_no_person_means_no_character_and_no_vision(self):
        self.project("free", images=[{"id": self.garden["id"], "role": "scenery"}])
        await sh.run_plan(self.job("short.plan"), self.ctx())
        self.assertEqual((self.script.calls["story_outline"][0][2]["character"], self.vision_calls), ({}, []))
        self.assertEqual(self.proj()["params"]["character_facts"], {})

    async def test_an_existing_outline_is_not_made_again(self):
        self.project("free")
        self.ledger.update_mv_project("proj1", params_patch={"outline": {"title": "既にある"}, "character_facts": {}})
        await sh.run_plan(self.job("short.plan"), self.ctx())
        self.assertEqual(self.script.calls["story_outline"], [])
        self.assertEqual(self.script.calls["plan_scenes"][0][1], {"title": "既にある"})

    async def test_an_unreadable_outline_still_goes_on(self):
        self.project("free")
        self.script.outline = {}
        await sh.run_plan(self.job("short.plan"), self.ctx())
        self.assertEqual(self.proj()["params"]["outline"], {})
        self.assertTrue(self.proj()["scenes"])

    async def test_failures(self):
        self.project("free")
        self.script.error["plan_scenes"] = mvscript.ScenarioError("シーン割りの数が合いません: 期待 8 個、LLM の答え 5 個")
        with self.assertRaises(JobFailed) as cm:
            await sh.run_plan(self.job("short.plan"), self.ctx())
        self.assertIn("シーン割りの数が合いません", cm.exception.message)
        self.assertEqual(self.proj()["scenes"], [])
        self.assertEqual(self.proj()["params"]["outline"], OUTLINE)                                          # 筋は残る(再開で作り直さない)
        self.script.error["plan_scenes"] = LlmError("LLM が使えません")
        with self.assertRaises(JobFailed) as cm:
            await sh.run_plan(self.job("short.plan"), self.ctx())
        self.assertEqual(cm.exception.message, "LLM が使えません")
        with self.assertRaises(JobFailed) as cm:
            await sh.run_plan(self.job("short.plan"), self.ctx(llm=None))
        self.assertIn("LLM が設定されていません", cm.exception.message)


class ScriptTest(HandlerBase):
    async def test_speech_script_passes_sentences_roles_and_writes_the_scenes(self):
        self.project("speech", analysis=True, images=[{"id": self.garden["id"], "role": "scenery"}, {"id": self.girl["id"], "role": "person"}],
                     aspect="9:16", background_text="none", seed=7)
        scenes = self.scenes("speech")
        self.ledger.update_mv_project("proj1", scenes=scenes)
        self.work().mkdir(parents=True)
        sentences = [{"index": 1, "text": "朝です。", "start": 0.0, "end": 2.0, "duration": 2.0}]
        (self.work() / "sentences.json").write_text(json.dumps({"digest": "x", "sentences": sentences}), "utf-8")
        job = self.job("short.script")
        ctx = self.ctx()
        await sh.run_script(job, ctx)
        ((chat, got_scenes, outline, character, sceneries, kw),) = self.script.calls["generate_scenario"]
        self.assertIs(chat, ctx.llm.chat)
        self.assertEqual([s["index"] for s in got_scenes], [1, 2, 3])
        self.assertEqual((outline, character), (OUTLINE, {"subject": "a girl with silver hair"}))
        self.assertEqual(sceneries, ["a girl with silver hair", "a quiet garden"])                           # 全画像の説明(人物が先頭、params.images の添字順)
        self.assertEqual((kw["timeline"], kw["frame"], kw["background_text"], kw["roles"]), ("speech", "portrait", "none", ["person", "scenery"]))
        self.assertEqual(kw["narration_sentences"], sentences)
        proj = self.proj()
        self.assertEqual([s["prompt"] for s in proj["scenes"]], ["prompt 1", "prompt 2", "prompt 3"])
        self.assertEqual([s["emotion"] for s in proj["scenes"]], ["emo1", "emo2", "emo3"])
        self.assertEqual([s["place"] for s in proj["scenes"]], ["loc1", "loc2", "loc3"])
        self.assertEqual([s["refs"] for s in proj["scenes"]], [[0]] * 3)
        self.assertEqual([s["seed"] for s in proj["scenes"]], [mvclip.scene_seed(7, i) for i in (1, 2, 3)])
        self.assertEqual(proj["params"]["script"], OUTLINE)
        self.assertEqual((job.progress, job.message), (1.0, "演出を書いています 3 / 3 シーン"))
        self.assertEqual(short.next_step(proj), ("scene", 1))

    async def test_music_and_free_scripts_get_no_sentences_and_keep_the_llm_refs(self):
        for timeline, pid in (("music", "m"), ("free", "f")):
            self.project(timeline, pid=pid, images=[{"id": self.girl["id"], "role": "person"}, {"id": self.garden["id"], "role": "scenery"}])
            self.ledger.update_mv_project(pid, scenes=self.scenes(timeline))
        self.script.refs = {1: [0, 1], 2: [1], 3: []}
        await sh.run_script(self.job("short.script", "m"), self.ctx())
        await sh.run_script(self.job("short.script", "f"), self.ctx())
        kws = [c[5] for c in self.script.calls["generate_scenario"]]
        self.assertEqual([(k["timeline"], k["narration_sentences"]) for k in kws], [("music", None), ("free", None)])
        for pid in "mf":
            self.assertEqual([s["refs"] for s in self.proj(pid)["scenes"]], [[0, 1], [1], []])               # 景色だけ・参照なし(t2va)も、そのまま

    async def test_out_of_range_refs_are_dropped(self):
        self.project("music", images=[{"id": self.girl["id"], "role": "person"}])
        self.ledger.update_mv_project("proj1", scenes=self.scenes())
        self.script.refs = {1: [0, 5, -1, "x", True], 2: [3]}
        await sh.run_script(self.job("short.script"), self.ctx())
        self.assertEqual([s["refs"] for s in self.proj()["scenes"]], [[0], [], []])

    async def test_no_person_script_uses_empty_character(self):
        self.project("free", images=[{"id": self.garden["id"], "role": "scenery"}])
        self.ledger.update_mv_project("proj1", scenes=self.scenes("free"))
        await sh.run_script(self.job("short.script"), self.ctx())
        ((_, _, _, character, sceneries, kw),) = self.script.calls["generate_scenario"]
        self.assertEqual((character, sceneries, kw["roles"]), ({}, ["a quiet garden"], ["scenery"]))

    async def test_default_seed_is_stable_per_project(self):
        self.project("music")
        self.ledger.update_mv_project("proj1", scenes=self.scenes())
        await sh.run_script(self.job("short.script"), self.ctx())
        base = int(hashlib.sha256(b"proj1").hexdigest()[:8], 16)
        self.assertEqual([s["seed"] for s in self.proj()["scenes"]], [mvclip.scene_seed(base, i) for i in (1, 2, 3)])

    async def test_music_outline_gets_the_song_concept(self):
        self.project("music")
        self.ledger.update_mv_project("proj1", scenes=self.scenes())
        await sh.run_script(self.job("short.script"), self.ctx())
        self.assertIn("夜明けの駅", self.script.calls["story_outline"][0][2]["song_concept"])

    async def test_speech_outline_gets_the_narration(self):
        self.project("speech")
        self.ledger.update_mv_project("proj1", scenes=self.scenes("speech"))
        await sh.run_script(self.job("short.script"), self.ctx())
        self.assertEqual(self.script.calls["story_outline"][0][2]["narration"], "朝です。駅に着きました。")

    async def test_outline_made_by_plan_is_reused(self):
        self.project("free")
        self.ledger.update_mv_project("proj1", scenes=self.scenes("free"), params_patch={"outline": {"title": "plan で作った"}, "character_facts": {}})
        await sh.run_script(self.job("short.script"), self.ctx())
        self.assertEqual(self.script.calls["story_outline"], [])
        self.assertEqual(self.script.calls["generate_scenario"][0][2], {"title": "plan で作った"})

    async def test_failures(self):
        self.project("speech")
        self.ledger.update_mv_project("proj1", scenes=self.scenes("speech", prompts=False))
        self.script.error["generate_scenario"] = mvscript.ScenarioError("セリフのある動画には、人物の画像が必要です")
        with self.assertRaises(JobFailed) as cm:
            await sh.run_script(self.job("short.script"), self.ctx())
        self.assertIn("人物の画像が必要です", cm.exception.message)
        self.assertTrue(all(not s["prompt"] for s in self.proj()["scenes"]))
        self.script.error["generate_scenario"] = LlmError("LLM が止まっています")
        with self.assertRaises(JobFailed) as cm:
            await sh.run_script(self.job("short.script"), self.ctx())
        self.assertEqual(cm.exception.message, "LLM が止まっています")
        with self.assertRaises(JobFailed) as cm:
            await sh.run_script(self.job("short.script"), self.ctx(llm=None))
        self.assertIn("LLM が設定されていません", cm.exception.message)


class SceneTest(HandlerBase):
    def setup_scenes(self, timeline, refs=None, images=None, **params):
        self.project(timeline, images=images, **params) if images is not None else self.project(timeline, **params)
        self.ledger.update_mv_project("proj1", scenes=self.scenes(timeline, refs=refs))
        self.work().mkdir(parents=True, exist_ok=True)
        if timeline == "speech":
            (self.work() / "vocals.wav").write_bytes(b"RIFFvocals")

    def clip_args(self) -> list:
        return next(c for c in self.ff.calls if c[2].endswith("のクリップの整形"))[0]

    async def test_speech_scene_sends_person_image_then_the_narration_with_vocal_lock(self):
        self.setup_scenes("speech", refs={1: [0], 2: [0], 3: [0]}, seed=3)
        job = self.job("short.scene", scene=2)
        await sh.run_scene(job, self.ctx())
        ((path, _),) = [(p, f) for p, f in self.h3.posts]
        self.assertEqual(path, "/api/ref2va")
        post = self.h3.posts[0]
        self.assertEqual(self.form(post), {"prompt": "prompt 2", "width": "768", "height": "448", "vocal_lock": "true", "seed": "1002"})        # seconds も mute も送らない
        files = self.files(post)
        self.assertEqual([(n, fn) for n, fn, _ in files], [("references", "girl.png"), ("references", "scene_002_vocals.wav")])      # 画像、そのシーンの読み上げの順
        self.assertEqual(files[1][2], b"RIFFscene")
        cut = next(c for c in self.audio.calls if c[0] == "cut")
        self.assertEqual(cut[1:], (self.work() / "vocals.wav", 8.0, 14.0, self.work() / "scene_002_vocals.wav", 16000, 1, 5.0))
        argv = self.clip_args()
        self.assertIn("-an", argv)                                                                       # 映像だけのクリップ(音は結合で読み上げに)
        self.assertEqual(argv[argv.index("-frames:v") + 1], "144")
        s = self.proj()["scenes"][1]
        self.assertEqual((s["status"], s["clip"], s["seed"], s["elapsed_s"], s["peak_vram_gb"], s["error"]), ("done", "scene_002.mp4", 99, 42.5, 11.0, None))
        self.assertTrue((self.work() / "scene_002.mp4").is_file())
        self.assertFalse((self.work() / "scene_002_raw.mp4").exists())
        self.assertEqual(job.result["scene"], 2)

    async def test_speech_scene_with_scenery_sends_images_in_refs_order_then_the_audio(self):
        images = [{"id": self.girl["id"], "role": "person"}, {"id": self.garden["id"], "role": "scenery"}, {"id": self.blank["id"], "role": "scenery"}]
        self.setup_scenes("speech", refs={1: [0, 2, 1]}, images=images)
        await sh.run_scene(self.job("short.scene", scene=1), self.ctx())
        self.assertEqual([fn for _, fn, _ in self.files(self.h3.posts[0])], ["girl.png", "blank.png", "garden.png", "scene_001_vocals.wav"])

    async def test_music_scene_without_refs_is_t2va_with_seconds_and_mute(self):
        self.setup_scenes("music", refs={})
        await sh.run_scene(self.job("short.scene", scene=1), self.ctx())
        post = self.h3.posts[0]
        self.assertEqual(post[0], "/api/t2va")
        self.assertEqual(self.files(post), [])
        self.assertEqual(self.form(post), {"prompt": "prompt 1", "width": "768", "height": "448", "seconds": "8", "mute": "true", "seed": "1001"})
        self.assertIn("-an", self.clip_args())
        self.assertFalse([c for c in self.audio.calls if c[0] == "cut"])

    async def test_music_scene_with_refs_is_ref2va_without_vocal_lock(self):
        images = [{"id": self.girl["id"], "role": "person"}, {"id": self.garden["id"], "role": "scenery"}]
        self.setup_scenes("music", refs={1: [0, 1], 2: [1]}, images=images)
        await sh.run_scene(self.job("short.scene", scene=1), self.ctx())
        await sh.run_scene(self.job("short.scene", scene=2), self.ctx())
        (p1, p2) = self.h3.posts
        self.assertEqual((p1[0], p2[0]), ("/api/ref2va", "/api/ref2va"))
        self.assertEqual([fn for _, fn, _ in self.files(p1)], ["girl.png", "garden.png"])
        self.assertEqual([fn for _, fn, _ in self.files(p2)], ["garden.png"])
        for post, secs in ((p1, "8"), (p2, "6")):
            form = self.form(post)
            self.assertEqual((form["seconds"], form["mute"]), (secs, "true"))
            self.assertNotIn("vocal_lock", form)

    async def test_free_scene_sends_seconds_only_and_keeps_the_h3_audio(self):
        images = [{"id": self.girl["id"], "role": "person"}]
        self.setup_scenes("free", refs={1: [], 2: [0]}, images=images)
        await sh.run_scene(self.job("short.scene", scene=1), self.ctx())
        await sh.run_scene(self.job("short.scene", scene=2), self.ctx())
        (t2va, ref2va) = self.h3.posts
        self.assertEqual(t2va[0], "/api/t2va")
        self.assertEqual(self.form(t2va), {"prompt": "prompt 1", "width": "768", "height": "448", "seconds": "8", "seed": "1001"})      # mute も vocal_lock も無し
        self.assertEqual(ref2va[0], "/api/ref2va")
        self.assertEqual(self.form(ref2va), {"prompt": "prompt 2", "width": "768", "height": "448", "seconds": "6", "seed": "1002"})
        self.assertEqual([fn for _, fn, _ in self.files(ref2va)], ["girl.png"])
        for call in [c for c in self.ff.calls if c[2].endswith("のクリップの整形")]:
            argv = call[0]
            self.assertNotIn("-an", argv)                                                                # 音声を残す
            self.assertEqual((argv[argv.index("-c:a") + 1], argv[argv.index("-ar") + 1], argv[argv.index("-ac") + 1]), ("aac", "48000", "2"))
            self.assertIn("apad=whole_dur=", argv[argv.index("-af") + 1])

    async def test_seconds_are_at_least_h3_minimum_and_at_most_its_maximum(self):
        self.project("free")
        self.ledger.update_mv_project("proj1", scenes=self.scenes("free", durations=(3.0, 20.0)))
        await sh.run_scene(self.job("short.scene", scene=1), self.ctx())
        await sh.run_scene(self.job("short.scene", scene=2), self.ctx())
        self.assertEqual([self.form(p)["seconds"] for p in self.h3.posts], ["5", "15"])

    async def test_short_clip_is_padded_like_mv(self):
        self.h3.result["duration_s"] = 5.0
        self.setup_scenes("music", refs={})
        await sh.run_scene(self.job("short.scene", scene=2), self.ctx())            # 6 秒 = 144 コマ。H3 が 5 秒しか返さない
        vf = self.clip_args()[self.clip_args().index("-vf") + 1]
        self.assertIn("tpad=stop_mode=clone:stop_duration=1.250", vf)

    async def test_invalid_state_fails_in_japanese(self):
        self.setup_scenes("speech", refs={1: [0]})
        (self.work() / "vocals.wav").unlink()
        with self.assertRaises(JobFailed) as cm:
            await sh.run_scene(self.job("short.scene", scene=1), self.ctx())
        self.assertIn("読み上げの音声が見つかりません", cm.exception.message)
        self.ledger.update_mv_project("proj1", scene_patches={2: {"refs": [4]}, 3: {"prompt": ""}})
        with self.assertRaises(JobFailed) as cm:
            await sh.run_scene(self.job("short.scene", scene=2), self.ctx())
        self.assertIn("参照画像の指定が正しくありません", cm.exception.message)
        with self.assertRaises(JobFailed) as cm:
            await sh.run_scene(self.job("short.scene", scene=3), self.ctx())
        self.assertIn("演出がまだありません", cm.exception.message)
        self.assertEqual(self.h3.posts, [])

    async def test_h3_failures_and_a_deleted_reference_image(self):
        self.setup_scenes("music", refs={1: [0]}, images=[{"id": self.garden["id"], "role": "scenery"}])
        self.ledger.delete_output(self.garden["id"])
        with self.assertRaises(JobFailed) as cm:
            await sh.run_scene(self.job("short.scene", scene=1), self.ctx())
        self.assertIn("参照画像が見つかりません", cm.exception.message)
        self.h3.result = {"job_id": "r"}                                           # video_url が無い
        self.ledger.update_mv_project("proj1", scene_patches={2: {"refs": []}})
        with self.assertRaises(JobFailed) as cm:
            await sh.run_scene(self.job("short.scene", scene=2), self.ctx())
        self.assertIn("H3 が動画を返しませんでした", cm.exception.message)

    async def test_cancel_before_start(self):
        self.setup_scenes("music", refs={})
        job = self.job("short.scene", scene=1)
        job.cancel_requested = True
        with self.assertRaises(JobCancelled):
            await sh.run_scene(job, self.ctx())
        self.assertEqual(self.h3.posts, [])


class AssembleTest(HandlerBase):
    def setup_done(self, timeline, final_audio="auto", song=False, **params):
        extra = {"song_id": self.song["id"]} if song else {}
        self.project(timeline, final_audio=final_audio, **extra, **params)
        scenes = self.scenes(timeline)
        self.work().mkdir(parents=True, exist_ok=True)
        for s in scenes:
            s["status"], s["clip"] = "done", f"scene_{s['index']:03d}.mp4"
            (self.work() / s["clip"]).write_bytes(b"clip")
        self.ledger.update_mv_project("proj1", scenes=scenes)
        if timeline == "speech":
            (self.work() / "narration.wav").write_bytes(b"RIFFnarration")
            self.ledger.update_mv_project("proj1", params_patch={"analysis": {"duration": 20.5, "scenes": 3}})
        elif timeline == "music":
            (self.work() / "song.wav").write_bytes(b"RIFFsongwav")
            self.ledger.update_mv_project("proj1", params_patch={"analysis": {"duration": 21.0, "scenes": 3}})
        else:
            self.ledger.update_mv_project("proj1", params_patch={"analysis": {"duration": 21.0, "scenes": 3}})

    def calls(self) -> list:
        return self.ff.calls

    async def run_it(self) -> Job:
        job = self.job("short.assemble")
        await sh.run_assemble(job, self.ctx())
        return job

    def dest(self) -> Path:
        return self.root / "video" / "short_proj1.mp4"

    def assertRegistered(self, job, timeline, final_audio):
        proj = self.proj()
        out = self.ledger.get_output(proj["output_id"])
        self.assertEqual(job.outputs, [out["id"]])
        self.assertEqual((out["kind"], out["tab"], out["path"], out["filename"]), ("video", "short", "video/short_proj1.mp4", out["filename"]))
        self.assertTrue(out["filename"].endswith("_short.mp4"))
        self.assertEqual((out["params"]["project_id"], out["params"]["timeline"], out["params"]["final_audio"], out["params"]["scenes"]), ("proj1", timeline, final_audio, 3))
        self.assertTrue(proj["params"]["assembled"])
        self.assertFalse(list((self.work()).glob("concat.txt")) + list(self.work().glob("video_only.mp4")))      # 作業用の中間ファイルは消す
        return out

    async def test_speech_without_a_song_replaces_the_audio_with_the_narration(self):
        self.setup_done("speech")
        job = await self.run_it()
        concat, mux = self.calls()
        self.assertEqual(concat[2], "シーンの結合")
        self.assertNotIn("-an", concat[0])
        self.assertEqual(concat[3], mvclip.concat_list([self.work() / f"scene_{i:03d}.mp4" for i in (1, 2, 3)]))
        self.assertEqual(concat[1], self.work() / "video_only.mp4")
        argv = mux[0]
        self.assertEqual(mux[1], self.dest())
        self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == "-i"], [str(self.work() / "video_only.mp4"), str(self.work() / "narration.wav")])
        self.assertEqual(argv[argv.index("-t") + 1], "20.500")                                       # 読み上げの長さまで
        self.assertNotIn("-filter_complex", argv)
        self.assertRegistered(job, "speech", "auto")
        self.assertEqual(self.ledger.get_output(self.proj()["output_id"])["duration_s"], 20.5)

    async def test_speech_with_a_song_mixes_it_under_the_narration_at_minus_14_db(self):
        self.setup_done("speech", song=True)
        job = await self.run_it()
        concat, mix = self.calls()
        argv = mix[0]
        self.assertEqual(mix[2], "読み上げと曲の混合")
        song = self.ledger.abs_path(self.song["path"])
        self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == "-i"], [str(self.work() / "video_only.mp4"), str(self.work() / "narration.wav"), str(song)])
        graph = argv[argv.index("-filter_complex") + 1]
        self.assertIn("volume=-14dB", graph)
        self.assertIn("afade=t=out:st=18.500:d=2.000", graph)                                      # 曲(30 秒)は読み上げ(20.5 秒)で切って、終わりの 2 秒をフェード
        self.assertIn("atrim=duration=20.500", graph)
        self.assertIn("amix=inputs=2:duration=first:dropout_transition=0:normalize=0", graph)
        self.assertEqual((argv[argv.index("-t") + 1], argv[argv.index("-map") + 1], mix[1]), ("20.500", "0:v:0", self.dest()))
        self.assertRegistered(job, "speech", "auto")

    async def test_a_song_shorter_than_the_narration_ends_there_without_looping(self):
        self.setup_done("speech", song=True)
        self.ledger.update_mv_project("proj1", params_patch={"analysis": {"duration": 40.0, "scenes": 3}})
        await self.run_it()
        graph = self.calls()[1][0][self.calls()[1][0].index("-filter_complex") + 1]
        self.assertIn("atrim=duration=30.000", graph)                                              # 曲の長さで終わる(繰り返さない)
        self.assertIn("afade=t=out:st=28.000:d=2.000", graph)
        self.assertNotIn("aloop", graph)
        self.assertNotIn("stream_loop", " ".join(self.calls()[1][0]))

    async def test_speech_with_subtitles_writes_ass_and_srt_and_burns_them_in(self):
        """subtitles=True(セリフ): sentences.json から ASS と SRT を作り、読み上げを載せる ffmpeg に ass フィルタを通す(映像は copy でなく H.264)。SRT は最終の動画の隣。"""
        self.setup_done("speech")
        self.ledger.update_mv_project("proj1", params_patch={"subtitles": True})
        (self.work() / "sentences.json").write_text(json.dumps({"duration": 20.5, "sentences": [
            {"index": 1, "text": "今日は、少しだけ早く学校を出た。", "start": 0.0, "end": 2.8, "duration": 2.8},
            {"index": 2, "text": "夕焼けの道は、いつもより長く見える。", "start": 2.8, "end": 5.9, "duration": 3.1}]}, ensure_ascii=False), "utf-8")
        job = await self.run_it()
        ass, srt = self.work() / "subtitles.ass", self.work() / "subtitles.srt"
        self.assertTrue(ass.is_file() and srt.is_file())
        self.assertIn("Dialogue: 0,0:00:00.00,0:00:02.80,Caption,,0,0,0,,今日は、少しだけ早く学校を出た。", ass.read_text("utf-8"))
        self.assertIn("1\n00:00:00,000 --> 00:00:02,800\n今日は、少しだけ早く学校を出た。", srt.read_text("utf-8"))
        concat, mux = self.calls()
        argv = mux[0]
        self.assertEqual(mux[2], "読み上げの差し替え")
        self.assertEqual(argv[argv.index("-vf") + 1], "ass=" + mvsub.filter_path(ass))
        self.assertEqual(argv[argv.index("-c:v") + 1], "libx264")
        self.assertNotIn("copy", argv[:argv.index("-c:a")])
        self.assertTrue(Path(self.dest()).with_suffix(".srt").is_file())              # 配布用の SRT が最終の動画の隣に
        rec = self.ledger.get_output(self.proj()["output_id"])
        self.assertEqual((rec["params"]["subtitles"], rec["params"]["srt"]), (True, Path(self.dest()).with_suffix(".srt").name))
        self.assertRegistered(job, "speech", "auto")

    async def test_subtitles_with_no_audio_burn_into_the_concatenated_video(self):
        self.setup_done("speech")
        self.ledger.update_mv_project("proj1", params_patch={"subtitles": True, "final_audio": "none"})
        (self.work() / "sentences.json").write_text(json.dumps({"duration": 20.5, "sentences": [
            {"index": 1, "text": "こんにちは。", "start": 0.0, "end": 2.0, "duration": 2.0}]}, ensure_ascii=False), "utf-8")
        await self.run_it()
        concat, burn = self.calls()
        self.assertEqual((concat[1], burn[1], burn[2]), (self.work() / "video_only.mp4", self.dest(), "テロップの焼き込み"))
        self.assertIn("-vf", burn[0])

    async def test_subtitles_are_ignored_without_narration(self):
        self.setup_done("music")
        self.ledger.update_mv_project("proj1", params_patch={"subtitles": True})
        await self.run_it()
        concat, mux = self.calls()
        self.assertNotIn("-vf", mux[0])
        self.assertFalse((self.work() / "subtitles.ass").exists())

    async def test_music_muxes_the_song_like_mv(self):
        self.setup_done("music")
        job = await self.run_it()
        concat, mux = self.calls()
        self.assertEqual(mux[2], "曲の差し替え")
        self.assertEqual(mux[0][:2], ["ffmpeg", "-y"])
        self.assertEqual(mux[0], mvclip.mux_args("ffmpeg", self.work() / "video_only.mp4", self.work() / "song.wav", mux[0][-1], 21.0))
        self.assertRegistered(job, "music", "auto")

    async def test_free_is_one_concat_that_keeps_the_clip_audio(self):
        self.setup_done("free")
        job = await self.run_it()
        (concat,) = self.calls()
        argv = concat[0]
        self.assertEqual((concat[1], concat[2]), (self.dest(), "シーンの結合"))
        self.assertNotIn("-an", argv)
        self.assertEqual(argv[argv.index("-c") + 1], "copy")
        self.assertIn("+faststart", argv)
        self.assertRegistered(job, "free", "auto")

    async def test_none_makes_a_video_only_file_for_every_timeline(self):
        for timeline, song in (("speech", False), ("speech", True), ("music", False), ("free", False)):
            self.ff.calls.clear()
            for p in (self.root / "video").glob("*") if (self.root / "video").exists() else []:
                p.unlink()
            self.ledger.delete_mv_project("proj1")
            self.setup_done(timeline, final_audio="none", song=song)
            job = await self.run_it()
            (concat,) = self.calls()
            self.assertEqual((concat[1], concat[2]), (self.dest(), "シーンの結合"), (timeline, song))
            self.assertIn("-an", concat[0])
            self.assertIn("+faststart", concat[0])
            self.assertNotIn("narration.wav", " ".join(concat[0]))
            self.assertRegistered(job, timeline, "none")

    async def test_reassembling_keeps_the_previous_video(self):
        self.setup_done("free")
        await self.run_it()
        await self.run_it()
        self.assertEqual({p.name for p in (self.root / "video").iterdir()}, {"short_proj1.mp4", "short_proj1_2.mp4"})
        self.assertEqual(len([o for o in self.ledger.list_outputs() if o["params"].get("project_id") == "proj1"]), 2)

    async def test_missing_files_fail_in_japanese(self):
        self.setup_done("speech", song=True)
        (self.work() / "narration.wav").unlink()
        with self.assertRaises(JobFailed) as cm:
            await self.run_it()
        self.assertIn("読み上げの音声が見つかりません", cm.exception.message)
        (self.work() / "narration.wav").write_bytes(b"x")
        self.ledger.delete_output(self.song["id"])
        with self.assertRaises(JobFailed) as cm:
            await self.run_it()
        self.assertIn("曲のファイルが見つかりません", cm.exception.message)
        (self.work() / "scene_002.mp4").unlink()
        with self.assertRaises(JobFailed) as cm:
            await self.run_it()
        self.assertIn("scene_002.mp4", cm.exception.message)
        self.ledger.update_mv_project("proj1", scene_patches={1: {"status": "failed"}})
        with self.assertRaises(JobFailed) as cm:
            await self.run_it()
        self.assertIn("まだ生成できていないシーンがあります", cm.exception.message)
        self.assertEqual(self.calls(), [])

    async def test_none_does_not_need_the_narration_or_the_song(self):
        self.setup_done("speech", final_audio="none", song=True)
        (self.work() / "narration.wav").unlink()
        self.ledger.delete_output(self.song["id"])
        await self.run_it()
        self.assertEqual(len(self.calls()), 1)

    async def test_cancel_before_start(self):
        self.setup_done("free")
        job = self.job("short.assemble")
        job.cancel_requested = True
        with self.assertRaises(JobCancelled):
            await sh.run_assemble(job, self.ctx())


if __name__ == "__main__":
    unittest.main()
