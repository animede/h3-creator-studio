"""編集タブの書き出しの AI 拡大(studio/upscale.py と /api/upscale/tools)。子プロセスは Python の偽物、道具のファイルは空のファイル。"""
import asyncio
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from studio import config, gpu, i18n, main, mvinstall, upscale
from studio.handlers import edit as eh
from studio.jobs import Job, JobCancelled, JobFailed
from tests.test_mvinstall import FakeProc

JP_RE = r"[぀-ヿ一-鿿]"


def fake_status(realesrgan=True, rtxvsr=False, hint_rtx="GPU の条件", hint_re="未導入") -> dict:
    """status() の結果の偽物(テストが resolve に渡す)。"""
    def m(mid, usable, hint):
        d = upscale.METHODS[mid]
        return {"id": mid, "label": d["ja"], "label_en": d["en"], "scales": list(d["scales"]), "installed": usable, "usable": usable,
                "installable": not usable, "hint": None if usable else hint, "license_url": d["license_url"]}
    usable = [x for x in upscale.AUTO_ORDER if {"realesrgan": realesrgan, "rtxvsr": rtxvsr}[x]]
    return {"methods": [m("realesrgan", realesrgan, hint_re), m("rtxvsr", rtxvsr, hint_rtx)], "default": "auto", "auto_method": usable[0] if usable else None}


class FakeSettings:
    def __init__(self, root, **up):
        self.root = Path(root)
        self.components_dir = "components"
        self.upscale = {**config.UPSCALE_DEFAULTS, **up}


class ResolveTest(unittest.TestCase):
    def test_auto_prefers_rtxvsr_then_realesrgan(self):
        both = fake_status(realesrgan=True, rtxvsr=True)
        self.assertEqual(upscale.resolve(None, "auto", 2, 1280, 720, 3840, st=both), {"method": "rtxvsr", "scale": 2, "width": 2560, "height": 1440})
        only_re = fake_status(realesrgan=True, rtxvsr=False)
        self.assertEqual(upscale.resolve(None, "auto", "3", 640, 360, 3840, st=only_re)["method"], "realesrgan")
        self.assertEqual(upscale.resolve(None, "realesrgan", 4, 640, 360, 3840, st=both), {"method": "realesrgan", "scale": 4, "width": 2560, "height": 1440})

    def test_nothing_installed(self):
        with self.assertRaisesRegex(ValueError, "拡大の方式が入っていません。拡大の欄の「入れる」で Real-ESRGAN か RTX Video Super Resolution"):
            upscale.resolve(None, "auto", 2, 1280, 720, 3840, st=fake_status(False, False))

    def test_unusable_method_reports_its_hint(self):
        with self.assertRaisesRegex(ValueError, "^GPU の条件$"):
            upscale.resolve(None, "rtxvsr", 2, 1280, 720, 3840, st=fake_status(True, False))
        with self.assertRaisesRegex(ValueError, "拡大の方式が不明です: waifu"):
            upscale.resolve(None, "waifu", 2, 1280, 720, 3840, st=fake_status())

    def test_bad_scale_and_max_edge(self):
        st = fake_status()
        for bad in (5, 0, -2, "x", 2.5):
            with self.assertRaisesRegex(ValueError, "倍率"):
                upscale.resolve(None, "realesrgan", bad, 1280, 720, 3840, st=st)
        with self.assertRaisesRegex(ValueError, r"拡大後の長辺が 3840px を超えます\(5120×2880\)。大きさを下げてください"):
            upscale.resolve(None, "realesrgan", 4, 1280, 720, 3840, st=st)
        self.assertEqual(upscale.resolve(None, "realesrgan", 3, 1280, 720, 3840, st=st)["width"], 3840)       # ちょうどは通る
        self.assertEqual(upscale.resolve(FakeSettings("/x", max_edge=2000), "realesrgan", 2, 1000, 500, None, st=st)["width"], 2000)   # 上限は設定から
        with self.assertRaisesRegex(ValueError, "2000px"):
            upscale.resolve(FakeSettings("/x", max_edge=2000), "realesrgan", 3, 1000, 500, None, st=st)

    def test_scale_one_or_no_method_means_no_upscale(self):
        for method, scale in ((None, 2), ("", 2), ("auto", 1), ("realesrgan", "1"), ("auto", None), ("auto", "")):
            self.assertIsNone(upscale.resolve(None, method, scale, 1280, 720, 3840, st=fake_status(False, False)), (method, scale))

    def test_check_params(self):
        state = {"width": 640, "height": 360}
        self.assertEqual(upscale.check_params({"method": "RTXVSR", "scale": "2"}, state), {"method": "rtxvsr", "scale": 2, "width": 1280, "height": 720})
        for bad in ("x", {"method": "nope", "scale": 2}, {"method": "realesrgan", "scale": 7}, {"method": "realesrgan", "scale": "a"}):
            with self.assertRaises(ValueError):
                upscale.check_params(bad, state)


