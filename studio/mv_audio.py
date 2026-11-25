"""MV の音声処理: 音声をそろえる・切る(ffmpeg)、ボーカルを分ける・曲をシーンに分ける(道具用の Python)。

管理役の venv は軽く保つため、torch や librosa はここに入れない。setup(既定)か `setup --only mvtools` が作る components/mvtools/.venv(CPU 版 torch、
約 1.2GB)の Python で、studio/mvtools/ のスクリプトを子プロセスとして動かす。`[mv] tools_python` で、Demucs と librosa の入った
別の Python に差し替えることもできる。子プロセスは asyncio.to_thread の中で subprocess を使う(Windows のイベントループの種類に
左右されないよう、asyncio.create_subprocess_* は使わない)。タスクが取り消されたら、子プロセスも(子孫ごと)止める。

  wav = await normalize(src, dst)                      # 任意の音声 → 44.1kHz ステレオの PCM WAV
  res = await separate(wav, out_dir, on_progress=cb)   # {"vocals": Path, "accompaniment": Path, "seconds": 31.8, "device": "cpu"}
  plan = await analyze(wav, res["vocals"], 5, 10, 8)   # {"duration": s, "bpm": x, "scenes": [{"index", "start", "end", ...}]}
  await cut(res["vocals"], scene["start"], scene["end"], clip)   # シーンごとのボーカルの切り出し

on_progress は子プロセスの stderr を 1 行ずつ受け取る(イベントループのスレッドで呼ぶ。コルーチン関数でもよい)。
separate.py は「progress 完了数/総数」の行を出すので、割合にするには progress_fraction(line) を使う。
"""
import asyncio
import collections
import inspect
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

import psutil

from . import config, media

log = logging.getLogger("studio.mv_audio")

SCRIPTS_DIR = Path(__file__).resolve().parent / "mvtools"
SEPARATE_SCRIPT = SCRIPTS_DIR / "separate.py"
ANALYZE_SCRIPT = SCRIPTS_DIR / "analyze.py"
MISSING_HINT = "MV タブの「道具を入れる」ボタンか setup --only mvtools で入れてください(または studio.local.toml の [mv] tools_python に Demucs と librosa の入った Python を指定)"
SAMPLE_RATE = 44100                 # 道具に渡す音声の標準(44.1kHz ステレオ)
LOG_TAIL_LINES = 40                 # 失敗したときに付ける stderr の末尾の行数
FFMPEG_TIMEOUT_S = 600
FFMPEG_TIMEOUT_PER_MEDIA_S = 20     # 長い動画を作り直す(テロップの焼き込みなど、再エンコードする)ffmpeg は、尺 1 秒あたりこれだけ待つ(10 分より短くはしない)
SEPARATE_TIMEOUT_S = 3600           # CPU の分離は、20 コアの PC で曲の長さの 0.5〜0.6 倍(実測)。4 コアの入門機は 3 倍前後と見込む
ANALYZE_TIMEOUT_S = 1800


class MvAudioError(Exception):
    """音声処理に失敗した。message は利用者向けの日本語、log_tail は子プロセスの stderr の末尾(原因の調査用)。"""

    def __init__(self, message: str, log_tail: str = ""):
        super().__init__(message)
        self.message, self.log_tail = message, log_tail


class MvToolsMissing(MvAudioError):
    """道具用の Python(Demucs と librosa 入り)が無い。message に対処の案内を含む。"""


# ---- 道具用の Python の場所 ----

def _settings(settings):
    return config.settings if settings is None else settings


def _mv_cfg(settings) -> dict:
    """設定の [mv] 表。Settings.mv(表そのもの)か、生の設定 Settings.data["mv"] のどちらかがあれば読む。無ければ空。"""
    for attr in ("mv", "data"):
        v = getattr(settings, attr, None)
        if attr == "data" and isinstance(v, dict):
            v = v.get("mv")
        if isinstance(v, dict):
            return v
    return {}


def tools_dir(settings=None) -> Path:
    """道具の置き場所: <リポジトリ直下>/<components_dir>/mvtools(setup が venv を作る所)。components_dir は絶対パスでもよい。"""
    s = _settings(settings)
    root = Path(getattr(s, "root", None) or config.REPO_ROOT)
    comp = Path(str(getattr(s, "components_dir", None) or "components")).expanduser()
    return (comp if comp.is_absolute() else root / comp) / "mvtools"


