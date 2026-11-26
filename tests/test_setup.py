import importlib.util
import os
import shutil
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

    def installed(self, *parts):
        """既に入れてあるエンジンらしいフォルダ(git の取り込み + Python 環境)を作る。"""
        d = self.home.joinpath(*parts)
        (d / ".git").mkdir(parents=True)
        (d / "venv").mkdir()
        return d

    def test_home_dir_is_reported_not_cloned(self):
        self.installed("minimax-h3")
        s = S.decide_source("h3", self.cfg, self.home)
        self.assertEqual(s.action, "home")
        self.assertIn(f"{self.home / 'minimax-h3'} が見つかりました", s.message)
        self.assertIn("studio.local.toml に dir を書いてください", s.message)
        self.assertIn("[components.h3]", s.message)
        self.assertIn(f"dir = '{self.home / 'minimax-h3'}'", s.message)
        self.assertFalse(s.run_setup)

    def test_clone_anyway_overrides_home_detection(self):
        (self.home / "minimax-h3").mkdir()
        s = S.decide_source("h3", self.cfg, self.home, clone_anyway=True)
        self.assertEqual(s.action, "clone")

    def test_home_dir_uses_configured_repo_name(self):
        self.installed("my-h3")
        cfg = Settings({"components": {"h3": {"repo": "my-h3"}}}, self.root)
        self.assertEqual(S.decide_source("h3", cfg, self.home).action, "home")

    def test_finds_installs_under_ai_folders_and_by_alias_and_ignores_empty_folders(self):
        (self.home / "minimax-h3").mkdir()                                   # 空のフォルダ(環境が無い)は既存と見なさない
        found = self.installed("AI", "Diffusers_minimax-h3")                 # ホームの AI の下、リポジトリの別名
        s = S.decide_source("h3", self.cfg, self.home)
        self.assertEqual((s.action, s.path), ("home", found))

    def test_windows_candidates_include_drive_roots(self):
        c = [str(x) for x in S.existing_candidates("ace", "ACE-Step-1.5-fast", Path("C:/Users/u"), "win32")]
        self.assertTrue(any(x.replace("\\", "/").startswith("C:/AI") and x.endswith("ACE-Step-1.5-fast") for x in c), c)
        self.assertTrue(any(x.startswith("D:") for x in c))

    def test_write_local_dir_adds_or_replaces_only_dir(self):
        t = self.root / "studio.local.toml"
        S.write_local_dir(t, "h3", Path("/x/minimax-h3"))
        t.write_text(t.read_text() + "[components.ace]\nenabled = false\n", encoding="utf-8")
        S.write_local_dir(t, "h3", Path("/y/minimax-h3"))
        S.write_local_dir(t, "ace", Path("/z/ACE"))
        import tomllib
        cfg = tomllib.loads(t.read_text(encoding="utf-8"))
        self.assertEqual(cfg["components"]["h3"], {"dir": "/y/minimax-h3"})
        self.assertEqual(cfg["components"]["ace"], {"dir": "/z/ACE", "enabled": False})

    def test_ask_yes_is_false_without_a_terminal(self):
        with mock.patch.object(sys, "stdin", None):
            self.assertFalse(S.ask_yes("?"))

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
            self.assertEqual(st[1].argv, ["uv", "pip", "install", "--python", ".venv/bin/python", "-r", "requirements.txt",
                                          "--index-strategy", "unsafe-best-match"])
            self.assertEqual(st[2].argv, [".venv/bin/python", "scripts/prepare_models.py", "--build-cache", "3"])
            self.assertEqual(len(S.component_steps("h3image", have_venv=True)), 2)

    def test_h3image_windows_uses_windows_requirements(self):
        with mock.patch.object(sys, "platform", "win32"):
            st = S.component_steps("h3image")
            self.assertEqual(st[1].argv[-5:], [".venv\\Scripts\\python.exe", "-r", "requirements-windows.txt", "--index-strategy", "unsafe-best-match"])
            self.assertEqual(st[2].argv, [".venv\\Scripts\\python.exe", "scripts\\prepare_models.py", "--build-cache", "3"])

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
        self.assertIn("175GB", S.disk_warning(50, True))
        self.assertIn("145GB", S.disk_warning(50, True, ref=False))
        self.assertIsNone(S.disk_warning(150, True, ref=False))
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
            self.assertEqual(S.component_status("h3image", d), (True, False))        # config.json が無い = 作りかけ(エンジンも読まない)
            touch(d / "models" / "te" / "config.json")
            self.assertEqual(S.component_status("h3image", d), (True, True))
            shared = d / "shared_te"
            touch(shared / "config.json")
            self.assertEqual(S.component_status("h3image", d / "other", env={"H3IMG_TE_DIR": str(shared)}), (False, True))   # 設定の場所を見る
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
        self.assertFalse(a.no_models or a.llm or a.ace_xl or a.clone_anyway or a.mv or a.no_mv)
        a = S.parse_args(["--no-models", "--only", "h3,ace", "--components-dir", "D:/c", "--llm"])
        self.assertTrue(a.no_models and a.llm)
        self.assertFalse(a.no_mv)
        self.assertEqual((a.only, a.components_dir), ("h3,ace", "D:/c"))

    def test_mv_flags(self):
        self.assertTrue(S.parse_args(["--no-mv"]).no_mv)
        a = S.parse_args(["--mv", "--only", "mvtools"])          # --mv は以前の名残(受け付けるが、何も変えない)
        self.assertTrue(a.mv)
        self.assertEqual(a.only, "mvtools")