class GpuOkTest(unittest.TestCase):
    def test_compute_capability_and_driver(self):
        ok = upscale.rtxvsr_gpu_ok
        for cap in ("7.5", "8.9", "12.0"):
            self.assertEqual(ok(["NVIDIA GeForce RTX 4090", "580.178.04", cap], "linux"), (True, None), cap)
        res = ok(["NVIDIA TITAN V", "580.178.04", "7.0"], "linux")
        self.assertFalse(res[0])
        self.assertIn("NVIDIA TITAN V は RTX Video Super Resolution に対応していません", res[1])
        res = ok(["NVIDIA GeForce RTX 3060", "560.35.03", "8.6"], "linux")
        self.assertFalse(res[0])
        self.assertIn("ドライバが古いです(560.35.03)", res[1])
        self.assertIn("570.190 以降", res[1])
        # Windows と Linux で要るドライバが違う
        self.assertEqual(ok(["RTX 4060", "570.86", "8.9"], "win32"), (True, None))
        self.assertFalse(ok(["RTX 4060", "570.86.15", "8.9"], "linux")[0])
        self.assertIn("570.65 以降", ok(["RTX 4060", "566.36", "8.9"], "win32")[1])
        self.assertTrue(ok(["RTX 4060", "570.65", "8.9"], "win32")[0])
        self.assertTrue(ok(["RTX 4060", "570.190", "8.9"], "linux")[0])

    def test_missing_or_unreadable(self):
        self.assertIn("nvidia-smi", upscale.rtxvsr_gpu_ok(None, "linux")[1])
        self.assertIn("nvidia-smi", upscale.rtxvsr_gpu_ok(["x"], "linux")[1])
        self.assertIn("compute capability", upscale.rtxvsr_gpu_ok(["RTX", "580.1", "[N/A]"], "linux")[1])

    def test_smi_fields_parses_the_first_row_and_tolerates_commas_in_names(self):
        done = types.SimpleNamespace(returncode=0, stdout="NVIDIA RTX PRO 5000, Blackwell, 580.178.04, 12.0\n")
        with mock.patch.object(gpu, "find_nvidia_smi", return_value="/usr/bin/nvidia-smi"), mock.patch.object(gpu.subprocess, "run", return_value=done) as run:
            self.assertEqual(gpu.smi_fields(["name", "driver_version", "compute_cap"]), ["NVIDIA RTX PRO 5000, Blackwell", "580.178.04", "12.0"])
            self.assertIn("--query-gpu=name,driver_version,compute_cap", run.call_args.args[0])
        with mock.patch.object(gpu, "find_nvidia_smi", return_value=None):
            self.assertIsNone(gpu.smi_fields(["name"]))
        bad = types.SimpleNamespace(returncode=6, stdout="")
        with mock.patch.object(gpu, "find_nvidia_smi", return_value="/usr/bin/nvidia-smi"), mock.patch.object(gpu.subprocess, "run", return_value=bad):
            self.assertIsNone(gpu.smi_fields(["compute_cap"]))


class StatusTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.settings = FakeSettings(self.root)
        self.good = lambda: ["NVIDIA GeForce RTX 4070", "580.178.04", "8.9"]

    def install(self, method):
        """道具が入った体にする(Linux と Windows の両方の置き場所に空のファイル)。"""
        for plat in ("linux", "win32"):
            p = upscale.realesrgan_exe(self.settings, plat) if method == "realesrgan" else upscale.rtxvsr_python(self.settings, plat)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"")

    def test_paths_follow_the_settings(self):
        self.assertEqual(upscale.tools_dir(self.settings), self.root / "components" / "upscale")
        self.assertEqual(upscale.realesrgan_exe(self.settings, "linux"), self.root / "components/upscale/realesrgan/realesrgan-ncnn-vulkan")
        self.assertEqual(upscale.realesrgan_exe(self.settings, "win32").name, "realesrgan-ncnn-vulkan.exe")
        self.assertEqual(upscale.rtxvsr_python(self.settings, "linux"), self.root / "components/upscale/rtxvsr/.venv/bin/python")
        self.assertEqual(upscale.rtxvsr_python(self.settings, "win32"), self.root / "components/upscale/rtxvsr/.venv/Scripts/python.exe")
        self.assertEqual(upscale.tools_dir(FakeSettings(self.root, tools_dir="/abs/tools")), Path("/abs/tools"))
        self.assertEqual((upscale.max_edge(self.settings), upscale.rtxvsr_quality(FakeSettings(self.root, rtxvsr_quality="high"))), (3840, "HIGH"))
        self.assertTrue(upscale.RTXVSR_SCRIPT.is_file())
        self.assertEqual(set(upscale.REALESRGAN_RELEASE), {"linux", "win32"})

    def test_nothing_installed(self):
        st = upscale.status(self.settings, smi=self.good, plat="linux")
        self.assertEqual((st["default"], st["auto_method"]), ("auto", None))
        self.assertEqual([m["id"] for m in st["methods"]], ["realesrgan", "rtxvsr"])
        re_, rtx = st["methods"]
        self.assertEqual(set(re_), {"id", "label", "label_en", "scales", "installed", "usable", "installable", "hint", "license_url"})
        self.assertEqual((re_["label"], re_["label_en"], re_["scales"], re_["installed"], re_["usable"], re_["installable"], re_["license_url"]),
                         ("Real-ESRGAN(アニメ向け)", "Real-ESRGAN (anime)", [2, 3, 4], False, False, True, None))
        self.assertEqual(re_["hint"], "Real-ESRGAN(アニメ向け)が入っていません。拡大の欄の「入れる」で入れてください")
        self.assertEqual((rtx["installed"], rtx["usable"], rtx["installable"]), (False, False, True))
        self.assertIn("NVIDIA の利用条件への同意が要ります", rtx["hint"])
        self.assertEqual(rtx["license_url"], upscale.NVIDIA_LICENSE_URL)

    def test_installed_and_gpu_conditions(self):
        self.install("realesrgan")
        self.install("rtxvsr")
        st = upscale.status(self.settings, smi=self.good, plat="linux")
        self.assertEqual([(m["installed"], m["usable"], m["hint"]) for m in st["methods"]], [(True, True, None), (True, True, None)])
        self.assertEqual(st["auto_method"], "rtxvsr")
        for fields in (["GTX 1660", "580.1", "7.5"], ["RTX 4090", "580.1", "8.9"], ["RTX PRO 5000", "580.178.04", "12.0"]):
            self.assertTrue(upscale.status(self.settings, smi=lambda: fields, plat="linux")["methods"][1]["usable"], fields)
        for fields, needle in ((["TITAN V", "580.1", "7.0"], "対応していません"), (["RTX 3060", "560.35.03", "8.6"], "ドライバが古い"), (None, "nvidia-smi")):
            st = upscale.status(self.settings, smi=lambda: fields, plat="linux")
            rtx = st["methods"][1]
            self.assertEqual((rtx["installed"], rtx["usable"], rtx["installable"]), (True, False, False), fields)
            self.assertIn(needle, rtx["hint"])
            self.assertEqual(st["auto_method"], "realesrgan")
        # 対応しない GPU では、入っていなくても「入れる」を出さない(入れても使えない)
        rtx = upscale.status(FakeSettings(self.root / "other"), smi=lambda: ["TITAN V", "580.1", "7.0"], plat="linux")["methods"][1]
        self.assertEqual((rtx["installed"], rtx["installable"]), (False, False))
        # Windows はドライバの条件が緩い
        self.assertTrue(upscale.status(self.settings, smi=lambda: ["RTX 4060", "570.86", "8.9"], plat="win32")["methods"][1]["usable"])
        self.assertFalse(upscale.status(self.settings, smi=lambda: ["RTX 4060", "570.86", "8.9"], plat="linux")["methods"][1]["usable"])

    def test_realesrgan_folder_without_executable(self):
        upscale.realesrgan_dir(self.settings).mkdir(parents=True)
        re_ = upscale.status(self.settings, smi=self.good, plat="linux")["methods"][0]
        self.assertEqual((re_["installed"], re_["usable"], re_["installable"]), (False, False, True))
        self.assertIn("実行ファイル(Vulkan)が見つかりません", re_["hint"])

    def test_hints_have_english(self):
        self.install("rtxvsr")
        for fields in (self.good(), ["TITAN V", "580.1", "7.0"], ["RTX 3060", "560.35.03", "8.6"], None, ["RTX", "580.1", "[N/A]"]):
            for m in upscale.status(FakeSettings(self.root / "none"), smi=lambda: fields, plat="linux")["methods"]:
                if m["hint"]:
                    self.assertNotRegex(i18n.tr(m["hint"], "en"), JP_RE, m["hint"])
        self.assertNotRegex(i18n.tr(upscale.MSG_WAIT_GPU, "en"), JP_RE)