def venv_python(venv: Path, plat: str = None) -> Path:
    sub = ("Scripts", "python.exe") if (plat or sys.platform) == "win32" else ("bin", "python")
    return Path(venv, *sub)


def default_python(settings=None) -> Path:
    return venv_python(tools_dir(settings) / ".venv")


def configured_python(settings=None, override: str = "") -> str:
    """[mv] tools_python の値(引数 override が優先)。書かれていなければ空文字。"""
    return str(override or _mv_cfg(_settings(settings)).get("tools_python") or "").strip()


def tools_python(settings=None, override: str = "") -> Path:
    """道具用の Python の実行ファイル。override(呼び出し側が渡す) → [mv] tools_python → 既定の venv の順。

    [mv] tools_python は絶対パス、リポジトリ直下からの相対パス、PATH 上のコマンド名("python3" など)のどれでもよい。
    見つからなければ MvToolsMissing(対処の案内つき)。
    """
    s = _settings(settings)
    configured = configured_python(s, override)
    if configured:
        p = Path(configured).expanduser()
        p = p if p.is_absolute() else Path(getattr(s, "root", None) or config.REPO_ROOT) / p
        if p.is_file():
            return p
        found = shutil.which(configured)
        if found:
            return Path(found)
        raise MvToolsMissing(f"[mv] tools_python の Python が見つかりません: {configured}。{MISSING_HINT}")
    py = default_python(s)
    if not py.is_file():
        raise MvToolsMissing(f"MV の音声処理の道具(Demucs と librosa)が見つかりません: {py}。{MISSING_HINT}")
    return py


def tools_available(settings=None) -> bool:
    """道具用の Python があるか(中身までは確かめない。MV タブの案内の出し分け用)。"""
    try:
        tools_python(settings)
    except MvToolsMissing:
        return False
    return True


# ---- ffmpeg ----

def _ffmpeg() -> str:
    exe = media._tool("ffmpeg")
    if exe is None:
        raise MvAudioError("ffmpeg が見つかりません(PATH に入れてください)")
    return exe


def _no_window() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)     # Windows だけ。Linux では 0


def normalize_args(exe: str, src, tmp) -> list:
    return [exe, "-v", "error", "-y", "-i", str(src), "-vn", "-ac", "2", "-ar", str(SAMPLE_RATE),
            "-c:a", "pcm_s16le", "-f", "wav", str(tmp)]


def cut_args(exe: str, src, start: float, end: float, tmp, rate: int = 0, channels: int = 0, pad_to: float = 0.0) -> list:
    """src の start〜end 秒を PCM の WAV に切り出す。-ss を入力の前に置く(PCM はサンプル単位で正確に飛べる)。
    pad_to が長さを超えるときは、末尾を無音で pad_to 秒まで延ばす(H3 に渡す音声の最短の長さ向け)。"""
    length = end - start
    argv = [exe, "-v", "error", "-y", "-ss", f"{start:.3f}", "-i", str(src)]
    if pad_to > length:
        argv += ["-af", f"apad=whole_dur={pad_to:.3f}"]
        length = pad_to
    argv += ["-t", f"{length:.3f}"]
    if rate:
        argv += ["-ar", str(int(rate))]
    if channels:
        argv += ["-ac", str(int(channels))]
    return argv + ["-vn", "-c:a", "pcm_s16le", "-f", "wav", str(tmp)]


def _part(dst: Path) -> Path:
    return dst.with_name(dst.name + ".part")


def ffmpeg_timeout(media_s=None) -> float:
    """ffmpeg を待つ最長の秒数: 10 分か、作る動画・音声の尺(media_s 秒)の FFMPEG_TIMEOUT_PER_MEDIA_S 倍の長いほう。尺が分からなければ 10 分。"""
    try:
        return max(float(FFMPEG_TIMEOUT_S), FFMPEG_TIMEOUT_PER_MEDIA_S * float(media_s or 0))
    except (TypeError, ValueError):
        return float(FFMPEG_TIMEOUT_S)


