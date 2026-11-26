#!/usr/bin/env python
"""YUZU Creator Studio のセットアップ(setup.sh / setup.bat から呼ばれる)。

エンジン(minimax-h3 / minimax-h3-image-server / ACE-Step-1.5-fast / AivisSpeech-Engine)を components/ に取り込み、
それぞれ自分のセットアップスクリプトで環境とモデルを用意する。何度実行してもよく、済んでいる所は飛ばし、
途中で止まっても再実行で続きから進む。

  python scripts/setup.py                 # 取り込み + 環境 + モデル + MV の音声処理の道具(合計 約 160GB)
  python scripts/setup.py --no-models     # 環境だけ(モデルは後で)
  python scripts/setup.py --only h3,ace   # 指定したエンジンだけ
  python scripts/setup.py --llm           # 同梱 LLM(llama-server + Gemma 4 E4B + 画像・音声を読む投影ファイル 約 0.56GB)も先に取得する
  python scripts/setup.py --no-mv         # MV の音声処理の道具(Demucs + librosa、CPU 版 torch。約 1.3GB。既定で入れる)を入れない
  python scripts/setup.py --only mvtools  # その道具だけ(--only には h3,mvtools のように混ぜて書ける。--only のときは書いた分だけ)
  (--mv は以前の名残。既定で入れるので、付けても何も変わらない)
"""
from __future__ import annotations

import argparse
import os
import re
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

from studio import h3cache  # noqa: E402
from studio.config import Settings, load_settings  # noqa: E402

AIVIS_VERSION = "1.2.0"
AIVIS_BASE = f"https://github.com/Aivis-Project/AivisSpeech-Engine/releases/download/{AIVIS_VERSION}/"
MIN_RAM_GIB = 30           # 32GB の PC は 31 GiB 台で見えることがあるので、少し下に線を引く
MIN_VRAM_GIB = 5.8         # 6GB のカードは 5.9〜6.0 GiB で見える
NEED_DISK_GIB = 160            # 4 つのエンジンのモデル(約 130GB。動画の参照モード用キャッシュ 32GB を含む)と Python 環境(約 25GB)の合計
NEED_DISK_NO_REF_GIB = 130     # --no-ref のとき


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

# MV の音声処理の道具(components/mvtools/.venv)。エンジンではなく、管理役が子プロセスで使う Python 環境(studio/mv_audio.py)
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


def disk_warning(free_gib: float, models: bool, ref: bool = True) -> Optional[str]:
    need = NEED_DISK_GIB if ref else NEED_DISK_NO_REF_GIB
    if models and free_gib < need:
        return f"ディスクの空きが {free_gib:.0f}GB です。モデルまで入れると約 {need}GB 要ります"
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


# 既に入れてあるエンジンを探すときのフォルダ名(リポジトリ名の別名も)
REPO_ALIASES = {"h3": ("minimax-h3", "Diffusers_minimax-h3"), "h3image": ("minimax-h3-image-server",),
                "ace": ("ACE-Step-1.5-fast", "ACE-Step-1.5", "ACE-Step"), "aivis": ("AivisSpeech-Engine", "AivisSpeech")}


def existing_candidates(name: str, repo: str, home: Path, plat: str = None) -> list:
    """既に入れてあるかもしれない場所: ホーム直下・ホームの AI / ai / Documents・(Windows)各ドライブの直下と AI フォルダ。"""
    names = [repo, *[a for a in REPO_ALIASES.get(name, ()) if a != repo]]
    bases = [Path(home), *(Path(home) / d for d in ("AI", "ai", "Documents", "github", "src"))]
    if is_win(plat):
        for drive in "CDEF":
            root = Path(f"{drive}:/")       # WindowsPath では C:\ になる
            bases += [root, root / "AI", root / "ai"]
    out = []
    for b in bases:
        for n in names:
            c = b / n
            if c not in out:
                out.append(c)
    return out


def looks_installed(name: str, d: Path, plat: str = None) -> bool:
    """そのフォルダが、そのエンジンを入れたものらしいか(AivisSpeech は実行ファイル、他は git の取り込み + Python 環境)。"""
    try:
        if not d.is_dir():
            return False
        if name == "aivis":
            return find_aivis_exe(d, plat) is not None
        return (d / ".git").exists() and any((d / v).is_dir() for v in ("venv", ".venv"))
    except OSError:
        return False


