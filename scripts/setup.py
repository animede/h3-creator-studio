#!/usr/bin/env python
"""h3-creator-studio のセットアップ(setup.sh / setup.bat から呼ばれる)。

部品(minimax-h3 / minimax-h3-image-server / ACE-Step-1.5-fast / AivisSpeech-Engine)を components/ に取り込み、
それぞれ自分のセットアップスクリプトで環境とモデルを用意する。何度実行してもよく、済んでいる所は飛ばし、
途中で止まっても再実行で続きから進む。

  python scripts/setup.py                 # 取り込み + 環境 + モデル(合計 約 100GB)
  python scripts/setup.py --no-models     # 環境だけ(モデルは後で)
  python scripts/setup.py --only h3,ace   # 指定した部品だけ
  python scripts/setup.py --llm           # 同梱 LLM(llama-server + Gemma 4 E4B)も先に取得する
  python scripts/setup.py --mv            # MV の音声処理の道具(Demucs + librosa、CPU 版 torch。約 1.3GB)も用意する
  python scripts/setup.py --only mvtools  # その道具だけ(--only には h3,mvtools のように混ぜて書ける)
"""
from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from studio.config import Settings, load_settings  # noqa: E402

AIVIS_VERSION = "1.2.0"
AIVIS_BASE = f"https://github.com/Aivis-Project/AivisSpeech-Engine/releases/download/{AIVIS_VERSION}/"
MIN_RAM_GIB = 30           # 32GB の PC は 31 GiB 台で見えることがあるので、少し下に線を引く
MIN_VRAM_GIB = 5.8         # 6GB のカードは 5.9〜6.0 GiB で見える
NEED_DISK_GIB = 100


@dataclass(frozen=True)
class Comp:
    name: str
    label: str
    repo: str                       # components/ 内の既定のフォルダ名
    url: Optional[str]              # git の取得元。None なら配布版のダウンロード(aivis)
    notice: str = ""                # 環境・モデルの準備の前に表示する目安


COMPS = {
    "h3": Comp("h3", "MiniMax-H3(動画・静止画)", "minimax-h3", "https://github.com/animede/Diffusers_minimax-h3.git",
               "モデルの準備は約 60GB のダウンロードです。回線次第で数時間かかります(途中で止まっても再実行で続きから進みます)"),
    "h3image": Comp("h3image", "MiniMax-H3-Image(画像)", "minimax-h3-image-server",
                    "https://github.com/animede/minimax-h3-image-server.git",
                    "モデルの準備は約 25GB のダウンロードと量子化です。回線次第で 1 時間以上かかります"),
    "ace": Comp("ace", "ACE-Step(音楽)", "ACE-Step-1.5-fast", "https://github.com/animede/ACE-Step-1.5-fast.git",
                "モデルは 2B 系だけ(約 15GB)です"),
    "aivis": Comp("aivis", "AivisSpeech(音声合成)", "AivisSpeech-Engine", None),
}
ORDER = ("h3", "h3image", "ace", "aivis")

# MV の音声処理の道具(components/mvtools/.venv)。部品ではなく、管理役が子プロセスで使う Python 環境(studio/mv_audio.py)
MV_NAME = "mvtools"
MV_LABEL = "MV の音声処理の道具(Demucs + librosa、CPU 版 torch)"
MV_TORCH_INDEX = "https://download.pytorch.org/whl/cpu"
# torch / torchaudio は CPU 版の cp312 の wheel が manylinux_2_28_x86_64 と win_amd64 の両方にある版(2.11.0)。
# Demucs 4.0.1 は torchaudio の上限を付けていないので、この版で動く(Linux で htdemucs の分離まで確認済み)
MV_TORCH = ["torch==2.11.0", "torchaudio==2.11.0"]
MV_PACKAGES = ["demucs==4.0.1", "soundfile", "librosa==0.11.0"]
MV_IMPORTS = "import torch, soundfile, librosa, demucs.apply, demucs.pretrained"
MV_WARMUP = "from demucs.pretrained import get_model; get_model('htdemucs')"


def say(msg: str = "") -> None:
    print(msg, flush=True)


def is_win(plat: str = None) -> bool:
    return (plat or sys.platform) == "win32"


# ---- 事前確認(純粋な部分) ----