class ParseOnlyTest(unittest.TestCase):
    ENABLED = ["h3", "h3image", "ace", "aivis"]

    def test_empty_means_every_enabled_component_and_the_mv_tools(self):
        self.assertEqual(S.parse_only("", self.ENABLED), (self.ENABLED, True, []))
        self.assertEqual(S.parse_only("  ", self.ENABLED, no_mv=True), (self.ENABLED, False, []))

    def test_components_keep_the_setup_order(self):
        self.assertEqual(S.parse_only("ace, h3", self.ENABLED), (["h3", "ace"], False, []))

    def test_mvtools_is_not_a_component(self):
        self.assertEqual(S.parse_only("mvtools", self.ENABLED), ([], True, []))
        self.assertEqual(S.parse_only("h3,mvtools", self.ENABLED), (["h3"], True, []))
        self.assertEqual(S.parse_only("h3", self.ENABLED), (["h3"], False, []))        # --only では書いた分だけ
        self.assertEqual(S.parse_only("h3,ace", self.ENABLED, no_mv=True), (["h3", "ace"], False, []))

    def test_unknown_and_disabled_names_are_reported(self):
        self.assertEqual(S.parse_only("zzz,mvtools", self.ENABLED), ([], True, ["zzz"]))
        self.assertEqual(S.parse_only("ace", ["h3"]), ([], False, ["ace"]))


class MvToolsTest(unittest.TestCase):
    """setup(既定)/ --only mvtools: components/mvtools/.venv に CPU 版 torch と Demucs / librosa を入れる。"""

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


