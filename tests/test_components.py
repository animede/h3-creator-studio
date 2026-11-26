import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from studio.components import ComponentNotInstalled, ace_gpu_env, default_budget_gb, get_spec, h3_gpu_env, h3_profile
from studio.config import Settings


def make_settings(root: Path, **dirs) -> Settings:
    comps = {n: {"dir": str(root / n)} for n in ("h3", "h3image", "ace", "aivis")}
    return Settings({"components": comps}, root)


def touch(p: Path):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("")


def without_alloc(env):
    """PYTORCH_CUDA_ALLOC_CONF は実行環境の値に左右されるので、比べるときは外す。"""
    return {k: v for k, v in env.items() if k != "PYTORCH_CUDA_ALLOC_CONF"}


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
        self.assertEqual(without_alloc(sp.env(sp.dir, sp.port)), {"H3_LLM_LOCAL": "0", "H3_LLM_URL": "http://127.0.0.1:8701"})

    def test_h3image(self):
        touch(self.root / "h3image" / ".venv" / "bin" / "python")
        sp = self.spec("h3image")
        cmd = sp.build_command(sp.dir)
        self.assertEqual(cmd[1:], ["-u", "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8633"])
        self.assertEqual((sp.unload_path, sp.reload_path), ("/api/admin/unload", "/api/admin/load"))
        self.assertEqual(without_alloc(sp.env(sp.dir, sp.port)),
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

    def test_h3_gpu_env_below_14gib(self):
        # 8GB・12GB のカード: 参照画像を短辺 768 で読む(既定の 2048 は 1 回の確保で約 6.9GB 要り、ref2va が VRAM 不足になる。2026-11-26 の実機 ×2)
        self.assertEqual(h3_gpu_env(8188), {"H3_REF_IMAGE_SHORT_EDGE": "768"})
        self.assertEqual(h3_gpu_env(12288), {"H3_REF_IMAGE_SHORT_EDGE": "768"})         # RTX 3060 12GB
        self.assertEqual(h3_gpu_env(14335), {"H3_REF_IMAGE_SHORT_EDGE": "768"})
        self.assertEqual((h3_gpu_env(14336), h3_gpu_env(16376), h3_gpu_env(None)), ({}, {}, {}))   # 16GB 以上は既定のまま
        with mock.patch("studio.components.smi_mib", lambda field: 8188):
            env = get_spec("h3", Settings({}, Path("/r"))).env(Path("/x"), 8611)
            self.assertEqual(env["H3_REF_IMAGE_SHORT_EDGE"], "768")
        with mock.patch("studio.components.smi_mib", lambda field: 16376):
            self.assertNotIn("H3_REF_IMAGE_SHORT_EDGE", get_spec("h3", Settings({}, Path("/r"))).env(Path("/x"), 8611))

    def test_ace_gpu_env_only_below_10gib(self):
        recipe = {"ACESTEP_GPU_PROFILE": "12gb", "ACESTEP_LM_OFFLOAD_TO_CPU": "1", "ACESTEP_RESIDENT_COMPONENTS": "none",
                  "ACESTEP_VAE_DECODE_CHUNK_SIZE": "128", "ACESTEP_QUANTIZATION": "int8_weight_only"}
        self.assertEqual(ace_gpu_env(8188), recipe)          # RTX 3050 8GB / 3060 Ti など
        self.assertEqual(ace_gpu_env(10239), recipe)
        self.assertEqual(ace_gpu_env(10240), {})             # 10GiB 以上は ACE の自動選択に任せる
        self.assertEqual(ace_gpu_env(16376), {})
        self.assertEqual(ace_gpu_env(None), {})              # nvidia-smi が無ければ何も足さない

    def test_default_budgets_from_total_vram(self):
        gib = 1024
        self.assertEqual([default_budget_gb(n, 96 * gib) for n in ("h3", "h3image", "ace", "aivis")], [48, 31, 14, None])
        self.assertEqual([default_budget_gb(n, 80 * gib) for n in ("h3", "h3image", "ace")], [40, 28, 14])
        self.assertEqual([round(default_budget_gb(n, 48 * gib), 2) for n in ("h3", "h3image", "ace")], [24, 16.8, 14])
        self.assertEqual(default_budget_gb("h3", 200 * gib), 48)           # 上限(h3 は 48、h3image は 31)
        self.assertEqual(default_budget_gb("h3image", 200 * gib), 31)

    def test_h3_profile_is_the_largest_that_fits_the_budget(self):
        self.assertEqual([h3_profile(g) for g in (96, 90, 80, 48, 47.8, 40, 32, 24, 16, 12, 8, 6)],
                         ["96gb", "80gb", "80gb", "48gb", "32gb", "32gb", "32gb", "16gb", "16gb", "8gb", "8gb", "8gb"])
        # 既定の利用枠(nvidia-smi の総量 MiB): A100 40GB / L40S / A6000(H3 = 20〜24GiB)は 16gb、A100 80GB / RTX PRO 6000(40〜47.8GiB)は 32gb
        # (2026-11-26 の実測で、上げない)
        self.assertEqual([h3_profile(default_budget_gb("h3", mib)) for mib in (40960, 46068, 49140, 81920, 97887)], ["16gb"] * 3 + ["32gb"] * 2)

    def test_budget_env_for_share(self):
        gib = 1024
        h3, h3image, ace = self.spec("h3"), self.spec("h3image"), self.spec("ace")
        # H3_VRAM_LIMIT_GB は 10 進の GB で、CUDA の固定分(0.75GiB)を引いた分: (24 - 0.75) × 1.0737 = 24.96
        self.assertEqual(h3.budget_env(48 * gib), {"H3_PROFILE": "48gb", "H3_VRAM_LIMIT_GB": "50.73"})
        self.assertEqual(h3.budget_env(24 * gib), {"H3_PROFILE": "16gb", "H3_VRAM_LIMIT_GB": "24.96"})
        self.assertEqual(h3.budget_env(int(47.8 * gib)), {"H3_PROFILE": "32gb", "H3_VRAM_LIMIT_GB": "50.52"})
        self.assertEqual(h3.budget_env(7 * gib), {"H3_PROFILE": "8gb", "H3_VRAM_LIMIT_GB": "6.71"})
        self.assertEqual(h3image.budget_env(31 * gib), {"H3IMG_VRAM_GB": "31"})
        self.assertEqual(h3image.budget_env(int(16.8 * gib)), {"H3IMG_VRAM_GB": "16"})       # 整数の GiB(切り捨て)
        self.assertEqual(ace.budget_env(14 * gib), {"ACESTEP_GPU_PROFILE": "16gb"})
        self.assertEqual(ace.budget_env(13 * gib), {"ACESTEP_GPU_PROFILE": "12gb"})
        self.assertEqual(ace.budget_env(10 * gib), {"ACESTEP_GPU_PROFILE": "12gb"})
        small = ace.budget_env(9 * gib)                                                       # 10GiB 未満は、8GB のカード向けの省メモリ設定も
        self.assertEqual((small["ACESTEP_GPU_PROFILE"], small["ACESTEP_QUANTIZATION"], small["ACESTEP_RESIDENT_COMPONENTS"]),
                         ("12gb", "int8_weight_only", "none"))
        self.assertEqual(self.spec("aivis").budget_env(8 * gib), {})                          # CPU のエンジンに利用枠は無い

    def test_budget_override_comes_from_settings(self):
        cfg = Settings({"components": {"h3": {"dir": "x", "vram_budget_gb": 40}, "h3image": {"dir": "y"}}}, self.root)
        self.assertEqual((get_spec("h3", cfg).vram_budget_gb, get_spec("h3image", cfg).vram_budget_gb), (40.0, None))

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