def _run_ffmpeg(argv: list, dst: Path, what: str, timeout: float = None, run: "_Run" = None) -> Path:
    """ffmpeg(argv の出力先は _part(dst))を動かし、成功したら dst に置き換える(失敗しても壊れたファイルを残さない)。止まる処理なので別スレッドから呼ぶ。
    timeout は待つ最長の秒数(既定 10 分。長い動画の作り直しは ffmpeg_timeout で延ばす)。run(_Run)を渡せば、ほかのスレッドから run.stop() で
    止められる(ジョブの中止・管理役の停止。止めたら「…を中止しました」の MvAudioError)。"""
    tmp = _part(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    limit = float(FFMPEG_TIMEOUT_S if timeout is None else timeout)
    run = run if run is not None else _Run()
    with run.lock:
        if run.cancelled:
            raise MvAudioError(f"{what}を中止しました")
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=_no_window())
        except OSError as e:
            raise MvAudioError(f"{what}に失敗しました: ffmpeg を起動できません({e})") from e
        run.proc = proc
    try:
        _, err = proc.communicate(timeout=limit)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        proc.communicate()
        tmp.unlink(missing_ok=True)
        raise MvAudioError(f"{what}が {limit / 60:.0f} 分を超えたので止めました(ffmpeg)") from None
    if run.cancelled:
        tmp.unlink(missing_ok=True)
        raise MvAudioError(f"{what}を中止しました")
    err = (err or b"").decode("utf-8", errors="replace").strip()
    if proc.returncode != 0 or not tmp.is_file() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        last = err.splitlines()[-1] if err else f"終了コード {proc.returncode}"
        raise MvAudioError(f"{what}に失敗しました(ffmpeg): {last}", "\n".join(err.splitlines()[-LOG_TAIL_LINES:]))
    os.replace(tmp, dst)
    return dst


async def _stoppable(fn, run: "_Run"):
    """止まる処理 fn() を別スレッドで走らせて待つ。待っている側が取り消されたら(ジョブの中止・管理役の停止)、run.stop() で子プロセスを止め、
    スレッドが終わるのを(最長 10 秒)待ってから取り消しを通す(止めた ffmpeg が掴んでいる作りかけのファイルを、呼び出し側の後片づけが消せるように。
    Windows は開いているファイルを消せない)。以前は取り消しても ffmpeg は最後まで(最長 10 分)走り続け、管理役の停止もそれを待っていた。"""
    fut = asyncio.ensure_future(asyncio.to_thread(fn))
    try:
        return await asyncio.shield(fut)
    except asyncio.CancelledError:
        run.stop()
        await asyncio.wait({fut}, timeout=10)
        if fut.done() and not fut.cancelled():
            fut.exception()                 # 止めたことによる失敗は、取り消しの側で扱う(「取り出されなかった例外」の警告を出さない)
        raise


async def run_ffmpeg(argv: list, dst, what: str, media_s: float = None) -> Path:
    """_run_ffmpeg を別スレッドで走らせ、取り消されたら ffmpeg も止める。media_s は作る動画・音声の尺(秒。分かれば、待つ最長を延ばす)。"""
    run, dst = _Run(), Path(dst)
    return await _stoppable(lambda: _run_ffmpeg(argv, dst, what, timeout=ffmpeg_timeout(media_s), run=run), run)


def normalize_sync(src, dst, run: "_Run" = None) -> Path:
    dst = Path(dst)
    return _run_ffmpeg(normalize_args(_ffmpeg(), src, _part(dst)), dst, "音声の変換", run=run)


def cut_sync(src, start: float, end: float, dst, rate: int = 0, channels: int = 0, pad_to: float = 0.0, run: "_Run" = None) -> Path:
    if not end > start >= 0:
        raise MvAudioError(f"切り出す範囲が正しくありません: {start} 〜 {end} 秒")
    dst = Path(dst)
    return _run_ffmpeg(cut_args(_ffmpeg(), src, start, end, _part(dst), rate, channels, pad_to), dst, "音声の切り出し", run=run)


async def normalize(src, dst) -> Path:
    """任意の音声(mp3 / wav / m4a / 動画の音声など)を 44.1kHz ステレオの PCM WAV にして dst に書く。取り消されたら ffmpeg も止める。"""
    run = _Run()
    return await _stoppable(lambda: normalize_sync(src, dst, run=run), run)


