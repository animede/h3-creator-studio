"""GPU が 2 枚以上ある PC でエンジンを 1 枚に寄せる処理と、仮想環境が不完全なときの文(2026-11-26)。"""
import os
import unittest
from unittest import mock

from studio import gpu, i18n
from studio.scheduler import exit_message


class PinEnvTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(gpu.configure_devices, "auto")
        env = {k: v for k, v in os.environ.items() if k != "CUDA_VISIBLE_DEVICES"}
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)

    def test_one_gpu_untouched(self):
        self.assertEqual(gpu.pin_env({}, count=1), {})

    def test_two_gpus_pinned_to_managed(self):
        self.assertEqual(gpu.pin_env({}, count=2), {"CUDA_VISIBLE_DEVICES": "0", "CUDA_DEVICE_ORDER": "PCI_BUS_ID"})

    def test_studio_env_first_entry_is_managed(self):
        os.environ["CUDA_VISIBLE_DEVICES"] = "1"
        self.assertEqual(gpu._gpu_index(), 1)
        # 子の環境には os.environ が入るので、利用者の指定はそのまま
        self.assertEqual(gpu.pin_env(dict(os.environ), count=2), {})

    def test_component_env_untouched(self):
        self.assertEqual(gpu.pin_env({"CUDA_VISIBLE_DEVICES": "0,1"}, count=2), {})
        self.assertEqual(gpu.pin_env({"CUDA_VISIBLE_DEVICES": "-1"}, count=2), {})     # CPU 専用の LLM

    def test_devices_all(self):
        gpu.configure_devices("all")
        self.assertEqual(gpu.pin_env({}, count=2), {})

    def test_devices_index(self):
        gpu.configure_devices("1")
        self.assertEqual(gpu._gpu_index(), 1)
        self.assertEqual(gpu.pin_env({}, count=2), {"CUDA_VISIBLE_DEVICES": "1", "CUDA_DEVICE_ORDER": "PCI_BUS_ID"})

    def test_bad_value_warns(self):
        with self.assertLogs("studio.gpu", "WARNING"):
            self.assertEqual(gpu.parse_gpu_devices("second"), "auto")

    def test_parse_smi_count_index(self):
        g = gpu.parse_smi("0, PRO 6000, 1, 98000, 0\n1, A4000, 1, 16000, 0\n", 1)
        self.assertEqual((g["index"], g["count"], g["name"]), (1, 2, "A4000"))

    def test_procman_applies_pin(self):
        from studio import procman
        captured = {}

        class FakeSpec:
            name, label, dir, port, extra_env = "t", "T", ".", 1, {}
            def build_command(self, d): return ["x"]
            def env(self, d, p): return {}
        with mock.patch.object(gpu, "gpu_count", return_value=2), \
             mock.patch.object(procman.subprocess, "Popen", side_effect=lambda *a, **k: captured.update(k["env"]) or (_ for _ in ()).throw(OSError())), \
             mock.patch.object(procman, "LOG_DIR", procman.LOG_DIR):
            with self.assertRaises(OSError):
                procman.start(FakeSpec())
        self.assertEqual(captured["CUDA_VISIBLE_DEVICES"], "0")


class ExitMessageTest(unittest.TestCase):
    TAIL = "=== start\nC:\\x\\.venv\\Scripts\\python.exe: No module named uvicorn\n"

    def test_no_module(self):
        m = exit_message("MiniMax-H3-Image(画像)", "h3image", 1, self.TAIL)
        self.assertIn("Python 環境が不完全です(モジュール uvicorn がありません)", m)
        self.assertIn("setup.bat --only h3image", m)
        self.assertIn("./setup.sh --only h3image", m)
        en = i18n.tr(m, "en")
        self.assertIn("module uvicorn is missing", en)
        self.assertIn("Image (images)", en)

    def test_module_not_found_error(self):
        m = exit_message("X", "ace", 1, "ModuleNotFoundError: No module named 'torch.cuda'")
        self.assertIn("モジュール torch.cuda", m)

    def test_other_exit(self):
        self.assertEqual(exit_message("X", "ace", 1, "Traceback\nRuntimeError: boom"), "X が途中で終了しました(終了コード 1)")


if __name__ == "__main__":
    unittest.main()
