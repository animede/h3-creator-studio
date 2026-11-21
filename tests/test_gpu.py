import unittest

from studio.gpu import localized_counter_path, parse_perflib, parse_smi

SAMPLE = "0, NVIDIA GeForce RTX 3090, 1234, 24576, 7\n1, NVIDIA GeForce RTX 3060, 100, 12288, 0\n"


class GpuTest(unittest.TestCase):
    def test_parse_picks_index(self):
        self.assertEqual(parse_smi(SAMPLE, 1)["total_mib"], 12288)
        self.assertEqual(parse_smi(SAMPLE, 0), {"name": "NVIDIA GeForce RTX 3090", "used_mib": 1234, "total_mib": 24576, "util": 7})

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


if __name__ == "__main__":
    unittest.main()