def write_local_dir(local_toml: Path, name: str, path: Path) -> None:
    """studio.local.toml に [components.<name>] dir = '...' を足す(無ければ作る)。同じ節があれば、その節の dir の行を置き換える。"""
    lines = local_toml.read_text(encoding="utf-8").splitlines() if local_toml.is_file() else []
    head, entry = f"[components.{name}]", f"dir = '{path}'"
    if head in (ln.strip() for ln in lines):
        i = next(k for k, ln in enumerate(lines) if ln.strip() == head)
        j = next((k for k in range(i + 1, len(lines)) if lines[k].lstrip().startswith("[")), len(lines))
        body = [ln for ln in lines[i + 1:j] if not re.match(r"\s*dir\s*=", ln)]
        lines = lines[:i + 1] + [entry] + body + lines[j:]
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines += [head, entry]
    local_toml.write_text("\n".join(lines) + "\n", encoding="utf-8")


def ask_yes(question: str, default: bool = True) -> bool:
    """端末で [Y/n] を聞く。端末でなければ(自動実行)既定にしない: False。"""
    if not sys.stdin or not sys.stdin.isatty():
        return False
    try:
        ans = input(f"{question} [{'Y/n' if default else 'y/N'}] ").strip().lower()
    except EOFError:
        return False
    return default if not ans else ans in ("y", "yes", "はい")


def decide_source(name: str, cfg: Settings, home: Path, clone_anyway: bool = False, plat: str = None) -> Source:
    """エンジン 1 つについて「既存を使う / 取り込み済み / 取り込む」を決める。何もダウンロードしない。"""
    comp = COMPS[name]
    target = cfg.component_dir(name)
    if cfg.components.get(name, {}).get("dir"):
        if target.is_dir():
            return Source("existing", target, f"既存を使います: {target}(取り込みもモデル取得も飛ばします)", False)
        return Source("missing-dir", target,
                      f"studio.local.toml の [components.{name}] dir のフォルダがありません: {target}\n"
                      "  パスを直すか、dir の行を消して setup をやり直してください(このエンジンは飛ばします)", False)
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
    in_home = next((c for c in existing_candidates(name, repo, home, plat) if looks_installed(name, c, plat)), None)
    if in_home is not None:
        msg = (f"{in_home} が見つかりました。これを使うなら studio.local.toml に dir を書いてください(例):\n"
               + "\n".join("    " + ln for ln in toml_dir_example(name, in_home).splitlines())
               + "\n  新しく取り込み直すなら --clone-anyway を付けて setup をやり直してください(このエンジンは飛ばします)")
        if not clone_anyway:
            return Source("home", in_home, msg, False)
    return Source("download" if not comp.url else "clone", target,
                  f"取り込みます: {comp.url or aivis_url()} → {target}", True)


# ---- エンジンごとのコマンド(純粋な部分) ----

@dataclass
class Step:
    label: str
    argv: list
    cwd: str = "."                               # エンジンのフォルダからの相対
    env: dict = field(default_factory=dict)


def format_command(argv: list, plat: str = None) -> str:
    return subprocess.list2cmdline(argv) if is_win(plat) else shlex.join(argv)


def component_steps(name: str, models: bool = True, ace_xl: bool = False, have_venv: bool = False,
                    plat: str = None, h3_ref: bool = True) -> list:
    """環境とモデルの準備に呼ぶコマンド(エンジン自身のスクリプト)。相対パスはエンジンのフォルダ基準。"""
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
        # --index-strategy unsafe-best-match: requirements の --extra-index-url(PyTorch の配布元)に無い wheel(Windows の torchao など)を
        # PyPI からも探す(pip と同じ探し方。uv の既定は最初に見つかった取得先だけを見るので、Windows で依存が 1 つも入らなかった)
        steps.append(Step("h3image: 依存パッケージ(uv pip install)", ["uv", "pip", "install", "--python", py, "-r", req,
                                                                 "--index-strategy", "unsafe-best-match"]))
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