def missing_tools(which: Callable = shutil.which, plat: str = None) -> list:
    """PATH に無い必須ツールを [(名前, 入れ方のヒント)] で返す。"""
    if is_win(plat):
        hints = {"git": "winget install --id Git.Git -e --source winget",
                 "ffmpeg": "winget install --id Gyan.FFmpeg -e --source winget"}
    else:
        hints = {"git": "sudo apt install git", "ffmpeg": "sudo apt install ffmpeg"}
    return [(t, h) for t, h in hints.items() if not which(t)]


def parse_gpu_query(text: str) -> Optional[tuple]:
    """`nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits` の最初の行 → (名前, 総 VRAM の MiB)。"""
    for line in text.strip().splitlines():
        name, _, total = line.rpartition(",")
        try:
            return name.strip(), int(float(total.strip()))
        except ValueError:
            continue
    return None


def resource_warnings(ram_gib: Optional[float], vram_mib: Optional[int]) -> list:
    out = []
    if ram_gib is not None and ram_gib < MIN_RAM_GIB:
        out.append(f"CPU メモリが {ram_gib:.0f}GB です。CPU メモリ 32GB 未満では動画の準備(量子化)と音楽の同梱 LLM が厳しくなります")
    if vram_mib is not None and vram_mib / 1024 < MIN_VRAM_GIB:
        out.append(f"VRAM が {vram_mib / 1024:.1f}GB です。6GB 未満では画角を絞っても動かない処理があります(推奨は 12GB 以上)")
    return out


def disk_warning(free_gib: float, models: bool) -> Optional[str]:
    if models and free_gib < NEED_DISK_GIB:
        return f"ディスクの空きが {free_gib:.0f}GB です。モデルまで入れると約 {NEED_DISK_GIB}GB 要ります"
    return None


# ---- 取り込みの判断(純粋な部分) ----

@dataclass
class Source:
    action: str          # existing / missing-dir / present / blocked / home / clone / download
    path: Path
    message: str
    run_setup: bool      # この後、環境とモデルの準備をするか


def toml_dir_example(name: str, path: Path) -> str:
    """studio.local.toml に書く例。パスはリテラル文字列('...')にして、Windows の \\ をそのまま書けるようにする。"""
    return f"[components.{name}]\ndir = '{path}'"


def aivis_exe_name(plat: str = None) -> str:
    return "run.exe" if is_win(plat) else "run"


def find_aivis_exe(d: Path, plat: str = None) -> Optional[Path]:
    """studio.components の探し方と同じ: <dir>/run[.exe] か <dir>/Windows-x64|Linux-x64/run[.exe]。"""
    exe, sub = ("run.exe", "Windows-x64") if is_win(plat) else ("run", "Linux-x64")
    for c in (d / exe, d / sub / exe):
        if c.is_file():
            return c
    return None


def decide_source(name: str, cfg: Settings, home: Path, clone_anyway: bool = False, plat: str = None) -> Source:
    """部品 1 つについて「既存を使う / 取り込み済み / 取り込む」を決める。何もダウンロードしない。"""
    comp = COMPS[name]
    target = cfg.component_dir(name)
    if cfg.components.get(name, {}).get("dir"):
        if target.is_dir():
            return Source("existing", target, f"既存を使います: {target}(取り込みもモデル取得も飛ばします)", False)
        return Source("missing-dir", target,
                      f"studio.local.toml の [components.{name}] dir のフォルダがありません: {target}\n"
                      "  パスを直すか、dir の行を消して setup をやり直してください(この部品は飛ばします)", False)
    if comp.url:
        present = (target / ".git").exists() or (target.is_dir() and any(target.iterdir()))
    else:
        present = find_aivis_exe(target, plat) is not None
    if present:
        return Source("present", target, f"取り込み済みです: {target}", True)
    if not comp.url and target.is_dir() and any(target.iterdir()):
        return Source("blocked", target,
                      f"{target} は空ではなく、AivisSpeech の実行ファイル({aivis_exe_name(plat)})もありません。"
                      "中身を確認して、不要ならそのフォルダを消してからやり直してください", False)
    repo = str(cfg.components.get(name, {}).get("repo", comp.repo))
    in_home = Path(home) / repo
    if in_home.is_dir():
        msg = (f"~/{repo} が見つかりました。これを使うなら studio.local.toml に dir を書いてください(例):\n"
               + "\n".join("    " + ln for ln in toml_dir_example(name, in_home).splitlines())
               + "\n  新しく取り込み直すなら --clone-anyway を付けて setup をやり直してください(この部品は飛ばします)")
        if not clone_anyway:
            return Source("home", in_home, msg, False)
    return Source("download" if not comp.url else "clone", target,
                  f"取り込みます: {comp.url or aivis_url()} → {target}", True)


