import tempfile
import unittest
from pathlib import Path

from studio import procman


class CapLogTest(unittest.TestCase):
    """部品のログは追記だけなので、起動のときに大きすぎれば末尾だけを残す。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "h3.log"

    def test_small_log_is_left_alone(self):
        self.path.write_bytes(b"line1\nline2\n")
        self.assertFalse(procman.cap_log(self.path, max_bytes=100, keep_bytes=10))
        self.assertEqual(self.path.read_bytes(), b"line1\nline2\n")

    def test_big_log_keeps_only_whole_lines_of_the_tail(self):
        self.path.write_bytes(b"".join(f"line {i:04d}\n".encode() for i in range(1000)))      # 10 バイト × 1000 行
        self.assertTrue(procman.cap_log(self.path, max_bytes=5000, keep_bytes=35))
        lines = self.path.read_text("utf-8").splitlines()
        self.assertIn("を超えたので、前の部分を消しました", lines[0])
        self.assertEqual(lines[1:], ["line 0997", "line 0998", "line 0999"])              # 行の途中からは始めない
        self.assertFalse(procman.cap_log(self.path, max_bytes=5000, keep_bytes=35))       # 書き直した後は小さい

    def test_missing_log_is_not_an_error(self):
        self.assertFalse(procman.cap_log(self.path))


if __name__ == "__main__":
    unittest.main()
