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


def probe(path: Path) -> dict:
    """{"duration_s", "width", "height"} を返す。読めない項目は None。"""
    out = {"duration_s": None, "width": None, "height": None}
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