# ---- 既に入れてあるエンジン(h3)の量子化キャッシュ ----
# setup は既存のエンジンの取り込みもモデルの準備もしないが、Windows と RAM 48GB 未満の PC では、エンジンが重みを量子化キャッシュの
# ファイルから流すので、キャッシュが無いと動かない(参照モード用が無いと、MV は曲の分離と演出に数分かけた後に最初のシーンで断られる。
# 2026-11-26、Windows の実機の報告)。そこで有無を確かめ、足りなければ作るか聞く。判定と場所は studio/h3cache.py(管理役と同じ)。

H3_CACHE_LABEL = {"base": "本体(transformer_int8、約 60GB。T2VA・FL2VA・T2I)",
                  "ref": "参照モード用(transformer_ref_int8、約 32GB。MV・参照画像のショート動画・動画タブの参照モード)"}
H3_CACHE_DISK_GB = {"base": 60, "ref": 37}      # 作るのに要るディスクの空き(参照モード用は書く 32GB + 取得中のシャード 1 つ分)


def h3_venv_python(h3_dir: Path, plat: str = None) -> Optional[Path]:
    """エンジン(h3)の venv の python(studio.components と同じく venv → .venv の順)。無ければ None。"""
    sub = "Scripts/python.exe" if is_win(plat) else "bin/python"
    return next((p for p in (Path(h3_dir) / v / sub for v in ("venv", ".venv")) if p.is_file()), None)


def h3_cache_status(h3_dir: Path, want_ref: bool = True, env: dict = None, plat: str = None) -> dict:
    """既に入れてあるエンジン(h3)の量子化キャッシュ: {"base": 本体があるか, "ref": 参照モード用があるか, "venv": エンジンの venv の python か None,
    "missing": 作るもの(want_ref が False なら参照モード用は数えない), "dir": キャッシュの置き場所}。置き場所はエンジンと同じく、環境変数
    H3_TRANSFORMER_PREQUANT_DIR / H3_TE_PREQUANT_DIR があればそこ(env を渡さなければこのプロセスの環境)、無ければ <エンジン>/models/prequant。
    できあがりの印は、フォルダの config.json と meta.json(作りかけは <名前>.tmp にあるので数えない)。"""
    st = h3cache.cache_status(h3_dir, env)
    missing = [k for k in ("base", "ref") if not st[k] and (k == "base" or want_ref)]
    return {"base": st["base"], "ref": st["ref"], "venv": h3_venv_python(h3_dir, plat), "missing": missing, "dir": st["dir"]}


def h3_cache_step(h3_dir: Path, venv: Path, want_ref: bool = True, env: dict = None, plat: str = None) -> Step:
    """足りないキャッシュを作る手順: エンジンの venv の python で scripts/prepare_lowvram.py(+ --ref)。できているキャッシュは prepare が飛ばす。
    環境は component_steps の h3 のモデルの準備と同じ(Windows は HF_HUB_DISABLE_SYMLINKS。python312_env は install.sh 用なので要らない)。
    キャッシュの置き場所を環境変数で変えているときは、prepare にも同じ場所を H3_TE_PREQUANT_DIR で渡す(prepare はそれだけを見る)。"""
    win = is_win(plat)
    step_env = {"HF_HUB_DISABLE_SYMLINKS": "1"} if win else {}
    moved = h3cache.moved_prequant_dir(h3_dir, env)
    if moved:
        step_env["H3_TE_PREQUANT_DIR"] = str(moved)
    script = "scripts\\prepare_lowvram.py" if win else "scripts/prepare_lowvram.py"
    return Step("h3: 量子化キャッシュの準備(scripts/prepare_lowvram.py)", [str(venv), script] + (["--ref"] if want_ref else []), env=step_env)