# ---- 部品ごとのコマンド(純粋な部分) ----

@dataclass
class Step:
    label: str
    argv: list
    cwd: str = "."                               # 部品のフォルダからの相対
    env: dict = field(default_factory=dict)


def format_command(argv: list, plat: str = None) -> str:
    return subprocess.list2cmdline(argv) if is_win(plat) else shlex.join(argv)


def component_steps(name: str, models: bool = True, ace_xl: bool = False, have_venv: bool = False,
                    plat: str = None, h3_ref: bool = True) -> list:
    """環境とモデルの準備に呼ぶコマンド(部品自身のスクリプト)。相対パスは部品のフォルダ基準。"""
    win = is_win(plat)
    env = {"HF_HUB_DISABLE_SYMLINKS": "1"} if win else {}
    if name == "h3":
        steps = [Step("h3: 環境の作成(scripts/install)", ["cmd", "/c", "scripts\\install.bat"] if win
                      else ["bash", "scripts/install.sh"])]
        if models:
            py = "venv\\Scripts\\python.exe" if win else "venv/bin/python"
            script = "scripts\\prepare_lowvram.py" if win else "scripts/prepare_lowvram.py"
            # --ref: 参照モード(Ref2VA / Ref2I、Vocal Lock)用の transformer_ref の量子化キャッシュ(+約 32GB)。
            # このスタジオは参照モードを使うので既定で付ける(--no-ref で外せる)
            argv = [py, script] + (["--ref"] if h3_ref else [])
            steps.append(Step("h3: モデルの準備(scripts/prepare_lowvram.py)", argv, env=env))
        return steps
    if name == "h3image":
        py = ".venv\\Scripts\\python.exe" if win else ".venv/bin/python"
        req = "requirements-windows.txt" if win else "requirements.txt"
        steps = []
        if not have_venv:
            steps.append(Step("h3image: 仮想環境の作成(uv venv)", ["uv", "venv", "--python", "3.12", ".venv"]))
        steps.append(Step("h3image: 依存パッケージ(uv pip install)", ["uv", "pip", "install", "--python", py, "-r", req]))
        if models:
            script = "scripts\\prepare_models.py" if win else "scripts/prepare_models.py"
            steps.append(Step("h3image: モデルの準備(scripts/prepare_models.py)", [py, script], env=env))
        return steps
    if name == "ace":
        # --no-client(easy-music の環境を作らない)は Linux の setup.sh だけにある。Windows の setup.bat は
        # 未知の引数を無視するが、紛らわしいので付けない(easy-music の .venv が作られるが使わないだけ)
        args = ([] if ace_xl else ["--12gb"]) + ([] if win else ["--no-client"]) + ([] if models else ["--no-models"])
        argv = (["cmd", "/c", "setup.bat"] if win else ["bash", "setup.sh"]) + args
        return [Step("ace: 環境とモデル(setup)", argv, env=env)]
    return []                                         # aivis は配布版


def python312_env(plat: str = None, which: Callable = shutil.which, run: Callable = subprocess.run) -> dict:
    """h3 の install.sh は python3.12 を探す。無ければ uv で入れて PYTHON を渡す(Linux のみ)。"""
    if is_win(plat) or which("python3.12"):
        return {}
    if not which("uv"):
        return {}
    run(["uv", "python", "install", "3.12"], check=False)
    out = run(["uv", "python", "find", "3.12"], capture_output=True, text=True, check=False)
    found = (out.stdout or "").strip().splitlines()
    return {"PYTHON": found[0]} if out.returncode == 0 and found else {}


# ---- 状態の確認(まとめの表) ----