class ExistingH3CacheTest(unittest.TestCase):
    """既に入れてあるエンジン(h3)の量子化キャッシュ: 有無を確かめて知らせ、足りなければ作るか聞く(h3_cache_status / plan_existing_h3 / main)。"""

    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.root = Path(self._t.name)
        self.d = self.root / "minimax-h3"
        touch(self.d / "scripts" / "prepare_lowvram.py")
        self.pq = self.d / "models" / "prequant"
        self.lines = []
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for k in [k for k in os.environ if k.startswith("H3_")]:
            del os.environ[k]
        self.real_ask_yes = S.ask_yes
        for target, new in (("say", self.lines.append), ("ask_yes", mock.Mock(return_value=True))):
            p = mock.patch.object(S, target, new)
            p.start()
            self.addCleanup(p.stop)
        self.streaming = mock.patch.object(S.h3cache, "file_streaming", return_value=True)      # Windows か RAM 48GB 未満の PC
        self.streaming.start()
        self.addCleanup(self.streaming.stop)

    def cache(self, name: str, base: Path = None) -> None:
        for f in ("config.json", "meta.json"):
            touch((base or self.pq) / name / f)

    def venv(self, plat: str = "linux") -> Path:
        return touch(self.d / "venv" / ("Scripts/python.exe" if plat == "win32" else "bin/python"))

    def text(self) -> str:
        return "\n".join(self.lines)

    def test_status_counts_only_finished_caches(self):
        st = S.h3_cache_status(self.d, want_ref=True, plat="linux")
        self.assertEqual((st["base"], st["ref"], st["venv"], st["missing"], st["dir"]), (False, False, None, ["base", "ref"], self.pq))
        touch(self.pq / "transformer_int8" / "config.json")                 # meta.json の無いもの・作りかけ(.tmp)は数えない
        self.cache("transformer_ref_int8.tmp")
        self.assertEqual(S.h3_cache_status(self.d, plat="linux")["missing"], ["base", "ref"])
        self.cache("transformer_int8")
        self.assertEqual(S.h3_cache_status(self.d, plat="linux")["missing"], ["ref"])
        self.assertEqual(S.h3_cache_status(self.d, want_ref=False, plat="linux")["missing"], [])        # --no-ref では参照用を数えない
        self.cache("transformer_ref_int8")
        st = S.h3_cache_status(self.d, plat="linux")
        self.assertEqual((st["base"], st["ref"], st["missing"]), (True, True, []))

    def test_status_follows_the_engine_env_and_finds_the_venv(self):
        other = self.root / "pq"
        self.cache("transformer_int8", other)
        self.assertEqual(S.h3_cache_status(self.d, env={"H3_TE_PREQUANT_DIR": str(other)})["missing"], ["ref"])
        both = {"H3_TE_PREQUANT_DIR": str(other), "H3_TRANSFORMER_PREQUANT_DIR": str(self.pq)}
        self.assertEqual(S.h3_cache_status(self.d, env=both)["dir"], self.pq)              # transformer の場所が勝つ(エンジンと同じ)
        os.environ["H3_TE_PREQUANT_DIR"] = str(other)                                    # env を渡さなければ、このプロセスの環境
        self.assertEqual(S.h3_cache_status(self.d)["dir"], other)
        self.assertIsNone(S.h3_cache_status(self.d, plat="linux")["venv"])
        dot = touch(self.d / ".venv" / "bin" / "python")
        self.assertEqual(S.h3_cache_status(self.d, plat="linux")["venv"], dot)
        self.assertEqual(S.h3_venv_python(self.d, "linux"), dot)
        plain = self.venv("linux")
        self.assertEqual(S.h3_venv_python(self.d, "linux"), plain)                       # venv が先(studio.components と同じ)
        self.assertIsNone(S.h3_venv_python(self.d, "win32"))

    def test_nothing_to_do_prints_one_line(self):
        self.venv()
        self.cache("transformer_int8")
        self.cache("transformer_ref_int8")
        self.assertIsNone(S.plan_existing_h3(self.d, True, {}, "linux"))
        self.assertIn("  キャッシュ: 本体 ✓ 参照用 ✓", self.lines)
        S.ask_yes.assert_not_called()
        self.lines.clear()
        shutil.rmtree(self.pq / "transformer_ref_int8")
        self.assertIsNone(S.plan_existing_h3(self.d, False, {}, "linux"))       # --no-ref: 参照用が無くても作らない
        self.assertIn("  キャッシュ: 本体 ✓ 参照用 未(--no-ref なので作りません)", self.lines)
        S.ask_yes.assert_not_called()

    def test_missing_asks_and_returns_the_prepare_step(self):
        py = self.venv()
        self.cache("transformer_int8")
        step = S.plan_existing_h3(self.d, True, {}, "linux")
        S.ask_yes.assert_called_once_with("  いま作りますか?")
        self.assertEqual((step.argv, step.env, step.cwd), ([str(py), "scripts/prepare_lowvram.py", "--ref"], {}, "."))
        self.assertIn("参照モード用(transformer_ref_int8、約 32GB。MV・参照画像のショート動画・動画タブの参照モード)", self.text())
        self.assertNotIn("本体(transformer_int8", self.text())
        self.assertIn("約 37GB", self.text())
        self.assertIn("数時間", self.text())

    def test_windows_no_ref_and_a_moved_cache_dir(self):
        py = self.venv("win32")
        step = S.plan_existing_h3(self.d, False, {}, "win32")
        self.assertEqual((step.argv, step.env), ([str(py), "scripts\\prepare_lowvram.py"], {"HF_HUB_DISABLE_SYMLINKS": "1"}))   # --no-ref
        self.assertIn("本体(transformer_int8、約 60GB", self.text())
        self.assertNotIn("参照モード用(", self.text())
        moved = self.root / "pq"
        step = S.plan_existing_h3(self.d, True, {"H3_TRANSFORMER_PREQUANT_DIR": str(moved)}, "win32")
        self.assertEqual(step.env, {"HF_HUB_DISABLE_SYMLINKS": "1", "H3_TE_PREQUANT_DIR": str(moved)})      # prepare にも同じ場所を渡す
        self.assertEqual(step.argv[-1], "--ref")

    def test_without_a_terminal_it_prints_the_command_and_skips(self):
        self.venv()
        with mock.patch.object(S, "ask_yes", self.real_ask_yes), mock.patch.object(sys, "stdin", None):     # 自動実行(端末でない)
            self.assertIsNone(S.plan_existing_h3(self.d, True, {}, "linux"))
        self.assertIn(f"    cd {self.d} && venv/bin/python scripts/prepare_lowvram.py --ref", self.lines)
        self.assertIn("作らずに進みます", self.text())

    def test_without_the_venv_it_asks_for_the_engine_install_first(self):
        self.assertIsNone(S.plan_existing_h3(self.d, True, {}, "linux"))
        S.ask_yes.assert_not_called()
        self.assertIn("先にエンジンの install を実行してください", self.text())
        self.assertIn(f"    cd {self.d}", self.lines)
        self.assertIn("    bash scripts/install.sh", self.lines)
        self.lines.clear()
        self.assertIsNone(S.plan_existing_h3(self.d, True, {}, "win32"))
        self.assertIn("    cmd /c scripts\\install.bat", self.lines)

    def test_an_old_engine_is_asked_to_update_first(self):
        self.venv()
        (self.d / "scripts" / "prepare_lowvram.py").unlink()              # 2026-11-18 より古いエンジン
        self.assertIsNone(S.plan_existing_h3(self.d, True, {}, "linux"))
        S.ask_yes.assert_not_called()
        self.assertIn("エンジンの更新(git pull)", self.text())

    def test_a_big_ram_host_is_only_told_how(self):
        """Linux で RAM 48GB 以上なら、エンジンが起動時に bf16 から作れる。prepare は bf16 の重みを消すので勧めず、残すコマンドだけを出す。"""
        self.venv()
        self.streaming.stop()
        with mock.patch.object(S.h3cache, "file_streaming", return_value=False):
            self.assertIsNone(S.plan_existing_h3(self.d, True, {}, "linux"))
            S.ask_yes.assert_not_called()
            self.assertIn(f"    cd {self.d} && venv/bin/python scripts/prepare_lowvram.py --ref --keep-source", self.lines)
            self.lines.clear()
            self.assertIsNone(S.plan_existing_h3(self.d, True, {"H3_LOWVRAM": "1"}, "linux"))      # group で読まない構成も同じ
        self.streaming.start()
        self.assertIn("無くても動きます", self.text())
        self.assertIsNone(S.plan_existing_h3(self.d, True, {"H3_LOWVRAM": "1"}, "win32"))
        S.ask_yes.assert_not_called()

    # ---- main の流れ(dir を書いた既存のエンジン = existing) ----

    def run_main(self, *argv):
        cfg = Settings({"components": {"h3": {"dir": str(self.d)}}}, self.root)
        run_step = mock.Mock(return_value=True)
        with mock.patch.object(S, "load_settings", return_value=cfg), mock.patch.object(S, "check_prerequisites", return_value=True), \
                mock.patch.object(S, "run_step", run_step), mock.patch.object(S, "print_summary"):
            rc = S.main(["--only", "h3", *argv])
        return rc, run_step

    def test_main_builds_the_missing_cache_after_asking(self):
        py = self.venv()
        rc, run_step = self.run_main()
        self.assertEqual(rc, 0)
        S.ask_yes.assert_called_once_with("  いま作りますか?")
        ((step, comp_dir), _), = run_step.call_args_list
        self.assertEqual((step.argv, comp_dir), ([str(py), "scripts/prepare_lowvram.py", "--ref"], self.d))
        self.assertIn("== 環境とモデルの準備 ==", "\n".join(self.lines))

    def test_main_no_ref_drops_the_flag(self):
        self.venv()
        rc, run_step = self.run_main("--no-ref")
        self.assertEqual(run_step.call_args[0][0].argv[-1], "scripts/prepare_lowvram.py")

    def test_main_does_not_run_anything_when_declined_complete_or_no_models(self):
        self.venv()
        S.ask_yes.return_value = False                     # 端末でない・「いいえ」
        rc, run_step = self.run_main()
        self.assertEqual(rc, 0)
        run_step.assert_not_called()
        self.assertIn(f"    cd {self.d} && venv/bin/python scripts/prepare_lowvram.py --ref", self.lines)
        S.ask_yes.reset_mock()
        rc, run_step = self.run_main("--no-models")          # モデルを扱わないときは確かめもしない
        run_step.assert_not_called()
        S.ask_yes.assert_not_called()
        self.cache("transformer_int8")
        self.cache("transformer_ref_int8")
        rc, run_step = self.run_main()
        run_step.assert_not_called()
        S.ask_yes.assert_not_called()
        self.assertIn("  キャッシュ: 本体 ✓ 参照用 ✓", self.lines)

    def test_main_stops_when_the_build_fails(self):
        self.venv()
        cfg = Settings({"components": {"h3": {"dir": str(self.d)}}}, self.root)
        with mock.patch.object(S, "load_settings", return_value=cfg), mock.patch.object(S, "check_prerequisites", return_value=True), \
                mock.patch.object(S, "run_step", return_value=False), mock.patch.object(S, "print_summary") as summary:
            self.assertEqual(S.main(["--only", "h3"]), 1)
        summary.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class SetupBatTest(unittest.TestCase):
    """setup.bat の書き方(Windows の cmd は .bat を画面のコードページで 1 行ずつ読む)。"""

    def lines(self):
        raw = (_path.parent.parent / "setup.bat").read_bytes()
        self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"))          # CRLF だけ
        return raw.split(b"\r\n")

    def test_japanese_only_while_utf8(self):
        lines = self.lines()
        first = lines.index(b"chcp 65001 >nul")
        self.assertTrue(all(line.isascii() for line in lines[:first]))   # 切り替える前は ASCII だけ

    def test_python_runs_with_the_original_code_page(self):
        # Windows 10 のコンソールは 65001 で日本語の無い字体に替わり、setup.py の質問が読めなかった(2026-11-26)
        lines = self.lines()
        run = next(i for i, line in enumerate(lines) if line.startswith(b".venv\\Scripts\\python.exe scripts\\setup.py"))
        back = max(i for i in range(run) if b"chcp %OLDCP%" in lines[i])
        utf8 = next(i for i in range(run, len(lines)) if lines[i] == b"chcp 65001 >nul")
        self.assertTrue(all(not line.strip() or line.startswith(b"rem ") for line in lines[back + 1:run]))
        self.assertTrue(all(line.isascii() for line in lines[back:utf8 + 1]))   # 元のコードページで読む行は ASCII だけ


