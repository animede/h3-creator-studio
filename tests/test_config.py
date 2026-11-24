import tempfile
import unittest
from pathlib import Path

from studio.config import Settings, load_settings, parse_gpu_mode


class ConfigTest(unittest.TestCase):
    def test_local_override_merges(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            (root / "studio.toml").write_text(
                'port = 8700\n[components.h3]\nrepo = "minimax-h3"\nport = 8611\n[components.ace]\nport = 8001\n', encoding="utf-8")
            (root / "studio.local.toml").write_text(
                'port = 9000\n[components.h3]\ndir = "/opt/h3"\n[components.h3.env]\nFOO = "1"\n', encoding="utf-8")
            s = load_settings(root)
            self.assertEqual(s.port, 9000)
            self.assertEqual(s.host, "127.0.0.1")
            self.assertEqual(s.components["h3"]["port"], 8611)        # 上書きされない値は残る
            self.assertEqual(s.component_dir("h3"), Path("/opt/h3"))
            self.assertEqual(s.component_env("h3"), {"FOO": "1"})
            self.assertEqual(s.component_dir("ace"), root / "components" / "ace")   # repo 無しなら名前そのまま

    def test_default_dir_uses_repo_name(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            (root / "studio.toml").write_text('[components.h3]\nrepo = "minimax-h3"\n', encoding="utf-8")
            self.assertEqual(load_settings(root).component_dir("h3"), root / "components" / "minimax-h3")

    def test_gpu_mode(self):
        self.assertEqual([parse_gpu_mode(v) for v in ("auto", "AUTO", " Share ", "exclusive", "", "bad", None)],
                         ["auto", "auto", "share", "exclusive", "auto", "auto", "auto"])
        self.assertEqual(Settings({}, Path(".")).gpu_mode, "auto")                         # 既定は auto
        self.assertEqual(Settings({"gpu": {"mode": "share"}}, Path(".")).gpu_mode, "share")
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            (root / "studio.toml").write_text('[gpu]\nmode = "auto"\n', encoding="utf-8")
            (root / "studio.local.toml").write_text('[gpu]\nmode = "exclusive"\n', encoding="utf-8")
            self.assertEqual(load_settings(root).gpu_mode, "exclusive")                    # 個人の設定が勝つ

    def test_vram_budget(self):
        s = Settings({"components": {"h3": {"vram_budget_gb": 20}, "ace": {"vram_budget_gb": "12.5"}, "h3image": {"vram_budget_gb": "x"},
                                     "aivis": {"vram_budget_gb": 0}, "other": {}}}, Path("."))
        self.assertEqual([s.component_budget_gb(n) for n in ("h3", "ace", "h3image", "aivis", "other", "none")],
                         [20.0, 12.5, None, None, None, None])

    def test_repo_defaults_file(self):
        s = load_settings()
        self.assertEqual(s.gpu_mode, "auto")
        self.assertEqual({n: c["port"] for n, c in s.components.items()},
                         {"h3": 8611, "h3image": 8633, "ace": 8001, "aivis": 10101})


if __name__ == "__main__":
    unittest.main()