def plan_existing_h3(h3_dir: Path, want_ref: bool = True, env: dict = None, plat: str = None) -> Optional[Step]:
    """既に入れてあるエンジン(h3)の量子化キャッシュを確かめて知らせ、足りなければ作るか聞く(既定は「はい」)。作るなら、その手順を返す
    (走らせるのは、ほかのエンジンの準備と同じ段で)。聞かないで None を返すのは: 足りている / エンジンの venv が無い(先に install)/
    この PC ではエンジンが起動時に bf16 から作れる(Linux で RAM 48GB 以上。ここで prepare を走らせると、エンジンが使う bf16 の重みを
    消してしまうので勧めない)/ 端末でない・「いいえ」(作るコマンドを出す)。env はエンジンが受け取る環境(既定はこのプロセスの環境)。"""
    env = os.environ if env is None else env
    st = h3_cache_status(h3_dir, want_ref, env, plat)
    if not st["missing"]:
        say(f"  キャッシュ: 本体 {'✓' if st['base'] else '未'} 参照用 " + ("✓" if st["ref"] else "未(--no-ref なので作りません)"))
        return None
    say(f"  量子化キャッシュがありません(場所: {st['dir']}):")
    for k in st["missing"]:
        say(f"    - {H3_CACHE_LABEL[k]}")
    if st["venv"] is None:
        say("  先にエンジンの install を実行してください(venv がありません)。終わってから setup をもう一度実行すると、キャッシュを作るか聞きます:")
        say(f"    cd {h3_dir}")
        say("    " + format_command(component_steps("h3", models=False, plat=plat)[0].argv, plat))
        return None
    if not h3cache.has_prepare_script(h3_dir):
        say("  このエンジンは古く、キャッシュを作る scripts/prepare_lowvram.py がありません。README の「エンジンの更新(git pull)」で"
            "更新してから、setup をもう一度実行してください")
        return None
    if not (h3cache.group_offload(env) and h3cache.file_streaming(env, plat)):
        say("  この PC では、エンジンが起動時に bf16 の重みから作れるので、無くても動きます。先に作っておくなら(bf16 の重みは消さずに残します):")
        say("    " + h3cache.prepare_command(h3_dir, env, plat, ref=want_ref, keep_source=True))
        return None
    say("  この PC(Windows、または RAM 48GB 未満)では、エンジンは重みをこのキャッシュのファイルから流すので、無いと使えません")
    say(f"  作るには数時間かかります(重みをシャード 1 つずつ取得して量子化し、取得した分は消す。ディスクの空き 約 "
        f"{sum(H3_CACHE_DISK_GB[k] for k in st['missing'])}GB、RAM 16GB で作れます。GPU は使いません)")
    if not ask_yes("  いま作りますか?"):
        say("  作らずに進みます。後で作るなら(スタジオを止めてから):")
        say("    " + h3cache.prepare_command(h3_dir, env, plat, ref=want_ref))
        return None
    return h3_cache_step(h3_dir, st["venv"], want_ref, env, plat)


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


def check_prerequisites(selected: list, models: bool, free_path: Path, ref: bool = True) -> bool:
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
    for w in resource_warnings(ram, gpu[1] if gpu else None) + [x for x in [disk_warning(free, models, ref)] if x]:
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


def parse_only(only: str, enabled: list, no_mv: bool = False) -> tuple:
    """--only の値 → (対象のエンジン, MV の音声処理の道具も対象か, 使えない名前)。空なら全部のエンジンと道具(--no-mv なら道具は除く)。
    --only があるときは書いた分だけ(--no-mv は見ない)。mvtools はエンジンではなく道具を指す(--only mvtools だけならエンジンは何も触らない)。"""
    if not only.strip():
        return list(enabled), not no_mv, []
    want = [x.strip() for x in only.split(",") if x.strip()]
    bad = [x for x in want if x != MV_NAME and x not in enabled]
    return [n for n in ORDER if n in want and n in enabled], MV_NAME in want, bad


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
                "入れるか、tools_python の行を消して setup --only mvtools をやり直してください")
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
    """同梱 LLM(llama-server と GGUF と、画像・音声を読む投影ファイル)を、管理役と同じ関数で先に取得する。"""
    from studio import llm
    say("\n>>> 同梱 LLM(llama.cpp + Gemma 4 E4B、約 5GB + 画像・音声を読む投影ファイル 約 0.56GB)")
    try:
        gpu = llm.use_gpu(llm.parse_gpu_setting(cfg.llm["gpu"]))
        exe = llm.find_binary(cfg, gpu)
        if exe is None:
            exe = llm.install_binary(llm.binary_dir(llm.tools_dir_of(cfg), gpu), gpu)
        say(f"  llama-server: {exe}")
        gguf = llm.find_gguf(cfg) or llm.download_model(cfg)
        say(f"  GGUF: {gguf}")
        mmproj = llm.find_mmproj(cfg, gguf) or llm.download_mmproj(cfg)
        say(f"  画像・音声を読む投影ファイル: {mmproj}")
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
        say("モデルが「未」のエンジンは、setup(--no-models なし)をもう一度実行すると続きから用意します。")


