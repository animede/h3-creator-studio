import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from studio.components import ComponentNotInstalled, ace_gpu_env, get_spec
from studio.config import Settings


def make_settings(root: Path, **dirs) -> Settings:
    comps = {n: {"dir": str(root / n)} for n in ("h3", "h3image", "ace", "aivis")}
    return Settings({"components": comps}, root)


def touch(p: Path):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("")


class ComponentsTest(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.root = Path(self._t.name)
        self.cfg = make_settings(self.root)

    def tearDown(self):
        self._t.cleanup()

    def spec(self, name):
        return get_spec(name, self.cfg)

    def test_missing_dir_raises_with_hint(self):
        for name in ("h3", "h3image", "ace", "aivis"):
            sp = self.spec(name)
            with self.assertRaises(ComponentNotInstalled) as cm:
                sp.build_command(sp.dir)
            self.assertIn("studio.local.toml", cm.exception.hint)

    def test_missing_python_raises(self):
        (self.root / "h3").mkdir()
        sp = self.spec("h3")
        with self.assertRaises(ComponentNotInstalled) as cm:
            sp.build_command(sp.dir)
        self.assertIn("setup", cm.exception.hint)

    def test_h3_linux_and_windows(self):
        touch(self.root / "h3" / "venv" / "bin" / "python")
        touch(self.root / "h3" / ".venv" / "Scripts" / "python.exe")
        sp = self.spec("h3")
        with mock.patch.object(sys, "platform", "linux"):
            cmd = sp.build_command(sp.dir)
        self.assertEqual(cmd[0], str(self.root / "h3" / "venv" / "bin" / "python"))
        self.assertEqual(cmd[1:], ["-u", "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "8611"])
        with mock.patch.object(sys, "platform", "win32"):
            cmd = sp.build_command(sp.dir)
        self.assertEqual(cmd[0], str(self.root / "h3" / ".venv" / "Scripts" / "python.exe"))
        # 内蔵 LLM は動かさず、管理役の LLM(8701)を指す
        self.assertEqual(sp.env(sp.dir, sp.port), {"H3_LLM_LOCAL": "0", "H3_LLM_URL": "http://127.0.0.1:8701"})

    def test_h3image(self):
        touch(self.root / "h3image" / ".venv" / "bin" / "python")
        sp = self.spec("h3image")
        cmd = sp.build_command(sp.dir)
        self.assertEqual(cmd[1:], ["-u", "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8633"])
        self.assertEqual((sp.unload_path, sp.reload_path), ("/api/admin/unload", "/api/admin/load"))
        self.assertEqual(sp.env(sp.dir, sp.port),
                         {"H3IMG_LLM_BASE_URL": "http://127.0.0.1:8701/v1", "H3IMG_LLM_MODEL": "gemma-4-E4B"})

    def test_ace(self):
        touch(self.root / "ace" / "run_api_server_fast.sh")
        touch(self.root / "ace" / "run_api_server_fast.bat")
        sp = self.spec("ace")
        with mock.patch.object(sys, "platform", "linux"):
            self.assertEqual(sp.build_command(sp.dir), ["bash", "run_api_server_fast.sh"])
        with mock.patch.object(sys, "platform", "win32"):
            self.assertEqual(sp.build_command(sp.dir), ["cmd", "/c", "run_api_server_fast.bat"])
        with mock.patch("studio.components.smi_mib", return_value=16376):
            self.assertEqual(sp.env(sp.dir, sp.port), {"ACESTEP_API_HOST": "127.0.0.1", "ACESTEP_API_PORT": "8001"})
        with mock.patch("studio.components.smi_mib", return_value=8192):     # 8GB のカードは省メモリ設定が付く
            self.assertEqual(sp.env(sp.dir, sp.port)["ACESTEP_QUANTIZATION"], "int8_weight_only")
        self.assertFalse(sp.can_unload)
        self.assertTrue(sp.health_ok({"data": {"models_initialized": True}}))
        self.assertFalse(sp.health_ok({"data": {"models_initialized": False}}))

    def test_ace_gpu_env_only_below_10gib(self):
        recipe = {"ACESTEP_GPU_PROFILE": "12gb", "ACESTEP_LM_OFFLOAD_TO_CPU": "1", "ACESTEP_RESIDENT_COMPONENTS": "",
                  "ACESTEP_VAE_DECODE_CHUNK_SIZE": "128", "ACESTEP_QUANTIZATION": "int8_weight_only"}
        self.assertEqual(ace_gpu_env(8188), recipe)          # RTX 3050 8GB / 3060 Ti など
        self.assertEqual(ace_gpu_env(10239), recipe)
        self.assertEqual(ace_gpu_env(10240), {})             # 10GiB 以上は ACE の自動選択に任せる
        self.assertEqual(ace_gpu_env(16376), {})
        self.assertEqual(ace_gpu_env(None), {})              # nvidia-smi が無ければ何も足さない

    def test_aivis(self):
        touch(self.root / "aivis" / "Linux-x64" / "run")
        touch(self.root / "aivis" / "run.exe")
        sp = self.spec("aivis")
        with mock.patch.object(sys, "platform", "linux"):
            cmd = sp.build_command(sp.dir)
        self.assertEqual(cmd, [str(self.root / "aivis" / "Linux-x64" / "run"), "--host", "127.0.0.1", "--port", "10101"])
        with mock.patch.object(sys, "platform", "win32"):
            cmd = sp.build_command(sp.dir)
        self.assertEqual(cmd[0], str(self.root / "aivis" / "run.exe"))
        self.assertNotIn("--use_gpu", cmd)
        self.assertFalse(sp.uses_gpu)


if __name__ == "__main__":
    unittest.main()
