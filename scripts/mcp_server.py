"""YUZU Creator Studio の MCP サーバの起動口(エージェントの MCP の設定から、このファイルを Python で起こす)。

  Linux:   <スタジオ>/.venv/bin/python <スタジオ>/scripts/mcp_server.py
  Windows: <スタジオ>\\.venv\\Scripts\\python.exe <スタジオ>\\scripts\\mcp_server.py

スタジオ(run.bat / run.sh)を先に起動しておく。中身は studio/mcp_server.py。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from studio.mcp_server import main  # noqa: E402

if __name__ == "__main__":
    main()