def parse_args(argv: list = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="YUZU Creator Studio のセットアップ(何度実行しても安全。続きから進みます)")
    p.add_argument("--no-models", action="store_true", help="環境だけ作り、モデルは取得しない")
    p.add_argument("--only", default="", help="対象のエンジンをカンマ区切りで(h3,h3image,ace,aivis。mvtools は MV の音声処理の道具だけを指す)")
    p.add_argument("--components-dir", default="", help="エンジンを取り込むフォルダ(既定は studio.toml の components_dir)")
    p.add_argument("--clone-anyway", action="store_true", help="~/<エンジン名> があっても components/ に取り込み直す")
    p.add_argument("--ace-xl", action="store_true", help="音楽に XL モデルも入れる(約 45GB。既定は 2B のみで約 15GB)")
    p.add_argument("--no-ref", action="store_true", help="h3 の参照モード用キャッシュ(transformer_ref、+約 32GB)を作らない")
    p.add_argument("--llm", action="store_true", help="同梱 LLM(llama-server と GGUF、約 5GB。画像・音声を読む投影ファイル 約 0.56GB を含む)も先に取得する")
    p.add_argument("--no-mv", action="store_true", help="MV の音声処理の道具(Demucs + librosa + CPU 版 torch、約 1.3GB。既定で入れる)を入れない")
    p.add_argument("--mv", action="store_true", help="以前の名残(道具は既定で入れるので、何も変わらない)")
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
        say(f"エンジンの取り込み先: {cfg.component_dir('h3').parent}(この場所を使い続けるなら studio.local.toml に components_dir = '...' を書いてください)")
    enabled = [n for n in ORDER if n in cfg.component_names()]
    selected, want_mv, bad = parse_only(args.only, enabled, args.no_mv)
    if bad:
        say(f"--only に使えない名前があります: {', '.join(bad)}(使えるのは {', '.join(enabled + [MV_NAME])})")
        return 2
    models = not args.no_models
    say(f"対象のエンジン: {', '.join(selected + ([MV_NAME] if want_mv else []))}" + ("   [環境だけ。モデルは取得しません]" if not models else ""))

    try:
        if selected and not check_prerequisites(selected, models, Path(cfg.root) / cfg.components_dir, ref=not args.no_ref):
            say("\n足りないものを入れてから、setup をもう一度実行してください。")
            return 1
    except KeyboardInterrupt:
        return 130

    try:
        # 1. 取り込み(すべて先に済ませる。ここは短い)
        if selected:
            say("\n== エンジンの取り込み ==")
        sources = {}
        h3_build = None          # 既に入れてあるエンジン(h3)で、足りない量子化キャッシュを作る手順(作ると答えたときだけ。走らせるのは 2. で)
        for name in selected:
            src = decide_source(name, cfg, Path.home(), args.clone_anyway)
            if src.action == "home":
                say(f"[{name}] 既に入れてある {COMPS[name].label} が見つかりました: {src.path}")
                if ask_yes(f"  これを使いますか?(studio.local.toml に場所を書きます。取り込みもモデル取得もしません)"):
                    write_local_dir(Path(cfg.root) / "studio.local.toml", name, src.path)
                    src = Source("existing", src.path, f"既存を使います: {src.path}(studio.local.toml に書きました)", False)
            sources[name] = src
            say(f"[{name}] {src.message}")
            if name == "h3" and src.action == "existing" and models:
                h3_build = plan_existing_h3(src.path, want_ref=not args.no_ref, env=h3cache.engine_env(cfg))
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
        if todo or h3_build is not None:
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
        if h3_build is not None:
            say(f"\n[h3] {COMPS['h3'].label}(既存のエンジン: {sources['h3'].path})")
            say("  ※ 量子化キャッシュを作ります。数時間かかります(止めても、できあがったキャッシュは次に飛ばします。作りかけの分は最初から作り直します)")
            if not run_step(h3_build, sources["h3"].path):
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
