import asyncio
import importlib.util
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import wave
from pathlib import Path
from unittest import mock

import psutil

from studio import mv_audio
from studio.config import REPO_ROOT, Settings

SCRIPTS = REPO_ROOT / "studio" / "mvtools"
_spec = importlib.util.spec_from_file_location("mvtools_analyze", SCRIPTS / "analyze.py")
A = importlib.util.module_from_spec(_spec)
sys.modules["mvtools_analyze"] = A
_spec.loader.exec_module(A)

try:
    REAL_TOOLS = mv_audio.tools_python()          # setup --mv で作った道具用の venv(無ければ統合試験は飛ばす)
except mv_audio.MvToolsMissing:
    REAL_TOOLS = None


def touch(p: Path, text: str = "x") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")                 # 偽のスクリプトの日本語を、Windows の cp932 で書かない
    return p


def utf8_env() -> dict:
    return {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}


# ---- 道具用の Python の場所 ----

class ToolsPythonTest(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.root = Path(self._t.name)
        self.sub = ("Scripts", "python.exe") if sys.platform == "win32" else ("bin", "python")

    def tearDown(self):
        self._t.cleanup()

    def test_hint_text(self):
        self.assertEqual(mv_audio.MISSING_HINT,
                         "setup --mv を実行してください(または studio.local.toml の [mv] tools_python に Demucs と librosa の入った Python を指定)")

    def test_venv_python_per_platform(self):
        self.assertEqual(mv_audio.venv_python(Path("v"), "win32"), Path("v", "Scripts", "python.exe"))
        self.assertEqual(mv_audio.venv_python(Path("v"), "linux"), Path("v", "bin", "python"))

    def test_default_is_under_components_mvtools(self):
        cfg = Settings({}, self.root)
        self.assertEqual(mv_audio.tools_dir(cfg), self.root / "components" / "mvtools")
        self.assertEqual(mv_audio.default_python(cfg), self.root / "components" / "mvtools" / ".venv" / self.sub[0] / self.sub[1])

    def test_components_dir_can_be_absolute_or_elsewhere(self):
        other = self.root / "elsewhere"
        self.assertEqual(mv_audio.tools_dir(Settings({"components_dir": str(other)}, self.root)), other / "mvtools")
        self.assertEqual(mv_audio.tools_dir(Settings({"components_dir": "c2"}, self.root)), self.root / "c2" / "mvtools")

    def test_missing_default_raises_with_hint(self):
        cfg = Settings({}, self.root)
        self.assertFalse(mv_audio.tools_available(cfg))
        with self.assertRaises(mv_audio.MvToolsMissing) as cm:
            mv_audio.tools_python(cfg)
        self.assertIn(mv_audio.MISSING_HINT, cm.exception.message)
        self.assertIsInstance(cm.exception, mv_audio.MvAudioError)

    def test_existing_default_is_returned(self):
        cfg = Settings({}, self.root)
        py = touch(mv_audio.default_python(cfg))
        self.assertEqual(mv_audio.tools_python(cfg), py)
        self.assertTrue(mv_audio.tools_available(cfg))

    def test_configured_absolute_relative_and_command_name(self):
        mine = touch(self.root / "my" / "python")
        for value in (str(mine), "my/python"):      # 絶対パスと、リポジトリ直下からの相対パス
            cfg = types.SimpleNamespace(root=self.root, components_dir="components", mv={"tools_python": value})
            self.assertEqual(mv_audio.tools_python(cfg), mine)
        cfg = types.SimpleNamespace(root=self.root, components_dir="components", mv={"tools_python": "python3"})
        with mock.patch.object(mv_audio.shutil, "which", return_value="/usr/bin/python3"):
            self.assertEqual(mv_audio.tools_python(cfg), Path("/usr/bin/python3"))

    def test_configured_missing_raises_naming_the_value(self):
        cfg = types.SimpleNamespace(root=self.root, components_dir="components", mv={"tools_python": "nope/python"})
        with mock.patch.object(mv_audio.shutil, "which", return_value=None), self.assertRaises(mv_audio.MvToolsMissing) as cm:
            mv_audio.tools_python(cfg)
        self.assertIn("[mv] tools_python", cm.exception.message)
        self.assertIn("nope/python", cm.exception.message)
        self.assertIn(mv_audio.MISSING_HINT, cm.exception.message)

    def test_raw_data_table_and_override_argument(self):
        mine, other = touch(self.root / "mine"), touch(self.root / "other")
        raw = types.SimpleNamespace(root=self.root, components_dir="components", data={"mv": {"tools_python": str(mine)}})
        self.assertEqual(mv_audio.tools_python(raw), mine)
        self.assertEqual(mv_audio.tools_python(raw, override=str(other)), other)        # 引数が設定より優先
        self.assertEqual(mv_audio.configured_python(Settings({}, self.root)), "")        # [mv] の無い Settings は空

    def test_settings_without_root_fall_back_to_the_repo(self):
        self.assertEqual(mv_audio.tools_dir(types.SimpleNamespace()), REPO_ROOT / "components" / "mvtools")


# ---- 進み具合の行 ----

class ProgressTest(unittest.TestCase):
    def test_progress_fraction(self):
        self.assertAlmostEqual(mv_audio.progress_fraction("progress 3/12"), 0.25)
        self.assertEqual(mv_audio.progress_fraction("progress 12/12"), 1.0)
        self.assertEqual(mv_audio.progress_fraction("  progress 5/10  "), 0.5)
        self.assertEqual(mv_audio.progress_fraction("progress 13/12"), 1.0)
        self.assertIsNone(mv_audio.progress_fraction("progress 0/0"))
        self.assertIsNone(mv_audio.progress_fraction("device: cpu"))
        self.assertIsNone(mv_audio.progress_fraction(""))

    def test_parse_result_takes_the_last_line(self):
        self.assertEqual(mv_audio.parse_result('noise\n\n{"a": 1}\n'), {"a": 1})
        for bad in ("", "\n", "not json", "[1, 2]"):
            with self.assertRaises(ValueError):
                mv_audio.parse_result(bad)


# ---- 子プロセス(偽の道具のスクリプトで、本物の Demucs / librosa 無しに試す) ----

FAKE_SEPARATE = r'''
import json, os, sys
src, out, rest = sys.argv[1], sys.argv[2], sys.argv[3:]
print("device: cpu", file=sys.stderr, flush=True)
for i in range(4):
    print(f"progress {i}/3", file=sys.stderr, flush=True)
print("stray output from a library")
os.makedirs(out, exist_ok=True)
for name in ("vocals.wav", "accompaniment.wav"):
    open(os.path.join(out, name), "wb").write(b"RIFF")
json.dump({"argv": sys.argv[1:], "env": {k: os.environ.get(k) for k in ("PYTHONIOENCODING", "PYTHONUTF8", "PYTHONPATH")}},
          open(os.path.join(out, "seen.json"), "w"))
print(json.dumps({"vocals": os.path.join(out, "vocals.wav"), "accompaniment": os.path.join(out, "accompaniment.wav"),
                  "seconds": 1.5, "device": "cpu"}))
'''

FAKE_ANALYZE = r'''
import json, sys
print("progress 1/1", file=sys.stderr, flush=True)
print(json.dumps({"duration": 12.0, "bpm": 120.0, "has_vocals": True, "argv": sys.argv[1:],
                  "scenes": [{"index": 1, "start": 0.0, "end": 6.0, "duration": 6.0}, {"index": 2, "start": 6.0, "end": 12.0, "duration": 6.0}]}))
'''

FAKE_FAIL = r'''
import sys
for i in range(60):
    print(f"line {i}", file=sys.stderr)
print("Traceback (most recent call last):", file=sys.stderr)
print("ValueError: boom", file=sys.stderr)
print("エラー: ボーカルを分けられません(試験)", file=sys.stderr)
sys.exit(1)
'''

FAKE_BAD_OUTPUT = r'''
print("this is not json")
'''

FAKE_SLEEP = r'''
import os, sys, time
open(sys.argv[2] + ".pid", "w").write(str(os.getpid()))
print("progress 0/9", file=sys.stderr, flush=True)
time.sleep(120)
'''


class WrapperTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.root = Path(self._t.name)
        self.wav = touch(self.root / "song.wav", "RIFF")
        self.out = self.root / "sep"

    def script(self, name: str, body: str):
        path = touch(self.root / f"{name}.py", body)
        target = {"separate": "SEPARATE_SCRIPT", "analyze": "ANALYZE_SCRIPT"}[name.split("_")[0]]
        p = mock.patch.object(mv_audio, target, path)
        p.start()
        self.addCleanup(p.stop)
        return path

    async def test_separate_builds_the_command_and_parses_the_last_line(self):
        self.script("separate", FAKE_SEPARATE)
        res = await mv_audio.separate(self.wav, self.out, device="cpu", python=sys.executable)
        self.assertEqual(res, {"vocals": self.out / "vocals.wav", "accompaniment": self.out / "accompaniment.wav",
                               "seconds": 1.5, "device": "cpu"})
        import json
        seen = json.loads((self.out / "seen.json").read_text())
        self.assertEqual(seen["argv"], [str(self.wav), str(self.out), "--device", "cpu", "--model", "htdemucs"])
        self.assertEqual(seen["env"]["PYTHONIOENCODING"], "utf-8")
        self.assertEqual(seen["env"]["PYTHONUTF8"], "1")

    async def test_python_path_env_is_not_inherited(self):
        self.script("separate", FAKE_SEPARATE)
        with mock.patch.dict(os.environ, {"PYTHONPATH": str(self.root / "poison")}):
            await mv_audio.separate(self.wav, self.out, python=sys.executable)
        import json
        self.assertIsNone(json.loads((self.out / "seen.json").read_text())["env"]["PYTHONPATH"])

    async def test_progress_lines_arrive_in_order_on_the_loop_thread(self):
        self.script("separate", FAKE_SEPARATE)
        got, threads = [], set()

        def on_progress(line):
            got.append(line)
            threads.add(threading.get_ident())
        await mv_audio.separate(self.wav, self.out, on_progress=on_progress, python=sys.executable)
        self.assertEqual(got, ["device: cpu", "progress 0/3", "progress 1/3", "progress 2/3", "progress 3/3"])
        self.assertEqual(threads, {threading.get_ident()})
        self.assertEqual([mv_audio.progress_fraction(x) for x in got[1:]], [0.0, 1 / 3, 2 / 3, 1.0])

    async def test_async_progress_callback_is_awaited(self):
        self.script("separate", FAKE_SEPARATE)
        got = []

        async def on_progress(line):
            await asyncio.sleep(0)
            got.append(line)
        await mv_audio.separate(self.wav, self.out, on_progress=on_progress, python=sys.executable)
        await asyncio.sleep(0.05)
        self.assertEqual(got[-1], "progress 3/3")

    async def test_a_failing_callback_does_not_break_the_run(self):
        self.script("separate", FAKE_SEPARATE)

        def boom(line):
            raise RuntimeError("callback bug")
        with self.assertLogs("studio.mv_audio", level="ERROR"):
            res = await mv_audio.separate(self.wav, self.out, on_progress=boom, python=sys.executable)
        self.assertEqual(res["device"], "cpu")

    async def test_analyze_passes_the_scene_range_and_returns_the_plan(self):
        self.script("analyze", FAKE_ANALYZE)
        plan = await mv_audio.analyze(self.wav, self.root / "vocals.wav", 5, 10, 8, python=sys.executable)
        self.assertEqual(plan["argv"], [str(self.wav), str(self.root / "vocals.wav"), "--min", "5", "--max", "10", "--target", "8"])
        self.assertEqual([s["index"] for s in plan["scenes"]], [1, 2])
        self.assertEqual((plan["duration"], plan["bpm"]), (12.0, 120.0))

    async def test_failure_gives_the_reason_and_the_log_tail(self):
        self.script("separate", FAKE_FAIL)
        with self.assertRaises(mv_audio.MvAudioError) as cm:
            await mv_audio.separate(self.wav, self.out, python=sys.executable)
        e = cm.exception
        self.assertIn("ボーカルの分離に失敗しました", e.message)
        self.assertIn("ボーカルを分けられません(試験)", e.message)
        lines = e.log_tail.splitlines()
        self.assertEqual(len(lines), mv_audio.LOG_TAIL_LINES)
        self.assertEqual(lines[-1], "エラー: ボーカルを分けられません(試験)")
        self.assertIn("ValueError: boom", lines)
        self.assertNotIn("line 0", lines)
        self.assertFalse(isinstance(e, mv_audio.MvToolsMissing))

    async def test_output_that_is_not_json_is_an_error(self):
        self.script("separate", FAKE_BAD_OUTPUT)
        with self.assertRaises(mv_audio.MvAudioError) as cm:
            await mv_audio.separate(self.wav, self.out, python=sys.executable)
        self.assertIn("結果を読めません", cm.exception.message)

    async def test_result_files_must_exist(self):
        self.script("separate", FAKE_SEPARATE)
        with mock.patch.object(mv_audio, "parse_result", return_value={"vocals": str(self.root / "no.wav"), "accompaniment": str(self.wav)}):
            with self.assertRaises(mv_audio.MvAudioError) as cm:
                await mv_audio.separate(self.wav, self.out, python=sys.executable)
        self.assertIn("結果のファイルがありません", cm.exception.message)

    async def test_missing_tools_raise_with_the_hint(self):
        with self.assertRaises(mv_audio.MvToolsMissing) as cm:
            await mv_audio.separate(self.wav, self.out, settings=Settings({}, self.root))
        self.assertIn(mv_audio.MISSING_HINT, cm.exception.message)
        with self.assertRaises(mv_audio.MvToolsMissing):
            await mv_audio.analyze(self.wav, self.wav, python=self.root / "no" / "python")

    async def test_cancel_kills_the_child_process(self):
        self.script("separate", FAKE_SLEEP)
        pidfile = Path(str(self.out) + ".pid")
        task = asyncio.create_task(mv_audio.separate(self.wav, self.out, python=sys.executable))
        for _ in range(150):
            if pidfile.exists() and pidfile.read_text():
                break
            await asyncio.sleep(0.1)
        pid = int(pidfile.read_text())
        self.assertTrue(psutil.pid_exists(pid))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        for _ in range(100):
            if not psutil.pid_exists(pid):
                break
            await asyncio.sleep(0.1)
        self.assertFalse(psutil.pid_exists(pid))

    async def test_timeout_stops_the_child_process(self):
        self.script("separate", FAKE_SLEEP)
        with self.assertRaises(mv_audio.MvAudioError) as cm:
            await mv_audio.separate(self.wav, self.out, python=sys.executable, timeout=2.0)
        self.assertIn("超えたので止めました", cm.exception.message)
        pid = int(Path(str(self.out) + ".pid").read_text())
        for _ in range(50):
            if not psutil.pid_exists(pid):
                break
            await asyncio.sleep(0.1)
        self.assertFalse(psutil.pid_exists(pid))

    async def test_no_window_flag_is_passed_on_popen(self):
        self.script("separate", FAKE_SEPARATE)
        real = subprocess.Popen
        seen = {}

        def spy(*a, **kw):
            seen.update(kw)
            return real(*a, **kw)
        with mock.patch.object(mv_audio.subprocess, "Popen", spy):
            await mv_audio.separate(self.wav, self.out, python=sys.executable)
        self.assertEqual(seen["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(seen["stdin"], subprocess.DEVNULL)


# ---- ffmpeg(偽の ffmpeg で引数を確かめる) ----

class FakeFfmpeg:
    """subprocess.Popen の代わり(_run_ffmpeg が使う口だけ)。引数を残し、成功なら出力先に書く。"""

    def __init__(self, rc=0, stderr=b"", write=True):
        self.rc, self.stderr, self.write, self.calls, self.timeouts = rc, stderr, write, [], []

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw))
        fake = self

        class Proc:
            pid, returncode = 999999, None

            def communicate(self, timeout=None):
                fake.timeouts.append(timeout)
                if fake.rc == 0 and fake.write:
                    Path(argv[-1]).write_bytes(b"RIFFdata")
                self.returncode = fake.rc
                return b"", fake.stderr

            def poll(self):
                return self.returncode

            def kill(self):
                pass
        return Proc()


class FfmpegArgsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.root = Path(self._t.name)
        self.src, self.dst = self.root / "in.mp3", self.root / "out" / "song.wav"
        p = mock.patch.object(mv_audio.media, "_tool", return_value="/fake/ffmpeg")
        p.start()
        self.addCleanup(p.stop)

    def run_with(self, fake):
        return mock.patch.object(mv_audio.subprocess, "Popen", fake)

    async def test_normalize_command_and_atomic_result(self):
        fake = FakeFfmpeg()
        with self.run_with(fake):
            out = await mv_audio.normalize(self.src, self.dst)
        self.assertEqual(out, self.dst)
        argv, kw = fake.calls[0]
        self.assertEqual(argv, ["/fake/ffmpeg", "-v", "error", "-y", "-i", str(self.src), "-vn", "-ac", "2", "-ar", "44100",
                                "-c:a", "pcm_s16le", "-f", "wav", str(self.dst) + ".part"])
        self.assertEqual(kw["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(self.dst.read_bytes(), b"RIFFdata")
        self.assertFalse(Path(str(self.dst) + ".part").exists())

    async def test_cut_command_variants(self):
        tmp = str(self.dst) + ".part"
        cases = [
            (dict(), ["-ss", "1.500", "-i", str(self.src), "-t", "3.250", "-vn", "-c:a", "pcm_s16le", "-f", "wav", tmp]),
            (dict(rate=16000, channels=1), ["-ss", "1.500", "-i", str(self.src), "-t", "3.250", "-ar", "16000", "-ac", "1",
                                            "-vn", "-c:a", "pcm_s16le", "-f", "wav", tmp]),
            (dict(pad_to=5.0), ["-ss", "1.500", "-i", str(self.src), "-af", "apad=whole_dur=5.000", "-t", "5.000", "-vn",
                                "-c:a", "pcm_s16le", "-f", "wav", tmp]),
            (dict(pad_to=2.0), ["-ss", "1.500", "-i", str(self.src), "-t", "3.250", "-vn", "-c:a", "pcm_s16le", "-f", "wav", tmp]),
        ]
        for kwargs, tail in cases:
            fake = FakeFfmpeg()
            with self.run_with(fake):
                await mv_audio.cut(self.src, 1.5, 4.75, self.dst, **kwargs)
            self.assertEqual(fake.calls[0][0], ["/fake/ffmpeg", "-v", "error", "-y"] + tail, kwargs)
            self.assertEqual(self.dst.read_bytes(), b"RIFFdata")

    async def test_cut_rejects_an_empty_range_without_running_ffmpeg(self):
        fake = FakeFfmpeg()
        with self.run_with(fake):
            for start, end in ((3.0, 3.0), (4.0, 3.0), (-1.0, 2.0)):
                with self.assertRaises(mv_audio.MvAudioError):
                    await mv_audio.cut(self.src, start, end, self.dst)
        self.assertEqual(fake.calls, [])

    async def test_failure_keeps_no_partial_file_and_reports_the_last_line(self):
        fake = FakeFfmpeg(rc=1, stderr=b"Invalid data found\nin.mp3: No such file or directory\n", write=False)
        Path(str(self.dst) + ".part").parent.mkdir(parents=True, exist_ok=True)
        touch(Path(str(self.dst) + ".part"), "half")
        with self.run_with(fake), self.assertRaises(mv_audio.MvAudioError) as cm:
            await mv_audio.normalize(self.src, self.dst)
        self.assertIn("音声の変換に失敗しました", cm.exception.message)
        self.assertIn("in.mp3: No such file or directory", cm.exception.message)
        self.assertIn("Invalid data found", cm.exception.log_tail)
        self.assertFalse(self.dst.exists())
        self.assertFalse(Path(str(self.dst) + ".part").exists())

    async def test_empty_output_is_a_failure(self):
        fake = FakeFfmpeg(rc=0, write=False)
        with self.run_with(fake), self.assertRaises(mv_audio.MvAudioError):
            await mv_audio.cut(self.src, 0.0, 1.0, self.dst)

    async def test_ffmpeg_missing(self):
        with mock.patch.object(mv_audio.media, "_tool", return_value=None), self.assertRaises(mv_audio.MvAudioError) as cm:
            await mv_audio.normalize(self.src, self.dst)
        self.assertIn("ffmpeg が見つかりません", cm.exception.message)


class StoppableFfmpegTest(unittest.IsolatedAsyncioTestCase):
    """ffmpeg の代わりに眠るだけの子プロセスで、取り消し(ジョブの中止・管理役の停止)と時間切れで止まることを確かめる。"""

    SLEEPER = [sys.executable, "-c", "import os, sys, time; open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(60)"]

    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.root = Path(self._t.name)

    async def test_cancel_kills_the_process_and_leaves_no_partial_file(self):
        pidfile, dst = self.root / "pid", self.root / "out.mp4"
        task = asyncio.create_task(mv_audio.run_ffmpeg([*self.SLEEPER, str(pidfile)], dst, "結合"))
        for _ in range(500):
            if pidfile.is_file() and pidfile.read_text():
                break
            await asyncio.sleep(0.01)
        t0 = time.monotonic()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertLess(time.monotonic() - t0, 5)
        self.assertFalse(psutil.pid_exists(int(pidfile.read_text())))         # 止めて、片づけ(wait)まで済んでいる
        self.assertEqual(list(self.root.glob("*.part")), [])

    async def test_timeout_stops_the_process(self):
        with self.assertRaises(mv_audio.MvAudioError) as cm:
            await asyncio.to_thread(mv_audio._run_ffmpeg, [*self.SLEEPER, str(self.root / "pid")], self.root / "out.mp4", "テロップの焼き込み", timeout=0.5)
        self.assertIn("テロップの焼き込みが", cm.exception.message)
        self.assertIn("超えたので止めました", cm.exception.message)

    def test_timeout_scales_with_the_length(self):
        self.assertEqual([mv_audio.ffmpeg_timeout(x) for x in (None, 0, 10, 30, 600, "x")], [600.0, 600.0, 600.0, 600.0, 12000.0, 600.0])


def write_sine(path: Path, seconds: float, rate: int, channels: int = 1, freq: float = 440.0) -> Path:
    frames = b"".join(struct.pack("<h", int(12000 * math.sin(2 * math.pi * freq * i / rate))) * channels
                      for i in range(int(seconds * rate)))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    return path


def wav_info(path: Path) -> tuple:
    with wave.open(str(path), "rb") as w:
        return w.getframerate(), w.getnchannels(), w.getnframes() / w.getframerate()


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg が PATH に無い")
class RealFfmpegTest(unittest.IsolatedAsyncioTestCase):
    async def test_normalize_then_cut(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            src = write_sine(t / "a.wav", 5.0, 22050)
            norm = await mv_audio.normalize(src, t / "n" / "song.wav")
            rate, ch, secs = wav_info(norm)
            self.assertEqual((rate, ch), (44100, 2))
            self.assertAlmostEqual(secs, 5.0, delta=0.05)
            clip = await mv_audio.cut(norm, 1.0, 3.5, t / "c" / "clip.wav")
            rate, ch, secs = wav_info(clip)
            self.assertEqual((rate, ch), (44100, 2))
            self.assertAlmostEqual(secs, 2.5, delta=0.002)
            padded = await mv_audio.cut(norm, 1.0, 2.0, t / "p.wav", rate=16000, channels=1, pad_to=3.0)
            rate, ch, secs = wav_info(padded)
            self.assertEqual((rate, ch), (16000, 1))
            self.assertAlmostEqual(secs, 3.0, delta=0.002)
            self.assertEqual([p.name for p in t.rglob("*.part")], [])

    async def test_a_broken_input_is_an_error(self):
        with tempfile.TemporaryDirectory() as t:
            bad = touch(Path(t) / "bad.mp3", "this is not audio")
            with self.assertRaises(mv_audio.MvAudioError) as cm:
                await mv_audio.normalize(bad, Path(t) / "out.wav")
            self.assertIn("音声の変換に失敗しました", cm.exception.message)
            self.assertFalse((Path(t) / "out.wav").exists())


# ---- シーン分割(動的計画法。標準ライブラリだけで動く部分) ----

def analysis(duration: float, candidates=None, rms: float = -12.0, **extra) -> dict:
    grid = [{"time": round(i * 0.25, 2), "score": 0.0, "kind": "fallback", "details": {}}
            for i in range(1, math.ceil(duration / 0.25))]
    return {"duration": duration, "window_seconds": 0.1, "rms_db": [rms] * max(1, round(duration * 10)),
            "candidates": [{"time": t, "score": s, "kind": k, "details": {}} for t, s, k in (candidates or [])] + grid, **extra}


class PlanScenesTest(unittest.TestCase):
    def check_tiling(self, scenes, duration, lo=5.0, hi=10.0):
        self.assertEqual(scenes[0]["start"], 0.0)
        self.assertEqual(scenes[-1]["end"], duration)
        for a, b in zip(scenes, scenes[1:]):
            self.assertEqual(a["end"], b["start"])
        for i, s in enumerate(scenes, start=1):
            self.assertEqual(s["index"], i)
            self.assertEqual(s["duration"], round(s["end"] - s["start"], 3))
            self.assertGreaterEqual(s["duration"], lo - 1e-9)
            self.assertLessEqual(s["duration"], hi + 1e-9)
        self.assertAlmostEqual(sum(s["duration"] for s in scenes), duration, places=2)

    def test_scenes_tile_the_song_within_bounds_for_many_lengths(self):
        for duration in (5.0, 5.2, 9.99, 10.0, 10.1, 10.3, 12.7, 24.0, 27.0, 32.0, 57.0, 57.03, 60.03, 61.37, 99.99, 180.0, 311.6):
            with self.subTest(duration=duration):
                self.check_tiling(A.plan_scenes(analysis(duration)), duration)

    def test_other_ranges_and_targets(self):
        for lo, hi, target in ((5, 10, 8), (6, 8, 7), (4, 6, 5), (3, 5, 4)):
            for duration in (23.4, 60.0, 121.7):
                with self.subTest(range=(lo, hi), duration=duration):
                    scenes = A.plan_scenes(analysis(duration), min_seconds=lo, max_seconds=hi, target_seconds=target)
                    self.check_tiling(scenes, duration, lo, hi)

    def test_natural_silence_wins_over_an_equal_split(self):
        scenes = A.plan_scenes(analysis(27.0, [(8.2, 100.0, "silence"), (17.1, 100.0, "silence")]))
        self.assertEqual(len(scenes), 3)
        self.assertAlmostEqual(scenes[0]["end"], 8.2, delta=0.05)
        self.assertAlmostEqual(scenes[1]["end"], 17.1, delta=0.05)
        self.assertIn("silence", scenes[0]["boundary_kind"])
        self.assertEqual(scenes[-1]["boundary_kind"], "song_end")
        self.assertEqual(scenes[-1]["boundary_score"], 0.0)

    def test_candidates_near_the_ends_do_not_move_them(self):
        near_end = analysis(60.03, [(60.0, 100.0, "silence"), (59.97, 50.0, "beat")])
        self.check_tiling(A.plan_scenes(near_end), 60.03)
        near_start = analysis(30.0, [(0.05, 100.0, "silence")])
        self.check_tiling(A.plan_scenes(near_start), 30.0)

    def test_short_song_is_a_single_scene_of_its_real_length(self):
        for duration in (3.7, 5.0):
            scenes = A.plan_scenes(analysis(duration))
            self.assertEqual(len(scenes), 1)
            self.assertEqual((scenes[0]["start"], scenes[0]["end"], scenes[0]["duration"]), (0.0, duration, duration))
            self.assertEqual(scenes[0]["boundary_kind"], "song_end")

    def test_vocal_ratio_and_kind(self):
        loud = A.plan_scenes(analysis(24.0, rms=-10.0))
        self.assertTrue(all(s["vocal_ratio"] == 1.0 and s["kind"] == "vocal" for s in loud))
        quiet = A.plan_scenes(analysis(24.0, rms=-60.0))
        self.assertTrue(all(s["vocal_ratio"] == 0.0 and s["kind"] == "instrumental" for s in quiet))
        # 前半だけ歌っている: 歌っている窓の割合がシーンごとに出て、0.35 以上が vocal
        rms = [-10.0] * 120 + [-60.0] * 120
        mixed = A.plan_scenes({**analysis(24.0), "rms_db": rms})
        by_end = {s["end"]: s for s in mixed}
        self.assertEqual(mixed[0]["kind"], "vocal")
        self.assertEqual(mixed[-1]["kind"], "instrumental")
        for s in mixed:
            lo, hi = int(s["start"] / 0.1), math.ceil(s["end"] / 0.1)
            self.assertAlmostEqual(s["vocal_ratio"], sum(v > -35 for v in rms[lo:hi]) / (hi - lo), places=3)
            self.assertEqual(s["kind"], "vocal" if s["vocal_ratio"] >= 0.35 else "instrumental")
        self.assertEqual(len(by_end), len(mixed))

    def test_a_song_without_vocals_has_no_vocal_scenes(self):
        scenes = A.plan_scenes(analysis(24.0, rms=-10.0, has_vocals=False))
        self.assertTrue(all(s["vocal_ratio"] == 0.0 and s["kind"] == "instrumental" for s in scenes))
        self.assertTrue(A.has_vocals(-15.5, -14.3))
        self.assertTrue(A.has_vocals(-30.0, -20.0))           # 静かな歌でも、曲の最大の 30dB 以内なら歌あり
        self.assertFalse(A.has_vocals(-45.9, -14.3))          # 歌の無い曲を分けたときのボーカル(実測)
        self.assertFalse(A.has_vocals(-35.0, -3.0))           # 曲の最大より 30dB 以上小さい
        self.assertFalse(A.has_vocals(-41.0, -42.0))          # 絶対値が小さすぎる

    def test_boundary_scores_follow_the_cost_terms(self):
        # 3 点の経路: 5 秒ちょうどで切れる強い候補が 2 つあっても、短い 5 秒のシーンは(7 秒未満の追加費用で)選ばれにくい
        penalty = A._boundary_penalty
        self.assertEqual(penalty(9.0, 9.0), 0.0)
        self.assertEqual(penalty(10.0, 9.0), 5.0)
        self.assertEqual(penalty(5.0, 9.0), 4 * 5.0 + 2 * 9.0)
        self.assertEqual(penalty(7.0, 7.0), 0.0)

    def test_dp_failure_falls_back_to_an_even_split(self):
        # 10.1 秒の曲を 5〜10 秒に分けるには 5.0〜5.1 秒で切るしかないが、その範囲に候補が無い
        an = {"duration": 10.1, "window_seconds": 0.1, "rms_db": [-10.0] * 101,
              "candidates": [{"time": 4.0, "score": 50.0, "kind": "bar", "details": {}},
                             {"time": 6.0, "score": 50.0, "kind": "bar", "details": {}}]}
        scenes = A.plan_scenes(an)
        self.assertEqual([s["boundary_kind"] for s in scenes], ["even_split", "song_end"])
        self.assertEqual([s["duration"] for s in scenes], [5.05, 5.05])
        self.check_tiling(scenes, 10.1)

    def test_impossible_range_is_an_error(self):
        with self.assertRaises(A.AnalyzeError) as cm:
            A.plan_scenes(analysis(9.0), min_seconds=6.0, max_seconds=8.0)
        self.assertIn("分割できませんでした", str(cm.exception))

    def test_input_analysis_is_not_modified(self):
        an = analysis(40.0, [(10.0, 100.0, "silence")])
        before = repr(an)
        A.plan_scenes(an)
        self.assertEqual(repr(an), before)

    def test_merge_candidates_weights_by_score(self):
        a, b = A.BoundaryCandidate(8.0, 100.0, "silence", {"x": 1}), A.BoundaryCandidate(8.2, 26.0, "bar", {"y": 2})
        far = A.BoundaryCandidate(9.0, 10.0, "beat", {})
        merged = A._merge_candidates([b, far, a])
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0].kind, "bar+silence")
        self.assertEqual(merged[0].score, 126.0)
        self.assertAlmostEqual(merged[0].time, round((8.0 * 100 + 8.2 * 26) / 126, 3))
        self.assertEqual(merged[0].details, {"x": 1, "y": 2})


class ScriptCliTest(unittest.TestCase):
    """道具のスクリプトを(重いパッケージ無しで届く範囲で)コマンドラインから動かす。失敗は stderr の最後の行と終了コード 1。"""

    def run_script(self, name, *args):
        return subprocess.run([sys.executable, str(SCRIPTS / name), *args], capture_output=True, text=True, encoding="utf-8",
                              env=utf8_env(), timeout=120)

    def test_analyze_reports_missing_files_and_bad_ranges(self):
        with tempfile.TemporaryDirectory() as t:
            r = self.run_script("analyze.py", str(Path(t) / "song.wav"), str(Path(t) / "vocals.wav"))
            self.assertEqual(r.returncode, 1)
            self.assertEqual(r.stdout, "")
            self.assertTrue(r.stderr.strip().splitlines()[-1].startswith("エラー: 曲の音声がありません"))
            a, b = touch(Path(t) / "a.wav"), touch(Path(t) / "b.wav")
            r = self.run_script("analyze.py", str(a), str(b), "--min", "11", "--max", "10")
            self.assertEqual(r.returncode, 1)
            self.assertIn("シーンの長さの範囲が正しくありません", r.stderr.strip().splitlines()[-1])

    def test_separate_reports_a_missing_input(self):
        with tempfile.TemporaryDirectory() as t:
            r = self.run_script("separate.py", str(Path(t) / "nope.wav"), str(Path(t) / "out"))
            self.assertEqual(r.returncode, 1)
            self.assertEqual(r.stdout, "")
            self.assertTrue(r.stderr.strip().splitlines()[-1].startswith("エラー: 入力の音声がありません"))
            self.assertFalse((Path(t) / "out").exists())

    def test_help_works_without_heavy_packages(self):
        for name in ("analyze.py", "separate.py"):
            r = self.run_script(name, "--help")
            self.assertEqual(r.returncode, 0)
            self.assertIn("usage", r.stdout)


# ---- 統合試験: 本物の道具用の venv(setup --mv)があるときだけ ----

def synth(path: Path, seconds: int, rate: int, kind: str) -> Path:
    """kind="song": 一定の和音 + 0.5 秒ごとのクリック。kind="vocals": 440Hz を 0〜8 秒と 11〜20 秒だけ鳴らす(あとは無音)。"""
    def gate(t: float) -> float:
        for lo, hi in ((0.0, 8.0), (11.0, 20.0)):
            if lo <= t < hi:
                return min(1.0, (t - lo) / 0.05, (hi - t) / 0.05)
        return 0.0

    samples = []
    for i in range(seconds * rate):
        t = i / rate
        if kind == "vocals":
            v = 0.3 * math.sin(2 * math.pi * 440 * t) * gate(t)
        else:
            beat = t % 0.5
            click = 0.5 * math.sin(2 * math.pi * 1000 * t) * math.exp(-beat * 60)
            v = 0.12 * math.sin(2 * math.pi * 220 * t) + 0.08 * math.sin(2 * math.pi * 330 * t) + click + 0.25 * gate(t) * math.sin(2 * math.pi * 440 * t)
        samples.append(int(max(-1.0, min(1.0, v)) * 32000))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return path


@unittest.skipUnless(REAL_TOOLS, "道具用の venv が無い(setup --mv で作る)")
class RealAnalyzeTest(unittest.IsolatedAsyncioTestCase):
    async def test_analyze_a_synthetic_song(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            song = await asyncio.to_thread(synth, t / "song.wav", 30, 22050, "song")
            vocals = await asyncio.to_thread(synth, t / "vocals.wav", 30, 22050, "vocals")
            plan = await mv_audio.analyze(song, vocals, 5, 10, 8)
            self.assertAlmostEqual(plan["duration"], 30.0, delta=0.01)
            self.assertTrue(plan["has_vocals"])
            self.assertGreater(plan["bpm"], 0)
            scenes = plan["scenes"]
            self.assertEqual(scenes[0]["start"], 0.0)
            self.assertEqual(scenes[-1]["end"], plan["duration"])
            for a, b in zip(scenes, scenes[1:]):
                self.assertEqual(a["end"], b["start"])
            for s in scenes:
                self.assertGreaterEqual(s["duration"], 5.0 - 1e-6)
                self.assertLessEqual(s["duration"], 10.0 + 1e-6)
                self.assertIn(s["kind"], ("vocal", "instrumental"))
                if s["end"] <= 8.0:
                    self.assertEqual(s["kind"], "vocal", s)            # 歌っている区間の中
                if s["start"] >= 21.0:
                    self.assertEqual(s["kind"], "instrumental", s)     # 無音の区間の中
            self.assertEqual(plan["scenes"][0]["kind"], "vocal")

    async def test_a_song_without_vocals(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            song = await asyncio.to_thread(synth, t / "song.wav", 20, 22050, "song")
            silent = t / "vocals.wav"
            with wave.open(str(silent), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(22050)
                w.writeframes(b"\x00\x00" * 22050 * 20)
            plan = await mv_audio.analyze(song, silent, 5, 10, 8)
            self.assertFalse(plan["has_vocals"])
            self.assertTrue(all(s["kind"] == "instrumental" and s["vocal_ratio"] == 0.0 for s in plan["scenes"]))

    async def test_missing_input_surfaces_the_script_error(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(mv_audio.MvAudioError) as cm:
                await mv_audio.analyze(Path(t) / "nope.wav", Path(t) / "nope2.wav")
            self.assertIn("曲の音声がありません", cm.exception.message)


if __name__ == "__main__":
    unittest.main()