class HelpersTest(unittest.TestCase):
    def test_argv_builders(self):
        argv = upscale.realesrgan_argv("/t/realesrgan/realesrgan-ncnn-vulkan", "/w/in", "/w/out", 2)
        self.assertEqual(argv, ["/t/realesrgan/realesrgan-ncnn-vulkan", "-i", "/w/in", "-o", "/w/out", "-n", "realesr-animevideov3", "-s", "2", "-f", "png",
                                "-m", "/t/realesrgan/models"])
        argv = upscale.rtxvsr_argv("/t/.venv/bin/python", "/v/src.mp4", "/w/video_only.mp4", 3)
        self.assertEqual(argv, ["/t/.venv/bin/python", str(upscale.RTXVSR_SCRIPT), "/v/src.mp4", "/w/video_only.mp4", "--scale", "3", "--quality", "ULTRA"])
        self.assertEqual(upscale.rtxvsr_argv("py", "s", "t", 4, "HIGH")[-1], "HIGH")
        ex = upscale.extract_args("ffmpeg", "/v/src.mp4", "/w/in")
        self.assertEqual((ex[0], ex[ex.index("-i") + 1], ex[-1]), ("ffmpeg", "/v/src.mp4", str(Path("/w/in") / "%06d.png")))
        self.assertIn("-progress", ex)
        enc = upscale.encode_args("ffmpeg", "/w/out", "/v/src.mp4", "/v/dst.mp4.part", 29.97)
        self.assertEqual(enc[enc.index("-framerate") + 1], "29.97")
        self.assertEqual(enc[enc.index("-i") + 1], str(Path("/w/out") / "%06d.png"))
        self.assertEqual(enc[enc.index("-i", enc.index("-i") + 1) + 1], "/v/src.mp4")
        for flag, value in (("-map", "0:v"), ("-c:v", "libx264"), ("-crf", "14"), ("-preset", "medium"), ("-pix_fmt", "yuv420p"), ("-c:a", "copy"), ("-f", "mp4")):
            self.assertIn(value, enc[enc.index(flag):enc.index(flag) + 3], flag)
        self.assertIn("1:a?", enc)
        self.assertIn("-shortest", enc)
        self.assertEqual(enc[-1], "/v/dst.mp4.part")
        self.assertEqual(upscale.encode_args("f", "o", "s", "d", 24)[upscale.encode_args("f", "o", "s", "d", 24).index("-framerate") + 1], "24")
        mux = upscale.mux_args("ffmpeg", "/w/video_only.mp4", "/v/src.mp4", "/v/dst.mp4.part")
        self.assertEqual((mux[mux.index("-i") + 1], mux[mux.index("-c") + 1], mux[-1]), ("/w/video_only.mp4", "copy", "/v/dst.mp4.part"))
        self.assertIn("1:a?", mux)
        self.assertEqual(mux[mux.index("-f") + 1], "mp4")

    def test_parse_progress_and_frames(self):
        self.assertEqual(upscale.parse_progress("progress 12/124"), (12, 124))
        self.assertEqual(upscale.parse_progress("progress 124/124\n"), (124, 124))
        for line in ("done", "progress", "progress x/y", "encoder h264_nvenc", "", None):
            self.assertIsNone(upscale.parse_progress(line), line)
        self.assertEqual(upscale.ffmpeg_frame("frame=42\n"), 42)
        for line in ("frame=N/A", "out_time=1", "", "fps=12"):
            self.assertIsNone(upscale.ffmpeg_frame(line), line)

    def test_upscaled_path_numbering(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "edit_abc.mp4"
            self.assertEqual(upscale.upscaled_path(dest, 2), Path(tmp) / "edit_abc_x2.mp4")
            (Path(tmp) / "edit_abc_x2.mp4").write_bytes(b"x")
            self.assertEqual(upscale.upscaled_path(dest, 2), Path(tmp) / "edit_abc_x2_2.mp4")
            (Path(tmp) / "edit_abc_x2_2.mp4").write_bytes(b"x")
            self.assertEqual(upscale.upscaled_path(dest, 2), Path(tmp) / "edit_abc_x2_3.mp4")
            self.assertEqual(upscale.upscaled_path(dest, 4), Path(tmp) / "edit_abc_x4.mp4")
            self.assertEqual(upscale.upscaled_path(Path(tmp) / "edit_abc_2.mp4", 3), Path(tmp) / "edit_abc_2_x3.mp4")

    def test_gpu_busy(self):
        self.assertFalse(upscale.gpu_busy(types.SimpleNamespace(sched=None)))
        self.assertFalse(upscale.gpu_busy(types.SimpleNamespace(sched=types.SimpleNamespace())))
        self.assertFalse(upscale.gpu_busy(types.SimpleNamespace(sched=types.SimpleNamespace(_busy={}))))
        self.assertFalse(upscale.gpu_busy(types.SimpleNamespace(sched=types.SimpleNamespace(_busy={"h3": 0}))))
        self.assertTrue(upscale.gpu_busy(types.SimpleNamespace(sched=types.SimpleNamespace(_busy={"h3": 1}))))


class RunTest(unittest.TestCase):
    """本物の _run を、子プロセスの代わりの Python で動かす(進み・失敗・中止)。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        p = mock.patch.object(upscale, "POLL_S", 0.05)
        p.start()
        self.addCleanup(p.stop)

    def script(self, body: str) -> list:
        return [sys.executable, "-c", "import sys, time\n" + body]

    def job(self) -> Job:
        return Job(id="ju", tab="edit", kind="edit.export", params={})

    def test_stdout_lines_tick_and_stderr_tail(self):
        lines, ticks, job, procs = [], [], self.job(), {}
        body = "print('progress 1/4', flush=True)\nprint('warn', file=sys.stderr, flush=True)\ntime.sleep(0.2)\nprint('progress 4/4', flush=True)\nprint('last', file=sys.stderr)\n"
        code, tail = upscale._run(job, self.script(body), procs, on_line=lines.append, tick=lambda: ticks.append(1))
        self.assertEqual((code, lines, tail), (0, ["progress 1/4", "progress 4/4"], "warn\nlast"))
        self.assertGreaterEqual(len(ticks), 2)
        self.assertEqual(procs, {})

    def test_failure_code_and_tail_and_missing_executable(self):
        code, tail = upscale._run(self.job(), self.script("print('bad thing', file=sys.stderr)\nsys.exit(3)\n"), {})
        self.assertEqual((code, tail), (3, "bad thing"))
        with self.assertRaisesRegex(JobFailed, "拡大に失敗しました: ffmpeg-xyz を起動できません"):
            upscale._run(self.job(), ["/nonexistent/ffmpeg-xyz"], {})
        with self.assertRaises(JobFailed) as cm:
            upscale._check(3, "a\nlast line", self.dir / "nope.mp4", "Real-ESRGAN(アニメ向け)")
        self.assertEqual((cm.exception.message, cm.exception.log_tail), ("拡大に失敗しました(Real-ESRGAN(アニメ向け)): last line", "a\nlast line"))
        empty = self.dir / "empty.mp4"
        empty.write_bytes(b"")
        with self.assertRaisesRegex(JobFailed, "終了コード 0"):
            upscale._check(0, "", empty, "x")

    def test_cancel_kills_the_registered_process(self):
        job, result = self.job(), {}

        def run():
            try:
                upscale._run(job, self.script("print('progress 1/9', flush=True)\ntime.sleep(60)\n"), eh._procs)
            except Exception as e:  # noqa: BLE001
                result["error"] = e
        t = threading.Thread(target=run)
        t.start()
        deadline = time.monotonic() + 10
        while job.id not in eh._procs and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIn(job.id, eh._procs)
        job.cancel_requested = True
        asyncio.run(eh.cancel_ffmpeg(job, None))               # 編集の既存の中止の口が、同じ _procs から止める
        t.join(15)
        self.assertFalse(t.is_alive())
        self.assertIsInstance(result.get("error"), JobCancelled)
        self.assertEqual(eh._procs, {})

    def test_cancel_before_start(self):
        job = self.job()
        job.cancel_requested = True
        with self.assertRaises(JobCancelled):
            upscale._run(job, self.script("time.sleep(60)\n"), {})

    def test_stall_is_stopped(self):
        with mock.patch.object(upscale, "STALL_S", 0.2):
            with self.assertRaisesRegex(JobFailed, "進まないので止めました"):
                upscale._run(self.job(), self.script("time.sleep(60)\n"), {})


class FakeRun:
    """upscale._run の代わり: 段ごとに、ffmpeg なら出力のファイルを書き、Real-ESRGAN ならコマを拡大した体で out に写し、道具の Python なら進みの行を流す。"""

    def __init__(self, frames=5, fail_at=None, short=False, rc_tail="boom"):
        self.frames, self.fail_at, self.short, self.rc_tail = frames, fail_at, short, rc_tail
        self.calls, self.messages = [], []

    def __call__(self, job, argv, procs, on_line=None, tick=None, env=None):
        self.calls.append(list(argv))
        step = len(self.calls)
        if self.fail_at == step:
            return 1, self.rc_tail
        if argv[0] == "ffmpeg" and "%06d.png" in argv[-1]:                                 # コマの取り出し
            Path(argv[-1]).parent.mkdir(parents=True, exist_ok=True)
            for n in range(1, self.frames + 1):
                (Path(argv[-1]).parent / f"{n:06d}.png").write_bytes(b"png")
        elif argv[0] == "ffmpeg":                                                             # 動画の作成・音声の結合
            if on_line is not None:
                on_line("frame=2")
                self.messages.append(job.message)
            Path(argv[-1]).write_bytes(b"mp4")
        elif argv[0].endswith("realesrgan-ncnn-vulkan"):
            out = Path(argv[argv.index("-o") + 1])
            for n in range(1, self.frames + (0 if not self.short else -2) + 1):
                (out / f"{n:06d}.png").write_bytes(b"big")
                tick()
                self.messages.append(job.message)
        else:                                                                                 # 道具の Python
            on_line("encoder h264_nvenc")
            on_line(f"progress 3/{self.frames}")
            self.messages.append(job.message)
            Path(argv[3]).write_bytes(b"video")
        return 0, ""


class RunnerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.src = self.root / "video" / "edit_p.mp4"
        self.src.parent.mkdir()
        self.src.write_bytes(b"src")
        self.dst = self.root / "video" / "edit_p_x2.mp4"
        self.work = self.root / "edit" / "p" / "upscale"
        self.job = Job(id="jr", tab="edit", kind="edit.export", params={})

    def test_realesrgan_pipeline(self):
        fake = FakeRun(frames=4)
        with mock.patch.object(upscale, "_run", fake):
            out = upscale.run_realesrgan(self.job, "/t/realesrgan-ncnn-vulkan", self.src, self.dst, 2, 24, self.work, "ffmpeg", {})
        self.assertEqual((out, self.dst.read_bytes()), (self.dst, b"mp4"))
        self.assertEqual([c[0] for c in fake.calls], ["ffmpeg", "/t/realesrgan-ncnn-vulkan", "ffmpeg"])
        self.assertEqual(fake.calls[0][-1], str(self.work / "in" / "%06d.png"))
        self.assertEqual(fake.calls[1][fake.calls[1].index("-s") + 1], "2")
        self.assertEqual(fake.calls[2][-1], str(self.dst) + ".part")
        self.assertEqual(fake.messages[:4], [f"拡大しています(Real-ESRGAN(アニメ向け) ×2) {p}%" for p in (25, 50, 75, 100)])
        self.assertEqual(fake.messages[4], "拡大した動画を作っています 50%")
        self.assertFalse(self.work.exists())                                                 # コマのフォルダは消す
        self.assertFalse((self.root / "video" / "edit_p_x2.mp4.part").exists())
        self.assertEqual(self.job.progress, 0.945)                                                # 動画の作成が frame=2/4 まで進んだ所(0.9〜0.99)

    def test_realesrgan_failures_clean_up(self):
        for fail_at, needle in ((1, "コマの取り出し、ffmpeg"), (2, "Real-ESRGAN(アニメ向け)"), (3, "動画の作成、ffmpeg")):
            fake = FakeRun(frames=3, fail_at=fail_at, rc_tail="line1\nthe reason")
            with mock.patch.object(upscale, "_run", fake), self.assertRaises(JobFailed) as cm:
                upscale.run_realesrgan(self.job, "/t/realesrgan-ncnn-vulkan", self.src, self.dst, 2, 24, self.work, "ffmpeg", {})
            self.assertEqual(cm.exception.message, f"拡大に失敗しました({needle}): the reason", fail_at)
            self.assertEqual(cm.exception.log_tail, "line1\nthe reason")
            self.assertFalse(self.work.exists())
            self.assertFalse(self.dst.exists())
            self.assertFalse((self.root / "video" / "edit_p_x2.mp4.part").exists())
        with mock.patch.object(upscale, "_run", FakeRun(frames=4, short=True)), self.assertRaisesRegex(JobFailed, r"拡大できたコマが足りません\(2/4\)"):
            upscale.run_realesrgan(self.job, "/t/realesrgan-ncnn-vulkan", self.src, self.dst, 2, 24, self.work, "ffmpeg", {})
        with mock.patch.object(upscale, "_run", FakeRun(frames=0)), self.assertRaisesRegex(JobFailed, "コマを取り出せませんでした"):
            upscale.run_realesrgan(self.job, "/t/realesrgan-ncnn-vulkan", self.src, self.dst, 2, 24, self.work, "ffmpeg", {})

    def test_rtxvsr_pipeline(self):
        fake = FakeRun(frames=6)
        with mock.patch.object(upscale, "_run", fake):
            out = upscale.run_rtxvsr(self.job, "/t/.venv/bin/python", self.src, self.dst, 3, 24, self.work, "ffmpeg", {}, "HIGH")
        self.assertEqual((out, self.dst.read_bytes()), (self.dst, b"mp4"))
        py, mux = fake.calls
        self.assertEqual(py[:4], ["/t/.venv/bin/python", str(upscale.RTXVSR_SCRIPT), str(self.src), str(self.work / "video_only.mp4")])
        self.assertEqual(py[py.index("--quality") + 1], "HIGH")
        self.assertEqual((mux[0], mux[mux.index("-i") + 1], mux[-1]), ("ffmpeg", str(self.work / "video_only.mp4"), str(self.dst) + ".part"))
        self.assertEqual(fake.messages[0], "拡大しています(RTX Video Super Resolution ×3) 50%")
        self.assertFalse(self.work.exists())
        self.dst.unlink()
        fake = FakeRun(frames=6, fail_at=1, rc_tail="error: CUDA の GPU が見えません")
        with mock.patch.object(upscale, "_run", fake), self.assertRaises(JobFailed) as cm:
            upscale.run_rtxvsr(self.job, "/t/.venv/bin/python", self.src, self.dst, 2, 24, self.work, "ffmpeg", {})
        self.assertEqual(cm.exception.message, "拡大に失敗しました(RTX Video Super Resolution): error: CUDA の GPU が見えません")
        self.assertFalse(self.dst.exists())
        self.assertFalse(self.work.exists())


class ToolsApiTest(unittest.TestCase):
    """GET /api/upscale/tools と POST /api/upscale/tools/install(導入の子プロセスは偽物)。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.procs = []

        def popen(argv, **kw):
            p = FakeProc()
            self.procs.append(p)
            self.argv = argv
            return p
        saved = getattr(main.app.state, "installers", None)
        self.addCleanup(lambda: setattr(main.app.state, "installers", saved))
        main.app.state.installers = {}
        for mid, m in upscale.METHODS.items():
            main.app.state.installers[m["target"]] = mvinstall.ToolsInstaller(m["target"], m["install_args"], popen=popen, log_path=Path(tmp.name) / f"{mid}.log")
        self.addCleanup(lambda: [p.release() for p in self.procs])
        p = mock.patch.object(upscale, "status", return_value=fake_status(realesrgan=False, rtxvsr=False))
        p.start()
        self.addCleanup(p.stop)
        self.client = TestClient(main.app)

    def test_get_shape(self):
        js = self.client.get("/api/upscale/tools").json()
        self.assertEqual(set(js), {"methods", "default", "auto_method", "installing", "progress_text", "error", "max_edge"})
        self.assertEqual((js["default"], js["auto_method"], js["installing"], js["progress_text"], js["error"]), ("auto", None, None, None, None))
        self.assertEqual(js["max_edge"], upscale.max_edge(main.settings))
        self.assertEqual([m["id"] for m in js["methods"]], ["realesrgan", "rtxvsr"])
        self.assertEqual(set(js["methods"][0]), {"id", "label", "label_en", "scales", "installed", "usable", "installable", "hint", "license_url"})
        en = self.client.get("/api/upscale/tools", headers={"Accept-Language": "en"}).json()
        self.assertEqual(en["methods"][0]["label"], "Real-ESRGAN (anime)")                       # label は訳す項目

    def test_install_validation(self):
        r = self.client.post("/api/upscale/tools/install", json={"method": "waifu"})
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "拡大の方式が不明です: waifu"))
        r = self.client.post("/api/upscale/tools/install", json={"method": "rtxvsr"})
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "NVIDIA の利用条件への同意が必要です"))
        r = self.client.post("/api/upscale/tools/install", json={"method": "rtxvsr", "accept_license": False}, headers={"Accept-Language": "en"})
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "You must accept NVIDIA's license terms"))
        self.assertEqual(self.procs, [])

    def test_install_realesrgan_then_conflict_then_done(self):
        r = self.client.post("/api/upscale/tools/install", json={"method": "realesrgan"})
        self.assertEqual(r.status_code, 202, r.text)
        js = r.json()
        self.assertEqual((js["installing"], js["error"]), ("realesrgan", None))
        self.assertEqual(self.argv[1:], [str(mvinstall.SETUP_PY), "--only", "upscale-realesrgan", "--no-models"])
        r = self.client.post("/api/upscale/tools/install", json={"method": "rtxvsr", "accept_license": True})      # 別の方式でも、導入中なら 409
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["detail"], "拡大の道具(Real-ESRGAN)を入れている最中です。終わるまでお待ちください")
        r = self.client.post("/api/upscale/tools/install", json={"method": "realesrgan"}, headers={"Accept-Language": "en"})
        self.assertEqual((r.status_code, r.json()["detail"]), (409, "Installing the upscaling tool (Real-ESRGAN). Please wait until it finishes"))
        for _ in range(100):
            if self.client.get("/api/upscale/tools").json()["progress_text"] == "done":
                break
            time.sleep(0.01)
        self.assertEqual(self.client.get("/api/upscale/tools").json()["installing"], "realesrgan")
        self.procs[0].release()
        main.app.state.installers["upscale-realesrgan"]._thread.join(5)
        js = self.client.get("/api/upscale/tools").json()
        self.assertEqual((js["installing"], js["progress_text"], js["error"]), (None, None, None))

    def test_install_rtxvsr_passes_the_license_flag_and_reports_failure(self):
        r = self.client.post("/api/upscale/tools/install", json={"method": "rtxvsr", "accept_license": True})
        self.assertEqual((r.status_code, r.json()["installing"]), (202, "rtxvsr"))
        self.assertEqual(self.argv[1:], [str(mvinstall.SETUP_PY), "--only", "upscale-rtxvsr", "--no-models", "--accept-nvidia-license"])
        self.procs[0].rc = 2
        self.procs[0].release()
        main.app.state.installers["upscale-rtxvsr"]._thread.join(5)
        js = self.client.get("/api/upscale/tools").json()
        self.assertEqual((js["installing"], js["error"]),
                         (None, "拡大の道具(RTX Video Super Resolution)を入れられませんでした(終了コード 2)。logs/upscale-rtxvsr-install.log を見てください"))
        en = self.client.get("/api/upscale/tools", headers={"Accept-Language": "en"}).json()
        self.assertEqual(en["error"], "Could not install the upscaling tool (RTX Video Super Resolution) (exit code 2). See logs/upscale-rtxvsr-install.log")


if __name__ == "__main__":
    unittest.main()
