"""agent/mcp_server.py --setup: この PC のパスを入れた登録の手引き(mcp が入っていなくても動く)。"""
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

from agent import mcp_server as launcher

ROOT = Path(__file__).resolve().parent.parent


def desktop_json(text: str) -> dict:
    block = re.search(r"\n    (\{\n.*?\n    \})\n", text, re.S).group(1)
    return json.loads(block)


class SetupTextTest(unittest.TestCase):
    def test_linux_paths_and_valid_desktop_json(self):
        text = launcher.setup_text("/home/u/my studio", "linux", has_mcp=True)
        self.assertIn("MCP の部品: installed", text)
        self.assertIn("claude mcp add -s user yuzu-studio -- '/home/u/my studio/.venv/bin/python' "
                      "'/home/u/my studio/agent/mcp_server.py'", text)                       # 空白のあるパスは囲む
        self.assertIn("~/.config/Claude/claude_desktop_config.json", text)
        self.assertEqual(desktop_json(text), {"mcpServers": {"yuzu-studio": {
            "command": "/home/u/my studio/.venv/bin/python", "args": ["/home/u/my studio/agent/mcp_server.py"]}}})

    def test_windows_paths_install_step_and_escaped_json(self):
        text = launcher.setup_text(r"C:\h3-creator-studio", "win32", has_mcp=False)
        self.assertIn(r"uv pip install --python .venv\Scripts\python.exe -r agent\requirements.txt", text)
        self.assertIn(r"cd C:\h3-creator-studio", text)
        self.assertIn(r"claude mcp add -s user yuzu-studio -- C:\h3-creator-studio\.venv\Scripts\python.exe "
                      r"C:\h3-creator-studio\agent\mcp_server.py", text)
        self.assertIn(r"%APPDATA%\Claude\claude_desktop_config.json", text)
        self.assertEqual(desktop_json(text)["mcpServers"]["yuzu-studio"]["command"], r"C:\h3-creator-studio\.venv\Scripts\python.exe")
        self.assertIn('"C:\\\\h3-creator-studio', text)                                        # JSON では \\ になる

    def test_mac_config_path_and_missing_environment(self):
        text = launcher.setup_text("/Users/u/studio", "darwin", has_mcp=True, venv_ok=False)
        self.assertIn("~/Library/Application Support/Claude/claude_desktop_config.json", text)
        self.assertIn("The studio's environment was not found", text)

    def test_runs_as_a_script(self):
        out = subprocess.run([sys.executable, str(ROOT / "agent" / "mcp_server.py"), "--setup"], capture_output=True, text=True,
                             encoding="utf-8", timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn(str(ROOT / "agent" / "mcp_server.py"), out.stdout)


if __name__ == "__main__":
    unittest.main()
