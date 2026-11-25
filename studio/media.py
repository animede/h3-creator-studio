"""動画・音声の補助: ffprobe で長さ・寸法を読み、ffmpeg で動画のサムネイルを作る。

ffmpeg / ffprobe が PATH に無くても失敗にはしない(サムネイルと長さが空になるだけ)。その旨は 1 回だけログに出す。
"""
import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Optional

log = logging.getLogger("studio.media")

TIMEOUT_S = 60
_warned: set = set()


def _tool(name: str) -> Optional[str]:
    path = shutil.which(name)
    if path is None and name not in _warned:
        _warned.add(name)
        log.warning("%s が見つかりません。動画のサムネイルと長さは表示されません(ffmpeg を PATH に入れてください)", name)
    return path


FPS_MIN, FPS_MAX = 1.0, 240.0


def frame_rate(text) -> Optional[float]:
    """ffprobe の avg_frame_rate / r_frame_rate("24/1"・"30000/1001"・"0/0")をコマ率にする。読めない・ありえない値は None。"""
    try:
        num, _, den = str(text or "").partition("/")
        fps = float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return None
    return round(fps, 3) if FPS_MIN <= fps <= FPS_MAX else None


def probe(path: Path) -> dict:
    """{"duration_s", "width", "height", "fps"} を返す。読めない項目は None。fps は動画の平均のコマ率(avg_frame_rate、無ければ r_frame_rate)。"""
    out = {"duration_s": None, "width": None, "height": None, "fps": None}
    exe = _tool("ffprobe")
    if exe is None:
        return out
    try:
        r = subprocess.run([exe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
                           capture_output=True, timeout=TIMEOUT_S)
        info = json.loads(r.stdout or b"{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        return out
    try:
        out["duration_s"] = float(info.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        pass
    for s in info.get("streams", []):
        if s.get("codec_type") == "video" and s.get("width"):
            out["width"], out["height"] = int(s["width"]), int(s["height"])
            out["fps"] = frame_rate(s.get("avg_frame_rate")) or frame_rate(s.get("r_frame_rate"))
            break
    return out


def video_thumb(src: Path, dst: Path, width: int, at_s: float = 0.5) -> bool:
    """動画の at_s 秒の 1 コマを幅 width の JPEG にする。できなければ False。"""
    exe = _tool("ffmpeg")
    if exe is None:
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    for t in (at_s, 0.0):    # 0.5 秒に届かない短い動画は先頭のコマにする
        try:
            subprocess.run([exe, "-v", "error", "-y", "-ss", str(t), "-i", str(src), "-frames:v", "1",
                            "-vf", f"scale={width}:-2", "-q:v", "4", str(dst)], capture_output=True, timeout=TIMEOUT_S)
        except (OSError, subprocess.SubprocessError):
            return False
        if dst.is_file() and dst.stat().st_size > 0:
            return True
    return False


def has_audio(path: Path) -> Optional[bool]:
    """動画に音声ストリームがあるか。ffprobe が無い・読めないときは None(呼び出し側は「ある」として扱う)。"""
    exe = _tool("ffprobe")
    if exe is None:
        return None
    try:
        r = subprocess.run([exe, "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
                           capture_output=True, timeout=TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    return bool(r.stdout.strip()) if r.returncode == 0 else None