async def cut(src_wav, start: float, end: float, dst, *, rate: int = 0, channels: int = 0, pad_to: float = 0.0) -> Path:
    """src_wav の start〜end 秒を PCM の WAV にして dst に書く(シーンごとのボーカルの切り出し)。取り消されたら ffmpeg も止める。

    rate / channels を 0 にすると元のまま。H3 に渡す音声を 16kHz モノラルにしたいときは rate=16000, channels=1。
    """
    run = _Run()
    return await _stoppable(lambda: cut_sync(src_wav, start, end, dst, rate, channels, pad_to, run=run), run)


# ---- 道具のスクリプトを子プロセスで動かす ----

_PROGRESS = re.compile(r"^progress (\d+)/(\d+)\b")


def progress_fraction(line: str) -> Optional[float]:
    """separate.py の「progress 完了数/総数」の行 → 0〜1。進み具合の行でなければ None。"""
    m = _PROGRESS.match(line.strip())
    if not m or int(m.group(2)) <= 0:
        return None
    return min(1.0, int(m.group(1)) / int(m.group(2)))


def _env() -> dict:
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    for k in ("PYTHONHOME", "PYTHONPATH"):         # 管理役の環境の設定が、道具用の venv のパッケージを隠さないように
        env.pop(k, None)
    return env


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        for child in psutil.Process(proc.pid).children(recursive=True):
            child.kill()
    except psutil.Error:
        pass
    try:
        proc.kill()
    except OSError:
        pass


class _Run:
    """スレッドの中で動く子プロセスを、取り消し・時間切れで外から止めるための入れ物。"""

    def __init__(self):
        self.proc = None
        self.cancelled = False
        self.timed_out = False
        self.lock = threading.Lock()

    def stop(self, timed_out: bool = False) -> None:
        with self.lock:
            self.cancelled = self.cancelled or not timed_out
            self.timed_out = self.timed_out or timed_out
            proc = self.proc
        if proc is not None:
            _kill_tree(proc)


def _sink(on_progress: Optional[Callable]) -> Callable:
    """別スレッドから呼ばれる「行の受け取り」。イベントループ側で on_progress を呼ぶ(コルーチン関数も可)。"""
    if on_progress is None:
        return lambda line: None
    loop = asyncio.get_running_loop()
    pending: set = set()

    def deliver(line: str) -> None:
        try:
            r = on_progress(line)
            if inspect.isawaitable(r):
                t = asyncio.ensure_future(r)
                pending.add(t)
                t.add_done_callback(pending.discard)
        except Exception:  # noqa: BLE001  進み具合の表示の失敗で、処理そのものを止めない
            log.exception("on_progress が失敗しました")

    def push(line: str) -> None:
        try:
            loop.call_soon_threadsafe(deliver, line)
        except RuntimeError:                       # イベントループが閉じた後
            pass
    return push


def _run_blocking(argv: list, run: _Run, push: Callable, timeout: float) -> tuple:
    with run.lock:
        if run.cancelled:
            raise MvAudioError("中止されました")
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    env=_env(), text=True, encoding="utf-8", errors="replace", bufsize=1,
                                    creationflags=_no_window())
        except OSError as e:
            raise MvAudioError(f"道具の Python を起動できません: {argv[0]}({e})") from e
        run.proc = proc
    tail: collections.deque = collections.deque(maxlen=LOG_TAIL_LINES)

    def pump() -> None:
        try:
            for raw in proc.stderr:                 # テキストモードなので、\r で書き換える進捗も 1 行ずつ読める
                line = raw.strip()
                if line:
                    tail.append(line)
                    push(line)
        except (OSError, ValueError):
            pass

    with proc:                                      # 抜けるときに stdout / stderr を閉じる
        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        timer = threading.Timer(timeout, run.stop, args=(True,))
        timer.daemon = True
        timer.start()
        try:
            out = proc.stdout.read()
            rc = proc.wait()
        finally:
            timer.cancel()
        reader.join(timeout=5)
    return rc, out, list(tail), run.timed_out


def parse_result(out: str) -> dict:
    """stdout の最後の(空でない)行を JSON として読む。ライブラリが途中で何か出力しても、結果の行だけを見る。"""
    lines = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
    if not lines:
        raise ValueError("出力が空です")
    data = json.loads(lines[-1])
    if not isinstance(data, dict):
        raise ValueError("結果が JSON のオブジェクトではありません")
    return data


