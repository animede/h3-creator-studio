"""YUZU Creator Studio の MCP サーバの起動口(エージェントの MCP の設定から、このファイルを Python で起こす)。

  Linux:   <スタジオ>/.venv/bin/python <スタジオ>/agent/mcp_server.py
  Windows: <スタジオ>\\.venv\\Scripts\\python.exe <スタジオ>\\agent\\mcp_server.py

--setup を付けると、この PC のパスを入れた登録のコマンド(Claude Code)と設定の例(Claude Desktop)を表示する(mcp が無くても動く)。
スタジオ(run.bat / run.sh)を先に起動しておく。MCP の部品(mcp)は agent/requirements.txt から入れる(agent/README.md)。中身は agent/server.py。
"""
import importlib.util
import json
import shlex
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath

ROOT = Path(__file__).resolve().parent.parent
NAME = "yuzu-studio"


def _path(root: str, win: bool, *parts: str) -> str:
    return str((PureWindowsPath if win else PurePosixPath)(root, *parts))


def venv_python(root: str, win: bool) -> str:
    """スタジオの環境(setup が作る .venv)の Python。"""
    return _path(root, win, ".venv", "Scripts", "python.exe") if win else _path(root, win, ".venv", "bin", "python")


def install_command(win: bool) -> str:
    py = r".venv\Scripts\python.exe" if win else ".venv/bin/python"
    req = r"agent\requirements.txt" if win else "agent/requirements.txt"
    return f"uv pip install --python {py} -r {req}"


def desktop_config_path(platform: str) -> str:
    """Claude Desktop の設定ファイルの場所。"""
    if platform == "win32":
        return r"%APPDATA%\Claude\claude_desktop_config.json"
    if platform == "darwin":
        return "~/Library/Application Support/Claude/claude_desktop_config.json"
    return "~/.config/Claude/claude_desktop_config.json"


def _quote(text: str, win: bool) -> str:
    if win:
        return f'"{text}"' if " " in text else text
    return shlex.quote(text)


def setup_text(root: str, platform: str, has_mcp: bool, venv_ok: bool = True) -> str:
    """--setup で表示する手引き(この PC のパス入り。日本語と英語)。"""
    win = platform == "win32"
    python, script = venv_python(root, win), _path(root, win, "agent", "mcp_server.py")
    config = json.dumps({"mcpServers": {NAME: {"command": python, "args": [script]}}}, indent=2, ensure_ascii=False)
    cd = f"cd {_quote(root, win)}"
    lines = ["YUZU Creator Studio MCP server: setup / 登録の手引き", ""]
    if not venv_ok:
        lines += [f"!! The studio's environment was not found: {python}", "   スタジオの環境が見つかりません。先にスタジオの setup を済ませてください", ""]
    if has_mcp:
        lines += ["[1] MCP package (mcp) / MCP の部品: installed / 入っています", ""]
    else:
        lines += ["[1] MCP package (mcp) / MCP の部品: not installed. Run once / 入っていません。一度だけ実行:",
                  f"      {cd}", f"      {install_command(win)}", ""]
    lines += ["[2] Claude Code: run once / 一度だけ実行:",
              f"      claude mcp add -s user {NAME} -- {_quote(python, win)} {_quote(script, win)}",
              "    Check / 確かめる:  claude mcp list",
              f"    Remove / 外す:     claude mcp remove {NAME} -s user",
              "    It works in conversations started after this / 登録した後に始めた会話から使える", "",
              "[3] Claude Desktop: put this in the settings file, then restart the app / 設定ファイルに書いて、アプリを再起動:",
              f"    File / ファイル: {desktop_config_path(platform)}",
              *("    " + line for line in config.splitlines()),
              f'    If the file already has "mcpServers", add only the "{NAME}" entry inside it',
              f"    既に \"mcpServers\" があれば、その中に \"{NAME}\" の項目だけを足す", "",
              "Start the studio before using it / 使う前にスタジオを起動しておく (run.bat / run.sh)"]
    return "\n".join(lines)


def run(argv: list = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    has_mcp = importlib.util.find_spec("mcp") is not None
    if "--setup" in argv:
        win = sys.platform == "win32"
        print(setup_text(str(ROOT), sys.platform, has_mcp, Path(venv_python(str(ROOT), win)).exists()))
        return
    if not has_mcp:
        sys.exit("The MCP package is not installed. In the studio folder, run once / MCP の部品が入っていません。スタジオのフォルダで一度だけ:\n"
                 f"  {install_command(sys.platform == 'win32')}\n"
                 "(agent/mcp_server.py --setup shows every step / --setup で手順をすべて表示)")
    sys.path.insert(0, str(ROOT))
    from agent.server import main
    main()


if __name__ == "__main__":
    run()