def component_status(name: str, d: Path, plat: str = None) -> tuple:
    """(環境あり, モデルあり or None=不要)。"""
    def exists(*parts):
        return Path(d, *parts).exists()
    sub = "Scripts/python.exe" if is_win(plat) else "bin/python"
    if name == "h3":
        return exists("venv", sub) or exists(".venv", sub), exists("models", "prequant", "transformer_int8")
    if name == "h3image":
        te = Path(d, "models", "te")
        return exists(".venv", sub) or exists("venv", sub), te.is_dir() and any(te.iterdir())
    if name == "ace":
        script = "run_api_server_fast.bat" if is_win(plat) else "run_api_server_fast.sh"
        return exists(".venv", sub) and exists(script), exists("checkpoints", "acestep-v15-turbo")
    return find_aivis_exe(d, plat) is not None, None


# ---- AivisSpeech 配布版 ----

def aivis_url(plat: str = None) -> str:
    return AIVIS_BASE + f"AivisSpeech-Engine-{'Windows' if is_win(plat) else 'Linux'}-x64-{AIVIS_VERSION}.7z.001"


def _rename_retry(src: Path, dst: Path, tries: int = 10, wait: float = 0.5) -> None:
    """rename を PermissionError のときだけ再試行する(Windows では Defender やインデクサが展開直後のファイルを掴む)。"""
    for i in range(tries):
        try:
            src.rename(dst)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(wait)


def _lift(sub: Path, dest: Path) -> None:
    """sub の中身を dest へ持ち上げて sub を消す(sub と同名の中身があっても衝突しないよう、一度どかす)。"""
    tmp = dest / f".lift-{os.getpid()}"
    _rename_retry(sub, tmp)
    for item in list(tmp.iterdir()):
        _rename_retry(item, dest / item.name)
    tmp.rmdir()


def normalize_aivis_layout(d: Path, plat: str = None) -> Optional[Path]:
    """展開した配布版の実行ファイルが <d>/run[.exe] か <d>/<Windows|Linux>-x64/run[.exe] に来るよう、1 段の入れ物を剥がす。"""
    exe = find_aivis_exe(d, plat)
    while exe is None:
        entries = list(d.iterdir())
        if len(entries) == 1 and entries[0].is_dir():
            _lift(entries[0], d)
            exe = find_aivis_exe(d, plat)
            continue
        break
    if exe is None:       # 入れ物が 1 つでないとき: run を探して、それと同じフォルダの中身を d へ持ち上げる
        found = next((p for p in sorted(d.rglob(aivis_exe_name(plat)), key=lambda p: len(p.parts)) if p.is_file()), None)
        if found is not None and found.parent != d:
            for item in list(found.parent.iterdir()):
                if not (d / item.name).exists():
                    _rename_retry(item, d / item.name)
            exe = find_aivis_exe(d, plat)
    if exe is not None and not is_win(plat):
        exe.chmod(exe.stat().st_mode | 0o111)
    return exe


def extract_7z(archive: Path, dest: Path) -> None:
    """.7z(.001 で終わる単一ボリュームでも、.002 以降がある分割でも)を dest へ展開する。"""
    import py7zr
    dest.mkdir(parents=True, exist_ok=True)
    if archive.suffix.lstrip(".").isdigit() and archive.with_suffix(".002").exists():
        import multivolumefile
        with multivolumefile.open(archive.with_suffix(""), "rb") as mv, py7zr.SevenZipFile(mv) as z:
            z.extractall(dest)
    else:
        with py7zr.SevenZipFile(archive, "r") as z:
            z.extractall(dest)


def download(url: str, dest: Path) -> None:
    """url を dest へ(メモリに載せず、進み具合を表示しながら)取得する。"""
    import httpx
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    last = time.monotonic()
    with httpx.stream("GET", url, follow_redirects=True, timeout=httpx.Timeout(60.0, read=300.0)) as r:
        r.raise_for_status()
        total, got = int(r.headers.get("content-length", 0)), 0
        with open(tmp, "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
                got += len(chunk)
                if time.monotonic() - last > 5:
                    last = time.monotonic()
                    say(f"  ... {got / 2**20:.0f}MB" + (f" / {total / 2**20:.0f}MB" if total else ""))
    tmp.replace(dest)


def install_aivis(target: Path, plat: str = None, fetch: Callable = download, extract: Callable = extract_7z) -> Path:
    """配布版をダウンロード・展開して target に置く。実行ファイルのパスを返す。"""
    import platform
    if platform.machine().lower() not in ("x86_64", "amd64"):
        raise RuntimeError("AivisSpeech の配布版は x64 の Windows / Linux だけです")
    work = target.parent / "_download"
    tmp = target.parent / (target.name + ".tmp")
    for p in (work, tmp):
        shutil.rmtree(p, ignore_errors=True)
    archive = work / aivis_url(plat).rsplit("/", 1)[1]
    try:
        say(f"AivisSpeech Engine {AIVIS_VERSION} を取得しています(約 {'220' if is_win(plat) else '340'}MB)")
        fetch(aivis_url(plat), archive)
        say("展開しています")
        extract(archive, tmp)
        if normalize_aivis_layout(tmp, plat) is None:
            raise RuntimeError(f"展開しても {aivis_exe_name(plat)} が見つかりません(配布物の構成が変わったかもしれません)")
        if target.is_dir():
            target.rmdir()                  # 空のフォルダだけ(decide_source が空でないものは弾いている)
        _rename_retry(tmp, target)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)
    exe = find_aivis_exe(target, plat)
    assert exe is not None
    return exe