class SharedTextEncoderTest(unittest.TestCase):
    """画像のエンジンのテキストエンコーダ: 動画のエンジンが作った量子化済みを使う(share_h3_te)、既存の画像のエンジンの確認(plan_existing_h3image)。"""

    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.root = Path(self._t.name)
        self.toml = self.root / "studio.local.toml"
        self.h3 = self.root / "minimax-h3"
        self.img = self.root / "minimax-h3-image-server"
        self.img.mkdir()
        self.shared = self.h3 / "models" / "prequant" / "te_bnb-4bit_prune1"
        self.cfg = Settings({}, self.root)
        self.lines = []
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for k in [k for k in os.environ if k.startswith(("H3_", "H3IMG_"))]:
            del os.environ[k]
        for target, new in (("say", self.lines.append), ("ask_yes", mock.Mock(return_value=True))):
            p = mock.patch.object(S, target, new)
            p.start()
            self.addCleanup(p.stop)

    def test_write_local_env(self):
        self.toml.write_text("[components.h3image]\ndir = 'C:\\e'\n[gpu]\nmode = 'auto'\n", encoding="utf-8")
        self.assertTrue(S.write_local_env(self.toml, "h3image", "H3IMG_TE_DIR", "C:\\a\\te"))
        self.assertTrue(S.write_local_env(self.toml, "h3image", "H3IMG_STEPS", "6"))
        self.assertTrue(S.write_local_env(self.toml, "h3image", "H3IMG_TE_DIR", "C:\\b\\te"))      # 同じキーは置き換える
        import tomllib
        cfg = tomllib.loads(self.toml.read_text(encoding="utf-8"))
        self.assertEqual(cfg["components"]["h3image"], {"dir": "C:\\e", "env": {"H3IMG_TE_DIR": "C:\\b\\te", "H3IMG_STEPS": "6"}})
        self.assertEqual(cfg["gpu"], {"mode": "auto"})
        inline = self.root / "inline.toml"
        inline.write_text("[components.h3image]\nenv = { H3IMG_STEPS = '6' }\n", encoding="utf-8")
        self.assertFalse(S.write_local_env(inline, "h3image", "H3IMG_TE_DIR", "x"))              # 書くと TOML の重複になる
        self.assertEqual(inline.read_text(encoding="utf-8"), "[components.h3image]\nenv = { H3IMG_STEPS = '6' }\n")

    def test_quotes_in_paths(self):
        self.assertEqual(S.toml_string("C:\\AI\\x"), "'C:\\AI\\x'")
        self.assertEqual(S.toml_string("C:\\Users\\O'Brien\\x"), '"C:\\\\Users\\\\O\'Brien\\\\x"')
        import tomllib
        S.write_local_dir(self.toml, "h3", Path("/home/o'brien/minimax-h3"))
        self.toml.write_text(self.toml.read_text(encoding="utf-8").replace("[components.h3]", "[components.h3]  # メモ"), encoding="utf-8")
        S.write_local_dir(self.toml, "h3", Path("/home/o'brien/h3"))                 # 見出しの後ろにコメントがあっても同じ節
        self.assertTrue(S.write_local_env(self.toml, "h3", "H3_X", "it's"))
        cfg = tomllib.loads(self.toml.read_text(encoding="utf-8"))
        self.assertEqual(cfg["components"]["h3"], {"dir": "/home/o'brien/h3", "env": {"H3_X": "it's"}})

    def test_second_run_keeps_passing_the_shared_encoder(self):
        # 2 回目の setup: 設定に H3IMG_TE_DIR がある → share_h3_te は {} だが、準備には設定の env を重ねて渡す(main と同じ合成)
        touch(self.shared / "config.json")
        S.share_h3_te(self.cfg, self.h3, self.img, self.toml)
        cfg2 = S.load_settings(self.root)
        self.assertEqual(cfg2.component_env("h3image"), {"H3IMG_TE_DIR": str(self.shared.resolve())})
        self.assertEqual(S.share_h3_te(cfg2, self.h3, self.img, self.toml), {})
        merged = {**cfg2.component_env("h3image"), **{}}
        self.ready_cache()
        touch(self.img / ".venv" / "bin" / "python")
        self.assertIsNone(S.plan_existing_h3image(self.img, env=S.h3imgcache.engine_env(cfg2), extra_env=merged))   # 揃っている
        self.assertEqual(S.component_status("h3image", self.img, env=S.h3imgcache.engine_env(cfg2))[1], True)        # まとめも「あり」
        self.assertIn("H3IMG_TE_DIR=", S.h3imgcache.prepare_command(self.img, 3, "linux", merged))
        self.assertIn("$env:H3IMG_TE_DIR='", S.h3imgcache.prepare_command(self.img, 3, "win32", merged))

    def test_configured_but_missing_encoder_is_explained(self):
        S.write_local_env(self.toml, "h3image", "H3IMG_TE_DIR", "/gone/te")
        cfg2 = S.load_settings(self.root)
        self.assertEqual(S.share_h3_te(cfg2, self.h3, self.img, self.toml), {})
        self.assertIn("H3IMG_TE_DIR(/gone/te)にテキストエンコーダがありません", "\n".join(self.lines))

    def test_share_when_the_video_engine_made_it(self):
        self.assertEqual(S.share_h3_te(self.cfg, self.h3, self.img, self.toml), {})        # 動画のエンジンが作っていない(8〜16GB の級)
        self.assertFalse(self.toml.exists())
        touch(self.shared / "config.json")
        env = S.share_h3_te(self.cfg, self.h3, self.img, self.toml)
        self.assertEqual(env, {"H3IMG_TE_DIR": str(self.shared.resolve())})
        import tomllib
        self.assertEqual(tomllib.loads(self.toml.read_text(encoding="utf-8"))["components"]["h3image"]["env"], env)
        self.assertIn("約 67GB の取得と量子化を省きます", "\n".join(self.lines))
        self.assertEqual(S.share_h3_te(self.cfg, None, self.img, self.toml), {})             # 動画のエンジンが無い

    def test_keep_the_image_engines_own(self):
        touch(self.shared / "config.json")
        touch(self.img / "models" / "te" / "config.json")
        self.assertEqual(S.share_h3_te(self.cfg, self.h3, self.img, self.toml), {})
        self.assertFalse(self.toml.exists())

    def ready_cache(self):
        touch(self.img / "models" / "cache" / "ckw4a8_adaln_table_pdmd3" / "transformer_adaln_table.safetensors")

    def test_existing_engine_without_text_encoder_is_prepared(self):
        # 「既存を使う」と答えたが、モデルの準備がされていなかった(2026-11-26、Windows の実機)
        self.ready_cache()
        py = touch(self.img / ".venv" / "bin" / "python")
        step = S.plan_existing_h3image(self.img, env={}, plat="linux")
        self.assertEqual(step.argv, [str(py), "scripts/prepare_models.py", "--build-cache", "3"])
        self.assertIn("テキストエンコーダ(約 17GB。作るときに 66.7GB を取得)がありません", "\n".join(self.lines))
        S.ask_yes.assert_called_once()

    def test_existing_engine_with_the_shared_text_encoder_needs_nothing(self):
        self.ready_cache()
        touch(self.shared / "config.json")
        self.assertIsNone(S.plan_existing_h3image(self.img, env={}, plat="linux", extra_env={"H3IMG_TE_DIR": str(self.shared)}))
        self.assertIn("  本体のキャッシュ(ステップ数 3): ✓  テキストエンコーダ: ✓", self.lines)
        S.ask_yes.assert_not_called()

    def test_the_build_step_gets_the_shared_text_encoder(self):
        touch(self.shared / "config.json")
        touch(self.img / ".venv" / "Scripts" / "python.exe")
        step = S.plan_existing_h3image(self.img, env={}, plat="win32", extra_env={"H3IMG_TE_DIR": str(self.shared)})
        self.assertEqual(step.env, {"HF_HUB_DISABLE_SYMLINKS": "1", "H3IMG_TE_DIR": str(self.shared)})
        self.assertNotIn("テキストエンコーダ(約 17GB", "\n".join(self.lines))            # 本体のキャッシュだけが足りない