def _reason(tail: list, rc: int) -> str:
    """失敗の理由: スクリプトが最後に書く「エラー: …」の行、無ければ stderr の最後の行。"""
    for line in reversed(tail):
        if line.startswith("エラー:"):
            return line[len("エラー:"):].strip()
    return tail[-1] if tail else f"終了コード {rc}"


async def _run_tool(script: Path, args: list, on_progress, python, settings, timeout: float, what: str) -> dict:
    py = Path(python) if python is not None else tools_python(settings)
    if python is not None and not py.is_file():
        raise MvToolsMissing(f"道具の Python が見つかりません: {py}。{MISSING_HINT}")
    argv = [str(py), str(script)] + [str(a) for a in args]
    run = _Run()
    push = _sink(on_progress)
    try:
        rc, out, tail, timed_out = await asyncio.to_thread(_run_blocking, argv, run, push, timeout)
    except asyncio.CancelledError:
        run.stop()                                  # 取り消されたら子プロセスも止める(スレッドは読み取りの終了で自然に終わる)
        raise
    log_tail = "\n".join(tail)
    if timed_out:
        limit = f"{timeout:g} 秒" if timeout < 60 else f"{timeout / 60:.0f} 分"
        raise MvAudioError(f"{what}が {limit}を超えたので止めました", log_tail)
    if rc != 0:
        raise MvAudioError(f"{what}に失敗しました: {_reason(tail, rc)}", log_tail)
    try:
        return parse_result(out)
    except ValueError as e:                         # json.JSONDecodeError も ValueError
        raise MvAudioError(f"{what}の結果を読めません({e})", log_tail) from e


async def separate(wav, out_dir, device: str = "cpu", on_progress: Optional[Callable] = None, *, model: str = "htdemucs",
                   python=None, settings=None, timeout: float = SEPARATE_TIMEOUT_S) -> dict:
    """wav(44.1kHz ステレオ)を Demucs でボーカルと伴奏に分け、out_dir に vocals.wav / accompaniment.wav を書く。

    device は "cpu" / "cuda" / "auto"。戻り値は {"vocals": Path, "accompaniment": Path, "seconds": 実行秒, "device": 使った装置}。
    """
    res = await _run_tool(SEPARATE_SCRIPT, [wav, out_dir, "--device", device, "--model", model], on_progress, python,
                          settings, timeout, "ボーカルの分離")
    try:
        out = {"vocals": Path(res["vocals"]), "accompaniment": Path(res["accompaniment"]),
               "seconds": res.get("seconds"), "device": res.get("device")}
    except (KeyError, TypeError) as e:
        raise MvAudioError(f"ボーカルの分離の結果を読めません({e!r})") from e
    for key in ("vocals", "accompaniment"):
        if not out[key].is_file():
            raise MvAudioError(f"ボーカルの分離の結果のファイルがありません: {out[key]}")
    return out


async def analyze(song_wav, vocals_wav, min_s: float = 5.0, max_s: float = 10.0, target_s: float = 8.0, *,
                  on_progress: Optional[Callable] = None, python=None, settings=None, timeout: float = ANALYZE_TIMEOUT_S) -> dict:
    """曲(song_wav)と分離したボーカル(vocals_wav)から、曲を min_s〜max_s 秒のシーンに分ける。

    戻り値は {"duration": 秒, "bpm": テンポ, "has_vocals": 歌が入っているか, "scenes": [{"index", "start", "end", "duration",
    "vocal_ratio", "kind": "vocal" | "instrumental", "boundary_kind", "boundary_score"}, ...]}。
    シーンは [0, duration] をちょうど覆い、(曲が min_s より短いときの 1 シーンを除いて)どれも min_s〜max_s 秒に収まる。
    """
    res = await _run_tool(ANALYZE_SCRIPT, [song_wav, vocals_wav, "--min", min_s, "--max", max_s, "--target", target_s],
                          on_progress, python, settings, timeout, "曲のシーン分割")
    if not isinstance(res.get("scenes"), list) or "duration" not in res:
        raise MvAudioError("曲のシーン分割の結果を読めません(scenes / duration がありません)")
    return res