# ---- 実行(副作用のある部分) ----

def ensure_uv_on_path() -> bool:
    """uv が PATH に無ければ、インストーラの置き場所(~/.local/bin など)を足してみる。"""
    if shutil.which("uv"):
        return True
    for d in (Path.home() / ".local" / "bin", Path.home() / ".cargo" / "bin",
              Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links"):
        if (d / ("uv.exe" if is_win() else "uv")).is_file():
            os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
            return True
    return False


def resolve_argv(argv: list, cwd: Path) -> list:
    """相対パスで書いた実行ファイル(venv/bin/python など)を、cwd 基準の絶対パスにする(Windows は親の cwd で探すため)。"""
    first = argv[0]
    if (os.sep in first or "/" in first) and not os.path.isabs(first):
        return [str(cwd / first)] + argv[1:]
    return argv


def fail(step_label: str, argv: list, cwd: Path, rc: Optional[int] = None) -> int:
    say()
    say(f"失敗しました: {step_label}" + (f"(終了コード {rc})" if rc is not None else ""))
    say("上の出力に原因が出ています。直したら、setup をもう一度実行してください(済んでいる所は飛ばして続きから進みます)。")
    say("この手順だけをやり直すなら:")
    say(f"  cd {cwd}")
    say(f"  {format_command(argv)}")
    return 1


def run_step(step: Step, comp_dir: Path) -> bool:
    cwd = (comp_dir / step.cwd).resolve()
    argv = resolve_argv(step.argv, cwd)
    say(f"\n>>> {step.label}\n    ({cwd} で {format_command(step.argv)})")
    env = {**os.environ, "PYTHONUNBUFFERED": "1", **step.env}
    try:
        rc = subprocess.call(argv, cwd=cwd, env=env)
    except OSError as e:
        say(f"実行できませんでした: {e}")
        rc = 127
    if rc != 0:
        fail(step.label, step.argv, cwd, rc)
        return False
    return True


def git_clone(url: str, dest: Path) -> bool:
    existed = dest.exists()
    dest.parent.mkdir(parents=True, exist_ok=True)
    rc = subprocess.call(["git", "clone", url, str(dest)])
    if rc != 0:
        if not existed:
            shutil.rmtree(dest, ignore_errors=True)         # 中途半端な clone を残さない(再実行で取り直せるように)
        fail(f"git clone {url}", ["git", "clone", url, str(dest)], dest.parent, rc)
        return False
    return True


def gpu_info() -> Optional[tuple]:
    from studio.gpu import find_nvidia_smi
    exe = find_nvidia_smi()
    if exe is None:
        return None
    try:
        out = subprocess.run([exe, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_gpu_query(out.stdout) if out.returncode == 0 else None


def check_prerequisites(selected: list, models: bool, free_path: Path) -> bool:
    """事前確認を表示する。続行できなければ False。"""
    say("== 事前確認 ==")
    ok = True
    for tool, hint in missing_tools():
        say(f"  {tool} が見つかりません → {hint}" + ("(入れたら PowerShell を開き直してください)" if is_win() else ""))
        ok = False
    if "h3" in selected and is_win() and not _py312_on_windows():
        say("  Python 3.12 が見つかりません → winget install --id Python.Python.3.12 -e --source winget(入れたら PowerShell を開き直す)")
        ok = False
    gpu = gpu_info()
    if gpu is None:
        say("  NVIDIA ドライバ(nvidia-smi)が見つかりません。GPU が使えないと生成は動きません(環境だけ作るなら続けられます)")
    else:
        say(f"  GPU: {gpu[0]}  VRAM {gpu[1] / 1024:.1f}GB")
    import psutil
    ram = psutil.virtual_memory().total / 2**30
    say(f"  CPU メモリ: {ram:.0f}GB")
    free_path.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(free_path).free / 2**30
    say(f"  ディスクの空き({free_path}): {free:.0f}GB")
    for w in resource_warnings(ram, gpu[1] if gpu else None) + [x for x in [disk_warning(free, models)] if x]:
        say(f"  ※ {w}")
    return ok


def _py312_on_windows() -> bool:
    for cmd in (["py", "-3.12", "--version"], ["python", "--version"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode == 0 and "3.12" in (out.stdout + out.stderr):
            return True
    return False


def parse_only(only: str, enabled: list, mv: bool = False) -> tuple:
    """--only の値 → (対象の部品, MV の音声処理の道具も対象か, 使えない名前)。空なら全部の部品。
    mvtools は部品ではなく、--mv と同じ(--only mvtools だけなら部品は何も触らない)。"""
    if not only.strip():
        return list(enabled), mv, []
    want = [x.strip() for x in only.split(",") if x.strip()]
    bad = [x for x in want if x != MV_NAME and x not in enabled]
    return [n for n in ORDER if n in want and n in enabled], mv or MV_NAME in want, bad


def mv_notice(plat: str = None) -> str:
    """導入の前に出す目安。ダウンロードは wheel の大きさの合計(実測): Linux で約 410MB(torch の CPU 版 約 190MB)、
    Windows で約 315MB(同 約 115MB)。どちらも Demucs の重み(約 80MB)を含む。ディスクは Linux の venv が約 1.2GB + 重み。"""
    total, torch_mb, rest = (315, 115, 120) if is_win(plat) else (410, 190, 140)
    return (f"約 {total}MB のダウンロード(torch の CPU 版 約 {torch_mb}MB、scipy・numba ほか 約 {rest}MB、Demucs の重み 約 80MB)で、"
            "ディスクには最大で約 1.3GB 使います。回線次第で数分かかります")


def mv_steps(venv: Path, py: Path, have_venv: bool = False) -> list:
    """MV の音声処理の道具を入れるコマンド(uv)。venv / py は絶対パス。
    torch / torchaudio を CPU 用のインデックスから先に入れておくと、あとの Demucs は(PyPI の CUDA 版ではなく)それを使う。"""
    steps = []
    if not have_venv:
        steps.append(Step("mvtools: 仮想環境の作成(uv venv)", ["uv", "venv", "--python", "3.12", str(venv)]))
    steps.append(Step("mvtools: torch / torchaudio(CPU 版)の導入",
                      ["uv", "pip", "install", "--python", str(py), "--index-url", MV_TORCH_INDEX] + MV_TORCH))
    steps.append(Step("mvtools: Demucs / soundfile / librosa の導入", ["uv", "pip", "install", "--python", str(py)] + MV_PACKAGES))
    return steps


def mv_warmup_step(py: Path) -> Step:
    """htdemucs の重み(約 80MB)を先に取得する。取得済みなら数秒で終わる。"""
    return Step("mvtools: Demucs の重み(htdemucs)の取得", [str(py), "-c", MV_WARMUP])


def mv_ready(py: Path, run: Callable = subprocess.run) -> bool:
    """その Python が torch / soundfile / librosa / demucs を import できるか。"""
    if not Path(py).is_file():
        return False
    try:
        r = run([str(py), "-c", MV_IMPORTS], capture_output=True, timeout=300)    # 初回の import は遅いことがある(Windows の Defender など)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def show_mv_import_error(py: Path) -> None:
    """import できない原因(トレースバック)を、そのまま表示する。"""
    try:
        subprocess.call([str(py), "-c", MV_IMPORTS])
    except OSError as e:
        say(f"  実行できませんでした: {e}")


def setup_mv(cfg: Settings, models: bool = True) -> bool:
    """MV の音声処理の道具を components/mvtools/.venv に用意する。Demucs と librosa を import できる venv があれば、導入は飛ばす
    (重みの取得だけは毎回流す。取得済みなら数秒で終わり、前回の失敗からの再開にもなる)。
    [mv] tools_python に自分の Python を書いてあるときは、そこを使い、何も入れない。"""
    from studio import mv_audio
    say(f"\n>>> {MV_LABEL}")
    ffmpeg_hint = dict(missing_tools()).get("ffmpeg")
    if ffmpeg_hint:
        say(f"  ※ ffmpeg が PATH にありません。MV の音声の変換と切り出しに要ります → {ffmpeg_hint}")
    if mv_audio.configured_python(cfg):
        try:
            py = mv_audio.tools_python(cfg)
        except mv_audio.MvToolsMissing as e:
            say(f"失敗しました: {e.message}")
            return False
        if not mv_ready(py):
            say(f"失敗しました: [mv] tools_python の Python({py})に Demucs / librosa などが入っていません。"
                "入れるか、tools_python の行を消して setup --mv をやり直してください")
            show_mv_import_error(py)
            return False
        say(f"  既存を使います: {py}([mv] tools_python。導入も重みの取得も飛ばします)")
        return True
    tdir = mv_audio.tools_dir(cfg)
    venv = tdir / ".venv"
    py = mv_audio.venv_python(venv)
    if mv_ready(py):
        say(f"  準備済みです: {py}(Demucs と librosa が入っています。導入は飛ばします)")
    else:
        if not ensure_uv_on_path():
            say("uv が見つかりません。setup.sh / setup.bat から実行してください(uv を入れます)。")
            return False
        say(f"  ※ {mv_notice()}")
        tdir.mkdir(parents=True, exist_ok=True)
        for step in mv_steps(venv, py, have_venv=py.is_file()):
            if not run_step(step, tdir):
                return False
        if not mv_ready(py):
            say("失敗しました: 導入は終わりましたが、torch / librosa / demucs を読み込めません。原因:")
            show_mv_import_error(py)
            return False
    if models:
        return run_step(mv_warmup_step(py), tdir)
    say("  Demucs の重み(約 80MB)は取得しません(--no-models)。MV の分離を最初に使うときに取得します")
    return True


def prefetch_llm(cfg: Settings) -> bool:
    """同梱 LLM(llama-server と GGUF)を、管理役と同じ関数で先に取得する。"""
    from studio import llm
    say("\n>>> 同梱 LLM(llama.cpp + Gemma 4 E4B、約 5GB)")
    try:
        gpu = llm.use_gpu(llm.parse_gpu_setting(cfg.llm["gpu"]))
        exe = llm.find_binary(cfg, gpu)
        if exe is None:
            exe = llm.install_binary(llm.binary_dir(llm.tools_dir_of(cfg), gpu), gpu)
        say(f"  llama-server: {exe}")
        gguf = llm.find_gguf(cfg) or llm.download_model(cfg)
        say(f"  GGUF: {gguf}")
    except Exception as e:  # noqa: BLE001
        say(f"失敗しました: 同梱 LLM の取得({getattr(e, 'message', e)})")
        say("管理役は最初に使うときにも取得します。ネットワークを確かめて、setup --llm をやり直すこともできます。")
        return False
    return True


def print_summary(cfg: Settings, selected: list, mv: Optional[bool] = None) -> None:
    """mv: MV の音声処理の道具を対象にしたときの結果(None なら対象外)。"""
    say("\n== まとめ ==")
    for name in selected:
        d = cfg.component_dir(name)
        env_ok, models_ok = component_status(name, d)
        say(f"  {name:8} {COMPS[name].label}")
        say(f"           場所: {d}")
        say(f"           環境: {'あり' if env_ok else '未'}    モデル: "
            + ("不要(配布版)" if models_ok is None else "あり" if models_ok else "未"))
    if mv is not None:
        from studio import mv_audio
        say(f"  {MV_NAME:8} {MV_LABEL}")
        say(f"           場所: {mv_audio.tools_dir(cfg)}")
        say(f"           環境: {'あり' if mv else '未'}")
    start = ".\\run.bat" if is_win() else "./run.sh"
    say(f"\n起動: {start}   → ブラウザで http://{cfg.host}:{cfg.port}")
    if any(component_status(n, cfg.component_dir(n))[1] is False for n in selected):
        say("モデルが「未」の部品は、setup(--no-models なし)をもう一度実行すると続きから用意します。")


def parse_args(argv: list = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="h3-creator-studio のセットアップ(何度実行しても安全。続きから進みます)")
    p.add_argument("--no-models", action="store_true", help="環境だけ作り、モデルは取得しない")
    p.add_argument("--only", default="", help="対象の部品をカンマ区切りで(h3,h3image,ace,aivis。mvtools は MV の音声処理の道具だけを指す)")
    p.add_argument("--components-dir", default="", help="部品を取り込むフォルダ(既定は studio.toml の components_dir)")
    p.add_argument("--clone-anyway", action="store_true", help="~/<部品名> があっても components/ に取り込み直す")
    p.add_argument("--ace-xl", action="store_true", help="音楽に XL モデルも入れる(約 45GB。既定は 2B のみで約 15GB)")
    p.add_argument("--no-ref", action="store_true", help="h3 の参照モード用キャッシュ(transformer_ref、+約 32GB)を作らない")
    p.add_argument("--llm", action="store_true", help="同梱 LLM(llama-server と GGUF、約 5GB)も先に取得する")
    p.add_argument("--mv", action="store_true", help="MV の音声処理の道具(Demucs + librosa + CPU 版 torch、約 1.3GB)も用意する")
    return p.parse_args(argv)


def main(argv: list = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    args = parse_args(argv)
    cfg = load_settings(REPO_ROOT)
    if args.components_dir:
        cfg.components_dir = args.components_dir
        say(f"部品の取り込み先: {cfg.component_dir('h3').parent}(この場所を使い続けるなら studio.local.toml に components_dir = '...' を書いてください)")
    enabled = [n for n in ORDER if n in cfg.component_names()]
    selected, want_mv, bad = parse_only(args.only, enabled, args.mv)
    if bad:
        say(f"--only に使えない名前があります: {', '.join(bad)}(使えるのは {', '.join(enabled + [MV_NAME])})")
        return 2
    models = not args.no_models
    say(f"対象の部品: {', '.join(selected + ([MV_NAME] if want_mv else []))}" + ("   [環境だけ。モデルは取得しません]" if not models else ""))

    try:
        if selected and not check_prerequisites(selected, models, Path(cfg.root) / cfg.components_dir):
            say("\n足りないものを入れてから、setup をもう一度実行してください。")
            return 1
    except KeyboardInterrupt:
        return 130

    try:
        # 1. 取り込み(すべて先に済ませる。ここは短い)
        if selected:
            say("\n== 部品の取り込み ==")
        sources = {}
        for name in selected:
            src = decide_source(name, cfg, Path.home(), args.clone_anyway)
            sources[name] = src
            say(f"[{name}] {src.message}")
            if src.action == "clone":
                if not git_clone(COMPS[name].url, src.path):
                    return 1
            elif src.action == "download":
                try:
                    install_aivis(src.path)
                except Exception as e:  # noqa: BLE001
                    say(f"失敗しました: AivisSpeech の取得({e})")
                    say("ネットワークを確かめて、setup をもう一度実行してください。")
                    return 1

        # 2. 環境とモデル
        todo = [n for n in selected if sources[n].run_setup and COMPS[n].url]
        if todo:
            say("\n== 環境とモデルの準備 ==")
            if any(n in ("h3image", "ace") for n in todo):
                if not ensure_uv_on_path():
                    say("uv が見つかりません。setup.sh / setup.bat から実行してください(uv を入れます)。")
                    return 1
        for name in todo:
            d = sources[name].path
            say(f"\n[{name}] {COMPS[name].label}")
            if models and COMPS[name].notice:
                say(f"  ※ {COMPS[name].notice}")
            ensure_uv_on_path()
            extra_env = python312_env() if name == "h3" else {}
            have_venv = (d / ".venv" / ("Scripts/python.exe" if is_win() else "bin/python")).exists()
            for step in component_steps(name, models=models, ace_xl=args.ace_xl, h3_ref=not args.no_ref, have_venv=have_venv):
                step.env.update(extra_env)
                if not run_step(step, d):
                    return 1

        if args.llm and not prefetch_llm(cfg):
            return 1
        mv_ok = None
        if want_mv:
            mv_ok = setup_mv(cfg, models)
            if not mv_ok:
                return 1
    except KeyboardInterrupt:
        say("\n中断しました。setup をもう一度実行すると続きから進みます。")
        return 130

    print_summary(cfg, selected, mv_ok)
    return 0


if __name__ == "__main__":
    sys.exit(main())
