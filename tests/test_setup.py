import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from studio import mv_audio
from studio.components import ComponentNotInstalled, get_spec
from studio.config import Settings

_path = Path(__file__).resolve().parent.parent / "scripts" / "setup.py"
_spec = importlib.util.spec_from_file_location("studio_setup_script", _path)
S = importlib.util.module_from_spec(_spec)
sys.modules["studio_setup_script"] = S
_spec.loader.exec_module(S)


def touch(p: Path, text: str = "x") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


class DecideSourceTest(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.root = Path(self._t.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.cfg = Settings({"components_dir": "components", "components": {
            "h3": {"repo": "minimax-h3"}, "aivis": {"repo": "AivisSpeech-Engine"}}}, self.root)

    def tearDown(self):
        self._t.cleanup()

    def test_configured_dir_existing_is_used_and_skipped(self):
        ext = self.root / "elsewhere"
        ext.mkdir()
        cfg = Settings({"components": {"h3": {"dir": str(ext)}}}, self.root)
        s = S.decide_source("h3", cfg, self.home)
        self.assertEqual(s.action, "existing")
        self.assertIn("既存を使います", s.message)
        self.assertIn(str(ext), s.message)
        self.assertFalse(s.run_setup)

    def test_configured_dir_missing_is_not_cloned(self):
        cfg = Settings({"components": {"h3": {"dir": str(self.root / "nope")}}}, self.root)
        s = S.decide_source("h3", cfg, self.home, clone_anyway=True)
        self.assertEqual(s.action, "missing-dir")
        self.assertFalse(s.run_setup)

    def test_already_cloned_in_components_dir(self):
        (self.root / "components" / "minimax-h3" / ".git").mkdir(parents=True)
        s = S.decide_source("h3", self.cfg, self.home)
        self.assertEqual(s.action, "present")
        self.assertTrue(s.run_setup)

    def test_clone_when_nothing_exists(self):
        s = S.decide_source("h3", self.cfg, self.home)
        self.assertEqual(s.action, "clone")
        self.assertEqual(s.path, self.root / "components" / "minimax-h3")
        self.assertIn("Diffusers_minimax-h3.git", s.message)
        self.assertTrue(s.run_setup)

    def test_home_dir_is_reported_not_cloned(self):
        (self.home / "minimax-h3").mkdir()
        s = S.decide_source("h3", self.cfg, self.home)
        self.assertEqual(s.action, "home")
        self.assertIn("~/minimax-h3 が見つかりました", s.message)
        self.assertIn("studio.local.toml に dir を書いてください", s.message)
        self.assertIn("[components.h3]", s.message)
        self.assertIn(f"dir = '{self.home / 'minimax-h3'}'", s.message)
        self.assertFalse(s.run_setup)

    def test_clone_anyway_overrides_home_detection(self):
        (self.home / "minimax-h3").mkdir()
        s = S.decide_source("h3", self.cfg, self.home, clone_anyway=True)
        self.assertEqual(s.action, "clone")

    def test_home_dir_uses_configured_repo_name(self):
        (self.home / "my-h3").mkdir()
        cfg = Settings({"components": {"h3": {"repo": "my-h3"}}}, self.root)
        self.assertEqual(S.decide_source("h3", cfg, self.home).action, "home")

    def test_aivis_download_present_and_blocked(self):
        with mock.patch.object(sys, "platform", "linux"):
            s = S.decide_source("aivis", self.cfg, self.home)
            self.assertEqual(s.action, "download")
            self.assertIn("Linux-x64-1.2.0.7z.001", s.message)
            d = self.root / "components" / "AivisSpeech-Engine"
            touch(d / "stuff.txt")
            self.assertEqual(S.decide_source("aivis", self.cfg, self.home).action, "blocked")
            touch(d / "run")
            s = S.decide_source("aivis", self.cfg, self.home)
            self.assertEqual(s.action, "present")

    def test_toml_example_uses_literal_string_for_windows_paths(self):
        ex = S.toml_dir_example("ace", Path("C:/x/ACE"))
        self.assertTrue(ex.startswith("[components.ace]\ndir = '"))


class CommandsTest(unittest.TestCase):
    def test_h3_linux_and_windows(self):
        with mock.patch.object(sys, "platform", "linux"):
            st = S.component_steps("h3")
            self.assertEqual([s.argv for s in st], [["bash", "scripts/install.sh"], ["venv/bin/python", "scripts/prepare_lowvram.py", "--ref"]])
        with mock.patch.object(sys, "platform", "win32"):
            st = S.component_steps("h3")
            self.assertEqual(st[0].argv, ["cmd", "/c", "scripts\\install.bat"])
            self.assertEqual(st[1].argv, ["venv\\Scripts\\python.exe", "scripts\\prepare_lowvram.py", "--ref"])
            self.assertEqual(S.component_steps("h3", plat="win32", h3_ref=False)[1].argv[-1], "scripts\\prepare_lowvram.py")
            self.assertEqual(st[1].env.get("HF_HUB_DISABLE_SYMLINKS"), "1")

    def test_no_models_drops_model_steps(self):
        for plat in ("linux", "win32"):
            with mock.patch.object(sys, "platform", plat):
                self.assertEqual(len(S.component_steps("h3", models=False)), 1)
                self.assertEqual(len(S.component_steps("h3image", models=False)), 2)
                self.assertIn("--no-models", S.component_steps("ace", models=False)[0].argv)

    def test_h3image_linux(self):
        with mock.patch.object(sys, "platform", "linux"):
            st = S.component_steps("h3image")
            self.assertEqual(st[0].argv, ["uv", "venv", "--python", "3.12", ".venv"])
            self.assertEqual(st[1].argv, ["uv", "pip", "install", "--python", ".venv/bin/python", "-r", "requirements.txt"])
            self.assertEqual(st[2].argv, [".venv/bin/python", "scripts/prepare_models.py"])
            self.assertEqual(len(S.component_steps("h3image", have_venv=True)), 2)

    def test_h3image_windows_uses_windows_requirements(self):
        with mock.patch.object(sys, "platform", "win32"):
            st = S.component_steps("h3image")
            self.assertEqual(st[1].argv[-3:], [".venv\\Scripts\\python.exe", "-r", "requirements-windows.txt"])
            self.assertEqual(st[2].argv, [".venv\\Scripts\\python.exe", "scripts\\prepare_models.py"])

    def test_ace(self):
        with mock.patch.object(sys, "platform", "linux"):
            self.assertEqual(S.component_steps("ace")[0].argv, ["bash", "setup.sh", "--12gb", "--no-client"])
            self.assertEqual(S.component_steps("ace", ace_xl=True)[0].argv, ["bash", "setup.sh", "--no-client"])
        with mock.patch.object(sys, "platform", "win32"):
            self.assertEqual(S.component_steps("ace")[0].argv, ["cmd", "/c", "setup.bat", "--12gb"])

    def test_aivis_has_no_steps(self):
        self.assertEqual(S.component_steps("aivis"), [])

    def test_format_command_and_resolve(self):
        self.assertEqual(S.format_command(["bash", "my dir/x.sh"], "linux"), "bash 'my dir/x.sh'")
        self.assertEqual(S.format_command(["cmd", "/c", "a b.bat"], "win32"), 'cmd /c "a b.bat"')
        self.assertEqual(S.resolve_argv(["venv/bin/python", "x.py"], Path("/r")), [str(Path("/r") / "venv/bin/python"), "x.py"])
        self.assertEqual(S.resolve_argv(["uv", "venv"], Path("/r")), ["uv", "venv"])

    def test_python312_env(self):
        run = mock.Mock(return_value=mock.Mock(returncode=0, stdout="/u/py312\n"))
        self.assertEqual(S.python312_env("linux", which=lambda n: "/x" if n == "python3.12" else None, run=run), {})
        self.assertEqual(S.python312_env("linux", which=lambda n: "/x" if n == "uv" else None, run=run), {"PYTHON": "/u/py312"})
        self.assertEqual(S.python312_env("win32", which=lambda n: None, run=run), {})


class PrerequisiteTest(unittest.TestCase):
    def test_missing_tools_hints(self):
        got = dict(S.missing_tools(lambda t: None, "win32"))
        self.assertEqual(got["git"], "winget install --id Git.Git -e --source winget")
        self.assertEqual(got["ffmpeg"], "winget install --id Gyan.FFmpeg -e --source winget")
        self.assertEqual(S.missing_tools(lambda t: "/bin/" + t, "linux"), [])
        self.assertIn("apt", dict(S.missing_tools(lambda t: None, "linux"))["git"])

    def test_parse_gpu_query(self):
        self.assertEqual(S.parse_gpu_query("NVIDIA GeForce RTX 3060, 12288\n"), ("NVIDIA GeForce RTX 3060", 12288))
        self.assertEqual(S.parse_gpu_query("NVIDIA RTX A, B, 6144"), ("NVIDIA RTX A, B", 6144))
        self.assertIsNone(S.parse_gpu_query(""))

    def test_resource_warnings(self):
        self.assertEqual(S.resource_warnings(31.5, 12288), [])
        self.assertEqual(S.resource_warnings(31.0, 6144), [])        # 6GB のカードは警告しない
        w = S.resource_warnings(16.0, 4096)
        self.assertEqual(len(w), 2)
        self.assertIn("CPU メモリ 32GB 未満では動画の準備(量子化)と音楽の同梱 LLM が厳しくなります", w[0])
        self.assertIn("VRAM", w[1])
        self.assertIsNone(S.disk_warning(500, True))
        self.assertIn("150GB", S.disk_warning(50, True))
        self.assertIn("120GB", S.disk_warning(50, True, ref=False))
        self.assertIsNone(S.disk_warning(130, True, ref=False))
        self.assertIsNone(S.disk_warning(50, False))


class AivisLayoutTest(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.root = Path(self._t.name)

    def tearDown(self):
        self._t.cleanup()

    def test_url(self):
        self.assertTrue(S.aivis_url("win32").endswith("/1.2.0/AivisSpeech-Engine-Windows-x64-1.2.0.7z.001"))
        self.assertTrue(S.aivis_url("linux").endswith("/1.2.0/AivisSpeech-Engine-Linux-x64-1.2.0.7z.001"))

    def test_flat_layout_kept(self):
        touch(self.root / "run")
        touch(self.root / "engine_internal" / "x")
        self.assertEqual(S.normalize_aivis_layout(self.root, "linux"), self.root / "run")

    def test_platform_subdir_kept(self):
        touch(self.root / "Linux-x64" / "run")
        self.assertEqual(S.normalize_aivis_layout(self.root, "linux"), self.root / "Linux-x64" / "run")

    def test_single_wrapper_dir_is_flattened(self):
        touch(self.root / "AivisSpeech-Engine-1.2.0" / "run")
        touch(self.root / "AivisSpeech-Engine-1.2.0" / "engine_internal" / "lib.so")
        exe = S.normalize_aivis_layout(self.root, "linux")
        self.assertEqual(exe, self.root / "run")
        self.assertTrue((self.root / "engine_internal" / "lib.so").is_file())
        self.assertFalse((self.root / "AivisSpeech-Engine-1.2.0").exists())

    def test_wrapper_around_platform_dir(self):
        touch(self.root / "top" / "Windows-x64" / "run.exe")
        exe = S.normalize_aivis_layout(self.root, "win32")
        self.assertEqual(exe, self.root / "Windows-x64" / "run.exe")

    def test_same_name_wrapper_does_not_collide(self):
        touch(self.root / "Linux-x64" / "Linux-x64" / "run")
        self.assertEqual(S.normalize_aivis_layout(self.root, "linux"), self.root / "Linux-x64" / "run")

    def test_deep_with_siblings_found_by_search(self):
        touch(self.root / "readme.txt")
        touch(self.root / "a" / "b" / "run")
        touch(self.root / "a" / "b" / "data" / "d")
        exe = S.normalize_aivis_layout(self.root, "linux")
        self.assertEqual(exe, self.root / "run")
        self.assertTrue((self.root / "data" / "d").is_file())

    def test_missing_exe_returns_none(self):
        touch(self.root / "x" / "y.txt")
        self.assertIsNone(S.normalize_aivis_layout(self.root, "linux"))

    def test_linux_exe_gets_exec_bit(self):
        touch(self.root / "w" / "run")
        exe = S.normalize_aivis_layout(self.root, "linux")
        self.assertTrue(exe.stat().st_mode & 0o100)

    def test_components_spec_finds_the_result(self):
        """展開後の置き場所を、管理役のエンジンカタログが実際に見つけられること。"""
        d = self.root / "AivisSpeech-Engine"
        touch(d / "AivisSpeech-Engine-Linux-x64-1.2.0" / "run")
        cfg = Settings({"components": {"aivis": {"dir": str(d)}}}, self.root)
        with mock.patch.object(sys, "platform", "linux"):
            with self.assertRaises(ComponentNotInstalled):
                get_spec("aivis", cfg).build_command(d)
            S.normalize_aivis_layout(d)
            self.assertEqual(get_spec("aivis", cfg).build_command(d)[0], str(d / "run"))

    def test_install_aivis_with_fake_fetch_and_extract(self):
        target = self.root / "components" / "AivisSpeech-Engine"
        fetched = []

        def fetch(url, dest):
            fetched.append(url)
            touch(dest, "archive")

        def extract(archive, dest):
            touch(dest / "AivisSpeech-Engine" / "run")
            touch(dest / "AivisSpeech-Engine" / "engine_internal" / "x")

        with mock.patch("platform.machine", return_value="x86_64"):
            exe = S.install_aivis(target, "linux", fetch=fetch, extract=extract)
        self.assertEqual(exe, target / "run")
        self.assertTrue(fetched[0].endswith("Linux-x64-1.2.0.7z.001"))
        self.assertFalse((self.root / "components" / "_download").exists())
        self.assertFalse((self.root / "components" / "AivisSpeech-Engine.tmp").exists())

    def test_extract_7z_single_volume_named_001(self):
        import py7zr
        src = self.root / "src"
        touch(src / "top" / "run", "bin")
        arc = self.root / "a.7z.001"
        with py7zr.SevenZipFile(arc, "w") as z:
            z.writeall(src / "top", "top")
        out = self.root / "out"
        S.extract_7z(arc, out)
        self.assertTrue((out / "top" / "run").is_file())


class StatusTest(unittest.TestCase):
    def test_component_status(self):
        with tempfile.TemporaryDirectory() as t, mock.patch.object(sys, "platform", "linux"):
            d = Path(t)
            self.assertEqual(S.component_status("h3", d), (False, False))
            touch(d / "venv" / "bin" / "python")
            touch(d / "models" / "prequant" / "transformer_int8" / "a")
            self.assertEqual(S.component_status("h3", d), (True, True))
            touch(d / ".venv" / "bin" / "python")
            self.assertEqual(S.component_status("h3image", d), (True, False))
            touch(d / "models" / "te" / "model.safetensors")
            self.assertEqual(S.component_status("h3image", d), (True, True))
            self.assertEqual(S.component_status("ace", d), (False, False))
            touch(d / "run_api_server_fast.sh")
            touch(d / "checkpoints" / "acestep-v15-turbo" / "c")
            self.assertEqual(S.component_status("ace", d), (True, True))
            self.assertEqual(S.component_status("aivis", d), (False, None))
            touch(d / "run")
            self.assertEqual(S.component_status("aivis", d), (True, None))


class ArgsTest(unittest.TestCase):
    def test_defaults_and_flags(self):
        a = S.parse_args([])
        self.assertFalse(a.no_models or a.llm or a.ace_xl or a.clone_anyway or a.mv)
        a = S.parse_args(["--no-models", "--only", "h3,ace", "--components-dir", "D:/c", "--llm"])
        self.assertTrue(a.no_models and a.llm)
        self.assertFalse(a.mv)
        self.assertEqual((a.only, a.components_dir), ("h3,ace", "D:/c"))

    def test_mv_flag(self):
        self.assertTrue(S.parse_args(["--mv"]).mv)
        a = S.parse_args(["--mv", "--only", "mvtools"])
        self.assertTrue(a.mv)
        self.assertEqual(a.only, "mvtools")


class ParseOnlyTest(unittest.TestCase):
    ENABLED = ["h3", "h3image", "ace", "aivis"]

    def test_empty_means_every_enabled_component(self):
        self.assertEqual(S.parse_only("", self.ENABLED), (self.ENABLED, False, []))
        self.assertEqual(S.parse_only("  ", self.ENABLED, True), (self.ENABLED, True, []))

    def test_components_keep_the_setup_order(self):
        self.assertEqual(S.parse_only("ace, h3", self.ENABLED), (["h3", "ace"], False, []))

    def test_mvtools_is_not_a_component(self):
        self.assertEqual(S.parse_only("mvtools", self.ENABLED), ([], True, []))
        self.assertEqual(S.parse_only("h3,mvtools", self.ENABLED), (["h3"], True, []))
        self.assertEqual(S.parse_only("h3", self.ENABLED, True), (["h3"], True, []))

    def test_unknown_and_disabled_names_are_reported(self):
        self.assertEqual(S.parse_only("zzz,mvtools", self.ENABLED), ([], True, ["zzz"]))
        self.assertEqual(S.parse_only("ace", ["h3"]), ([], False, ["ace"]))


class MvToolsTest(unittest.TestCase):
    """setup --mv: components/mvtools/.venv に CPU 版 torch と Demucs / librosa を入れる。"""

    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.root = Path(self._t.name)
        self.cfg = Settings({}, self.root)
        self.venv = self.root / "components" / "mvtools" / ".venv"
        self.py = mv_audio.venv_python(self.venv)
        self.lines, self.labels = [], []
        for target, new in (("say", mock.Mock(side_effect=self.lines.append)),
                            ("ensure_uv_on_path", mock.Mock(return_value=True)),
                            ("run_step", mock.Mock(side_effect=self.run_step))):
            p = mock.patch.object(S, target, new)
            p.start()
            self.addCleanup(p.stop)
        self.fail_label = None

    def tearDown(self):
        self._t.cleanup()

    def run_step(self, step, comp_dir):
        self.labels.append(step.label)
        return step.label != self.fail_label

    def text(self) -> str:
        return "\n".join(self.lines)

    def test_steps(self):
        steps = S.mv_steps(self.venv, self.py)
        self.assertEqual([st.argv for st in steps], [
            ["uv", "venv", "--python", "3.12", str(self.venv)],
            ["uv", "pip", "install", "--python", str(self.py), "--index-url", "https://download.pytorch.org/whl/cpu",
             "torch==2.11.0", "torchaudio==2.11.0"],
            ["uv", "pip", "install", "--python", str(self.py), "demucs==4.0.1", "soundfile", "librosa==0.11.0"]])
        self.assertEqual([st.argv for st in S.mv_steps(self.venv, self.py, have_venv=True)], [st.argv for st in steps[1:]])
        self.assertEqual(S.mv_warmup_step(self.py).argv,
                         [str(self.py), "-c", "from demucs.pretrained import get_model; get_model('htdemucs')"])

    def test_notice_gives_the_download_size_per_platform(self):
        linux, win = S.mv_notice("linux"), S.mv_notice("win32")
        self.assertIn("約 410MB のダウンロード(torch の CPU 版 約 190MB", linux)
        self.assertIn("約 315MB のダウンロード(torch の CPU 版 約 115MB", win)
        for text in (linux, win):
            self.assertIn("Demucs の重み 約 80MB", text)
            self.assertIn("約 1.3GB", text)

    def test_torch_comes_from_the_cpu_index_only(self):
        steps = S.mv_steps(self.venv, self.py)
        self.assertIn(S.MV_TORCH_INDEX, steps[1].argv)
        self.assertNotIn("--index-url", steps[2].argv)           # あとの導入は PyPI。torch は入っているものを使う
        self.assertFalse(any(a.startswith("torch") for a in steps[2].argv))

    def test_mv_ready(self):
        run = mock.Mock(return_value=mock.Mock(returncode=0))
        self.assertFalse(S.mv_ready(self.py, run))                # python が無い
        run.assert_not_called()
        touch(self.py)
        self.assertTrue(S.mv_ready(self.py, run))
        self.assertEqual(run.call_args.args[0], [str(self.py), "-c", S.MV_IMPORTS])
        self.assertIn("librosa", S.MV_IMPORTS)
        self.assertIn("demucs", S.MV_IMPORTS)
        self.assertFalse(S.mv_ready(self.py, mock.Mock(return_value=mock.Mock(returncode=1))))
        self.assertFalse(S.mv_ready(self.py, mock.Mock(side_effect=OSError("x"))))
        self.assertFalse(S.mv_ready(self.py, mock.Mock(side_effect=subprocess.TimeoutExpired("x", 1))))

    def test_fresh_install_runs_every_step_then_fetches_the_weights(self):
        with mock.patch.object(S, "mv_ready", side_effect=[False, True]):
            self.assertTrue(S.setup_mv(self.cfg))
        self.assertEqual(self.labels, ["mvtools: 仮想環境の作成(uv venv)", "mvtools: torch / torchaudio(CPU 版)の導入",
                                       "mvtools: Demucs / soundfile / librosa の導入", "mvtools: Demucs の重み(htdemucs)の取得"])
        self.assertIn("ダウンロード", self.text())                 # ダウンロードの目安を出す
        self.assertIn(S.mv_notice(), self.text())
        self.assertTrue((self.root / "components" / "mvtools").is_dir())
        self.assertEqual(S.run_step.call_args_list[0].args[1], self.root / "components" / "mvtools")

    def test_nothing_is_installed_when_demucs_and_librosa_are_already_there(self):
        with mock.patch.object(S, "mv_ready", return_value=True):
            self.assertTrue(S.setup_mv(self.cfg))
        self.assertEqual(self.labels, ["mvtools: Demucs の重み(htdemucs)の取得"])      # 重みの取得だけ(取得済みなら数秒)
        self.assertIn("準備済みです", self.text())
        S.ensure_uv_on_path.assert_not_called()

    def test_no_models_skips_the_weights(self):
        with mock.patch.object(S, "mv_ready", return_value=True):
            self.assertTrue(S.setup_mv(self.cfg, models=False))
        self.assertEqual(self.labels, [])
        self.assertIn("--no-models", self.text())

    def test_existing_venv_is_reused(self):
        touch(self.py)
        with mock.patch.object(S, "mv_ready", side_effect=[False, True]):
            self.assertTrue(S.setup_mv(self.cfg, models=False))
        self.assertNotIn("mvtools: 仮想環境の作成(uv venv)", self.labels)
        self.assertEqual(len(self.labels), 2)

    def test_a_failing_step_stops_the_setup(self):
        self.fail_label = "mvtools: torch / torchaudio(CPU 版)の導入"
        with mock.patch.object(S, "mv_ready", return_value=False):
            self.assertFalse(S.setup_mv(self.cfg))
        self.assertEqual(self.labels, ["mvtools: 仮想環境の作成(uv venv)", self.fail_label])

    def test_installed_but_not_importable_is_an_error(self):
        with mock.patch.object(S, "mv_ready", return_value=False), mock.patch.object(S, "show_mv_import_error") as show:
            self.assertFalse(S.setup_mv(self.cfg))
        self.assertIn("読み込めません", self.text())
        show.assert_called_once_with(self.py)                     # 原因のトレースバックを見せる
        self.assertNotIn("mvtools: Demucs の重み(htdemucs)の取得", self.labels)

    def test_import_error_is_shown_by_running_the_same_import(self):
        with mock.patch.object(S.subprocess, "call", return_value=1) as call:
            S.show_mv_import_error(self.py)
        call.assert_called_once_with([str(self.py), "-c", S.MV_IMPORTS])
        S.show_mv_import_error(self.root / "no" / "python")        # 実行できなくても例外にしない
        self.assertIn("実行できませんでした", self.text())

    def test_uv_missing(self):
        S.ensure_uv_on_path.return_value = False
        with mock.patch.object(S, "mv_ready", return_value=False):
            self.assertFalse(S.setup_mv(self.cfg))
        self.assertEqual(self.labels, [])
        self.assertIn("uv が見つかりません", self.text())

    def test_configured_python_is_used_and_nothing_is_installed(self):
        mine = touch(self.root / "my" / "python")
        self.cfg.mv = {"tools_python": str(mine)}
        with mock.patch.object(S, "mv_ready", return_value=True):
            self.assertTrue(S.setup_mv(self.cfg))
        self.assertEqual(self.labels, [])
        self.assertIn("既存を使います", self.text())
        self.assertIn(str(mine), self.text())
        with mock.patch.object(S, "mv_ready", return_value=False), mock.patch.object(S, "show_mv_import_error"):
            self.assertFalse(S.setup_mv(self.cfg))                # 自分の Python に何かを入れることはしない
        self.assertEqual(self.labels, [])
        self.assertIn("Demucs / librosa などが入っていません", self.text())

    def test_configured_python_that_does_not_exist(self):
        self.cfg.mv = {"tools_python": str(self.root / "nope" / "python")}
        self.assertFalse(S.setup_mv(self.cfg))
        self.assertIn("[mv] tools_python", self.text())
        self.assertEqual(self.labels, [])

    def test_components_dir_is_followed(self):
        cfg = Settings({"components_dir": "elsewhere"}, self.root)
        with mock.patch.object(S, "mv_ready", side_effect=[False, True]):
            S.setup_mv(cfg, models=False)
        self.assertEqual(S.run_step.call_args_list[0].args[1], self.root / "elsewhere" / "mvtools")


class PrefetchLlmTest(unittest.TestCase):
    """setup --llm: llama-server と GGUF に加えて、画像・音声を読む投影ファイル(約 0.56GB)も、管理役と同じ関数で取得する。"""

    def setUp(self):
        from studio import llm as llm_mod
        self.llm = llm_mod
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.root = Path(self._t.name)
        self.cfg = Settings({"llm": {"tools_dir": str(self.root / "tools")}}, self.root)
        self.said = []
        p = mock.patch.object(S, "say", self.said.append)
        p.start()
        self.addCleanup(p.stop)
        for name, value in (("use_gpu", False), ("find_binary", self.root / "llama-server"), ("find_gguf", self.root / "m.gguf")):
            p = mock.patch.object(llm_mod, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)

    def test_projector_is_fetched_when_missing(self):
        proj = self.root / "tools" / self.llm.MMPROJ_FILE
        with mock.patch.object(self.llm, "find_mmproj", return_value=None), mock.patch.object(self.llm, "download_mmproj", return_value=proj) as download:
            self.assertTrue(S.prefetch_llm(self.cfg))
        download.assert_called_once_with(self.cfg)
        self.assertIn(f"  画像・音声を読む投影ファイル: {proj}", self.said)
        self.assertIn("約 0.56GB", self.said[0])

    def test_an_existing_projector_is_not_downloaded_again(self):
        proj = self.root / "have" / self.llm.MMPROJ_FILE
        with mock.patch.object(self.llm, "find_mmproj", return_value=proj), mock.patch.object(self.llm, "download_mmproj") as download:
            self.assertTrue(S.prefetch_llm(self.cfg))
        download.assert_not_called()

    def test_a_failed_download_is_reported_and_setup_goes_on(self):
        with mock.patch.object(self.llm, "find_mmproj", return_value=None), mock.patch.object(self.llm, "download_mmproj", side_effect=OSError("network down")):
            self.assertFalse(S.prefetch_llm(self.cfg))
        self.assertTrue(any("同梱 LLM の取得" in line and "network down" in line for line in self.said))
        self.assertTrue(any("最初に使うときにも取得します" in line for line in self.said))


if __name__ == "__main__":
    unittest.main()
