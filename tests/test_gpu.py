import time
import unittest
from unittest import mock

from studio import gpu
from studio.gpu import localized_counter_path, parse_perflib, parse_smi

SAMPLE = "0, NVIDIA GeForce RTX 3090, 1234, 24576, 7\n1, NVIDIA GeForce RTX 3060, 100, 12288, 0\n"


class GpuTest(unittest.TestCase):
    def test_parse_picks_index(self):
        self.assertEqual(parse_smi(SAMPLE, 1)["total_mib"], 12288)
        self.assertEqual(parse_smi(SAMPLE, 0), {"index": 0, "count": 2, "name": "NVIDIA GeForce RTX 3090", "used_mib": 1234, "total_mib": 24576, "util": 7})

    def test_parse_garbage(self):
        self.assertIsNone(parse_smi("No devices were found"))

    def test_perflib_parse(self):
        self.assertEqual(parse_perflib("1\r\n1847\r\n2\r\nSystem\r\n\r\n"), {1: "1847", 2: "System"})
        self.assertEqual(parse_perflib(""), {})

    def test_perflib_localized_path(self):
        en = "2\nSystem\n1500\nGPU Process Memory\n1502\nShared Usage\n1504\nDedicated Usage\n"
        ja = "2\nシステム\n1500\nGPU プロセス メモリ\n1502\n共有使用量\n1504\n専用使用量\n"
        self.assertEqual(localized_counter_path(en, ja), "\\GPU プロセス メモリ(*)\\共有使用量")
        self.assertEqual(localized_counter_path(en, en), "\\GPU Process Memory(*)\\Shared Usage")

    def test_perflib_missing(self):
        self.assertIsNone(localized_counter_path("2\nSystem\n", "2\nシステム\n"))
        # 現地語側に番号が無いときも None(呼び出し側が英語のパスに戻す)
        self.assertIsNone(localized_counter_path("1500\nGPU Process Memory\n1502\nShared Usage\n", "1500\nGPU\n"))

    def test_shared_read_throttled(self):
        m, t0 = gpu.GpuMonitor(), time.time()
        with mock.patch.object(gpu.sys, "platform", "win32"), mock.patch.object(gpu, "read_shared_mib", return_value=100) as rd:
            m._update_shared(None, t0)
            m._update_shared(None, t0 + 2)      # 10 秒未満は前の値を使う
            self.assertEqual(rd.call_count, 1)
            m._update_shared(None, t0 + gpu.SHARED_CACHE_S + 1)
            self.assertEqual(rd.call_count, 2)

    def test_shared_total_is_half_of_ram_on_windows_only(self):
        self.assertEqual(gpu.shared_total_mib(32 * 1024 ** 3, "win32"), 16 * 1024)
        self.assertIsNone(gpu.shared_total_mib(32 * 1024 ** 3, "linux"))
        self.assertIsNone(gpu.shared_total_mib(0, "win32"))
        m = gpu.GpuMonitor()
        with mock.patch.object(gpu.sys, "platform", "win32"), mock.patch.object(gpu, "read_shared_mib", return_value=512), \
                mock.patch.object(m, "_read_gpu", return_value=None):
            m.poll()
        snap = m.snapshot()
        self.assertEqual((snap["shared_gpu_mib"], snap["shared_gpu_total_mib"]), (512, int(gpu.psutil.virtual_memory().total / 2 / 1048576)))
        with mock.patch.object(m, "_read_gpu", return_value=None):
            m.poll()                                                           # Linux(このテストの外の platform)では null
        if gpu.sys.platform != "win32":
            self.assertIsNone(m.snapshot()["shared_gpu_total_mib"])

    def test_shared_gives_up_after_failures(self):
        m, t0 = gpu.GpuMonitor(), time.time()
        with mock.patch.object(gpu.sys, "platform", "win32"), mock.patch.object(gpu, "read_shared_mib", return_value=None) as rd:
            for i in range(6):
                m._update_shared(None, t0 + i * 100)
            self.assertEqual(rd.call_count, gpu.SHARED_MAX_FAILS)


if __name__ == "__main__":
    unittest.main()
