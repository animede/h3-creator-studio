"""AI 拡大(2 倍・3 倍・4 倍。v1.3.0)。方式の一覧・使えるかの判定・倍率の検証・拡大の実行をここにまとめる。
使う口は 2 つ: 編集タブの書き出し(handlers/edit.py)と、ギャラリーの動画・画像を 1 つ直接拡大するジョブ(handlers/upscale.py の upscale.output)。

方式は 2 つ。どちらも任意の道具で、編集タブの「入れる」(scripts/setup.py --only upscale-<方式>)で <tools_dir>(既定 components/upscale)に入れる。
  realesrgan : Real-ESRGAN ncnn-vulkan(Vulkan の動く GPU なら何でも。アニメ向けの realesr-animevideov3 だけ使う)。
               書き出した MP4 → ffmpeg でコマを PNG に → 実行ファイルがフォルダごと拡大 → ffmpeg で元の音声を付けて MP4
  rtxvsr     : NVIDIA RTX Video Super Resolution(PyPI の nvidia-vfx。NVIDIA の利用条件に同意が要るので同梱しない。画面で同意して入れる)。
               Tensor コアのある RTX(compute capability 7.5 以上)と 570 系以降のドライバだけ。scripts/upscale_rtxvsr.py を道具の venv の Python で動かし、
               音声は ffmpeg で元の MP4 から写す
"auto" は、使える方式のうち rtxvsr → realesrgan の順で先のもの。

実行(run_*)は止まる処理なので、ハンドラが asyncio.to_thread で呼ぶ。走っている子プロセスは procs[ジョブの ID] に入れ、ハンドラの中止の口
(handlers/edit.py の cancel_ffmpeg・handlers/upscale.py の cancel)がそこから止める。失敗しても作りかけ(.part・.part.png)とコマのフォルダは残さない。
動画は H.264(yuv420p)なので幅・高さを偶数に切り下げる(アップロードには奇数の寸法もある)。画像は拡大した寸法のまま。
"""
import asyncio
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import OrderedDict, deque
from pathlib import Path
from typing import Callable, Optional

from . import config, gpu, mv_audio
from .jobs import JobCancelled, JobFailed

log = logging.getLogger("studio.upscale")

AUTO = "auto"
MAX_EDGE_DEFAULT = 3840
RTXVSR_QUALITY_DEFAULT = "ULTRA"
NVIDIA_LICENSE_URL = "https://pypi.org/project/nvidia-vfx/"
# id → 画面の名前(ja / en)、倍率、同意の要る利用条件、道具の名前、setup の --only の対象、setup に足す引数
METHODS = OrderedDict([
    ("realesrgan", {"ja": "Real-ESRGAN(アニメ向け)", "en": "Real-ESRGAN (anime)", "scales": (2, 3, 4), "license_url": None,
                    "tool": "realesrgan", "target": "upscale-realesrgan", "install_args": ()}),
    ("rtxvsr", {"ja": "RTX Video Super Resolution", "en": "RTX Video Super Resolution", "scales": (2, 3, 4), "license_url": NVIDIA_LICENSE_URL,
                "tool": "rtxvsr", "target": "upscale-rtxvsr", "install_args": ("--accept-nvidia-license",)}),
])
AUTO_ORDER = ("rtxvsr", "realesrgan")            # auto で先に選ぶ順(RTX VSR のほうが速く、きれい)
REALESRGAN_RELEASE = {
    "linux": "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-ubuntu.zip",
    "win32": "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-windows.zip",
}
REALESRGAN_MODEL = "realesr-animevideov3"
RTXVSR_SCRIPT = config.REPO_ROOT / "scripts" / "upscale_rtxvsr.py"
RTXVSR_MIN_COMPUTE_CAP = 7.5                     # Turing(RTX 20)以降。Tensor コアの無い GTX では nvidia-vfx が動かない
RTXVSR_MIN_DRIVER = {"win32": (570, 65), "linux": (570, 190)}
SMI_FIELDS = ["name", "driver_version", "compute_cap"]
POLL_S = 0.5                                     # 子プロセスの進みを見る間隔
STALL_S = 600                                    # 進みがこれだけ止まったら、止まったと見て中止する(Vulkan のドライバが固まる、など)
LOG_TAIL_LINES = 40
GPU_WAIT_POLL_S = 1.0                            # GPU のエンジンの生成が終わるのを待つ間隔(wait_gpu_idle)
MSG_WAIT_GPU = "GPU の生成が終わるのを待っています"
WORK_DIRNAME = "upscale"                         # ギャラリーの拡大の作業フォルダ outputs/upscale/<ジョブの ID>(再起動のたびに丸ごと消す)
IMAGE_INPUTS = (".png", ".jpg", ".jpeg", ".webp")   # Real-ESRGAN の実行ファイルがそのまま読める画像。ほかは PNG にしてから渡す
AUDIO_COPY, AUDIO_AAC = "copy", "aac"            # 元の音声をそのまま写すか、AAC にし直すか(MP4 に入らない音声: webm の Vorbis など)


# ---- 置き場所 ----

def _settings(settings):
    return config.settings if settings is None else settings


def _cfg(settings) -> dict:
    """設定の [upscale] 表。Settings.upscale か、生の設定 Settings.data["upscale"]。無ければ空。"""
    for attr in ("upscale", "data"):
        v = getattr(settings, attr, None)
        if attr == "data" and isinstance(v, dict):
            v = v.get("upscale")
        if isinstance(v, dict):
            return v
    return {}


def tools_dir(settings=None) -> Path:
    """道具の置き場所: [upscale] tools_dir(既定 components/upscale。リポジトリ直下からの相対か絶対パス)。"""
    s = _settings(settings)
    root = Path(getattr(s, "root", None) or config.REPO_ROOT)
    p = Path(str(_cfg(s).get("tools_dir") or config.UPSCALE_DEFAULTS["tools_dir"])).expanduser()
    return p if p.is_absolute() else root / p


def max_edge(settings=None) -> int:
    try:
        return max(256, int(_cfg(_settings(settings)).get("max_edge") or MAX_EDGE_DEFAULT))
    except (TypeError, ValueError):
        return MAX_EDGE_DEFAULT


def rtxvsr_quality(settings=None) -> str:
    return str(_cfg(_settings(settings)).get("rtxvsr_quality") or RTXVSR_QUALITY_DEFAULT).strip().upper() or RTXVSR_QUALITY_DEFAULT


def realesrgan_dir(settings=None) -> Path:
    return tools_dir(settings) / "realesrgan"


def realesrgan_exe(settings=None, plat: str = None) -> Path:
    name = "realesrgan-ncnn-vulkan" + (".exe" if (plat or sys.platform) == "win32" else "")
    return realesrgan_dir(settings) / name


def rtxvsr_venv(settings=None) -> Path:
    return tools_dir(settings) / "rtxvsr" / ".venv"


def rtxvsr_python(settings=None, plat: str = None) -> Path:
    return mv_audio.venv_python(rtxvsr_venv(settings), plat)


# ---- 使えるか ----

def _version_tuple(text: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", str(text or "")))


def rtxvsr_gpu_ok(fields: Optional[list], plat: str = None) -> tuple:
    """nvidia-smi の [name, driver_version, compute_cap] から、RTX Video Super Resolution が動く GPU か → (使えるか, 使えない理由(日本語)か None)。
    Tensor コアのある Turing 以降(compute capability 7.5 以上)と、nvidia-vfx の要るドライバ(Windows 570.65 / Linux 570.190 以降)。"""
    plat = plat or sys.platform
    if not fields or len(fields) < 3:
        return False, "NVIDIA の GPU の情報を読めません(nvidia-smi が無いか古い)。RTX Video Super Resolution は NVIDIA の RTX だけで使えます"
    name, driver, cap = (str(x).strip() for x in fields[:3])
    try:
        cap_value = float(cap)
    except ValueError:
        return False, f"GPU の世代(compute capability)を読めません: {cap}"
    if cap_value < RTXVSR_MIN_COMPUTE_CAP:
        return False, f"{name} は RTX Video Super Resolution に対応していません(Tensor コアのある RTX 20 以降が要ります)"
    need = RTXVSR_MIN_DRIVER["win32" if plat == "win32" else "linux"]
    if _version_tuple(driver) < need:
        return False, f"NVIDIA のドライバが古いです({driver})。RTX Video Super Resolution には {'.'.join(map(str, need))} 以降が要ります"
    return True, None


def _gpu_fields() -> Optional[list]:
    return gpu.smi_fields(SMI_FIELDS)


def status(settings=None, smi: Callable[[], Optional[list]] = None, plat: str = None) -> dict:
    """方式ごとの、入っているか・使えるか・使えない理由(hint)。auto_method は auto で選ばれる方式(使えるものが無ければ None)。
    installable は画面の「入れる」を出すか(まだ入っていなくて、入れれば使える GPU)。"""
    s, plat = _settings(settings), plat or sys.platform
    smi = smi or _gpu_fields
    out, gpu_checked = [], None
    for mid, m in METHODS.items():
        label, hint, installable = m["ja"], None, False
        if m["tool"] == "realesrgan":
            exe = realesrgan_exe(s, plat)
            installed = exe.is_file()
            if not installed:
                if exe.parent.is_dir():
                    hint = f"{label} の実行ファイル(Vulkan)が見つかりません: {exe}。「入れる」でもう一度入れてください"
                else:
                    hint = f"{label}が入っていません。拡大の欄の「入れる」で入れてください"
                installable = True
        else:
            if gpu_checked is None:
                gpu_checked = rtxvsr_gpu_ok(smi(), plat)
            ok, reason = gpu_checked
            installed = rtxvsr_python(s, plat).is_file()
            if not ok:
                hint = reason
            elif not installed:
                hint = f"{label}が入っていません。拡大の欄の「入れる」で入れてください(NVIDIA の利用条件への同意が要ります)"
                installable = True
        out.append({"id": mid, "label": label, "label_en": m["en"], "scales": list(m["scales"]), "installed": installed,
                    "usable": installed and hint is None, "installable": installable, "hint": hint, "license_url": m["license_url"]})
    usable = {x["id"] for x in out if x["usable"]}
    return {"methods": out, "default": AUTO, "auto_method": next((mid for mid in AUTO_ORDER if mid in usable), None)}


def even_size(width: int, height: int) -> tuple:
    """H.264(yuv420p)で作れる寸法(偶数に切り下げる)。奇数なら右と下を 1px 削る(encode_args の crop・upscale_rtxvsr.py と同じ)。"""
    return int(width) - int(width) % 2, int(height) - int(height) % 2


def resolve(settings, method, scale, width: int, height: int, max_edge_px: int = None, st: dict = None, kind: str = "video") -> Optional[dict]:
    """拡大の指定を検証し、{"method": 実際の方式, "scale", "width", "height"(拡大後)} にする。倍率 1 か方式なしなら None(拡大しない)。
    不正・使えないなら ValueError(日本語)。st は status() の結果(渡さなければ調べる)。kind が video なら拡大後の寸法を偶数に切り下げる。"""
    if method in (None, "") or scale in (None, "", 1, "1"):
        return None
    try:
        scale = int(str(scale).strip())          # 2.5 のような半端な倍率は通さない(int(2.5) は 2 になる)
    except (TypeError, ValueError):
        raise ValueError("拡大の倍率は 2 / 3 / 4 のいずれかです") from None
    if scale == 1:
        return None
    method = str(method).strip().lower()
    if method != AUTO and method not in METHODS:
        raise ValueError(f"拡大の方式が不明です: {method}")
    st = st if st is not None else status(settings)
    by_id = {x["id"]: x for x in st["methods"]}
    if method == AUTO:
        method = st.get("auto_method")
        if not method:
            raise ValueError("拡大の方式が入っていません。拡大の欄の「入れる」で Real-ESRGAN か RTX Video Super Resolution を入れてください")
    elif not by_id[method]["usable"]:
        raise ValueError(by_id[method]["hint"] or f"{METHODS[method]['ja']}は使えません")
    if scale not in METHODS[method]["scales"]:
        raise ValueError(f"{METHODS[method]['ja']}の倍率は {' / '.join(map(str, METHODS[method]['scales']))} のいずれかです")
    w, h = int(width) * scale, int(height) * scale
    if kind == "video":
        w, h = even_size(w, h)
    limit = int(max_edge_px) if max_edge_px else max_edge(settings)
    if max(w, h) > limit:
        raise ValueError(f"拡大後の長辺が {limit}px を超えます({w}×{h})。大きさを下げてください")
    return {"method": method, "scale": scale, "width": w, "height": h}


def check_params(up, state: dict) -> dict:
    """ジョブの params["upscale"](EditManager が resolve した辞書)の形を確かめる(方式・倍率と、拡大後の寸法)。不正なら ValueError。"""
    if not isinstance(up, dict):
        raise ValueError("upscale は {method, scale} の辞書で指定してください")
    method = str(up.get("method") or "").strip().lower()
    if method not in METHODS:
        raise ValueError(f"拡大の方式が不明です: {method}")
    try:
        scale = int(str(up.get("scale")).strip())
    except (TypeError, ValueError):
        raise ValueError("拡大の倍率は 2 / 3 / 4 のいずれかです") from None
    if scale not in METHODS[method]["scales"]:
        raise ValueError(f"{METHODS[method]['ja']}の倍率は {' / '.join(map(str, METHODS[method]['scales']))} のいずれかです")
    return {"method": method, "scale": scale, "width": int(state["width"]) * scale, "height": int(state["height"]) * scale}


def gpu_busy(ctx) -> bool:
    """GPU のエンジンに仕事が割り当てられているか(scheduler の _busy。無ければ False)。拡大の道具は VRAM を 1GB ほど使うので、6〜8GB の GPU で
    MiniMax-H3 の生成の最中に動かすと生成のほうが OOM になりかねない。生成が終わるまで待つための判定。"""
    busy = getattr(getattr(ctx, "sched", None), "_busy", None)
    if not isinstance(busy, dict):
        return False
    return any(int(n or 0) > 0 for n in busy.values())


async def wait_gpu_idle(job, ctx) -> None:
    """GPU のエンジン(MiniMax-H3 など)の生成が終わるまで待つ(gpu_busy)。拡大の道具は VRAM を 1GB ほど使うので、6〜8GB の GPU で
    生成と重なると生成のほうが OOM になりかねない。待っている間も中止できる。編集の書き出しとギャラリーの拡大の両方が使う。"""
    waiting = False
    while gpu_busy(ctx):
        if job.cancel_requested:
            raise JobCancelled()
        if not waiting:
            job.message, waiting = MSG_WAIT_GPU, True
        await asyncio.sleep(GPU_WAIT_POLL_S)
    if job.cancel_requested:
        raise JobCancelled()


# ---- 引数(テストで確かめられる純粋な関数) ----

def _fps_text(fps) -> str:
    try:
        return f"{float(fps):.3f}".rstrip("0").rstrip(".") or "24"
    except (TypeError, ValueError):
        return "24"


def realesrgan_argv(exe, in_dir, out_dir, scale: int) -> list:
    """フォルダの画像を全部拡大する(同じ名前で out_dir に書く)。-m はモデルの場所(省くと作業フォルダ基準の models になり、見つからない)。"""
    exe = Path(exe)
    return [str(exe), "-i", str(in_dir), "-o", str(out_dir), "-n", REALESRGAN_MODEL, "-s", str(int(scale)), "-f", "png", "-m", str(exe.parent / "models")]


def rtxvsr_argv(python, src, tmp, scale: int, quality: str = RTXVSR_QUALITY_DEFAULT) -> list:
    return [str(python), str(RTXVSR_SCRIPT), str(src), str(tmp), "--scale", str(int(scale)), "--quality", str(quality)]


def realesrgan_image_argv(exe, src, dst_part, scale: int) -> list:
    """画像 1 枚を拡大する。出力の名前は .png で終える(実行ファイルが拡張子で形式を決め、.part のような名前は受け付けない)。"""
    exe = Path(exe)
    return [str(exe), "-i", str(src), "-o", str(dst_part), "-n", REALESRGAN_MODEL, "-s", str(int(scale)), "-f", "png", "-m", str(exe.parent / "models")]


def rtxvsr_image_argv(python, src, dst_part, scale: int, quality: str = RTXVSR_QUALITY_DEFAULT) -> list:
    return [str(python), str(RTXVSR_SCRIPT), "--image", str(src), str(dst_part), "--scale", str(int(scale)), "--quality", str(quality)]


def image_part(dst) -> Path:
    """拡大した画像の作りかけの名前: x_x2.png → x_x2.part.png(Real-ESRGAN は .png で終わる名前しか書かない。起動時の掃除は ledger.remove_stale_parts)。"""
    dst = Path(dst)
    return dst.with_name(dst.stem + ".part.png")


def _audio_args(audio: str) -> list:
    return ["-c:a", "aac", "-b:a", "192k"] if audio == AUDIO_AAC else ["-c:a", "copy"]


def extract_args(ffmpeg, src, in_dir) -> list:
    """書き出した MP4 の全コマを PNG(%06d.png)に。image2 の既定(passthrough)でコマを増減させない。"""
    return [str(ffmpeg), "-v", "error", "-y", "-progress", "pipe:1", "-nostats", "-i", str(src), str(Path(in_dir) / "%06d.png")]


def encode_args(ffmpeg, out_dir, src, dst_part, fps, audio: str = AUDIO_COPY) -> list:
    """拡大した PNG を元のコマ率で H.264 にし、元の動画の音声(あれば)を付ける(audio = copy はそのまま、aac は AAC にし直す)。出力は .part(成功したら置き換える)。
    crop は奇数の寸法を偶数に切り下げる(yuv420p は偶数しか作れない。奇数の寸法のアップロードを ×3 にした時など。偶数なら何もしない)。"""
    return [str(ffmpeg), "-v", "error", "-y", "-progress", "pipe:1", "-nostats", "-framerate", _fps_text(fps), "-i", str(Path(out_dir) / "%06d.png"),
            "-i", str(src), "-map", "0:v", "-map", "1:a?", "-vf", "crop=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:v", "libx264", "-crf", "14", "-preset", "medium", "-pix_fmt", "yuv420p",
            *_audio_args(audio), "-shortest", "-movflags", "+faststart", "-f", "mp4", str(dst_part)]       # 出力は .part なので -f mp4 で形式を指す


def mux_args(ffmpeg, video_only, src, dst_part, audio: str = AUDIO_COPY) -> list:
    """RTX VSR が作った映像だけの MP4 に、元の動画の音声(あれば)を写す(映像は再エンコードしない。audio = aac なら音声だけ AAC にし直す)。"""
    return [str(ffmpeg), "-v", "error", "-y", "-progress", "pipe:1", "-nostats", "-i", str(video_only), "-i", str(src),
            "-map", "0:v", "-map", "1:a?", "-c", "copy", *(_audio_args(audio) if audio == AUDIO_AAC else []),
            "-shortest", "-movflags", "+faststart", "-f", "mp4", str(dst_part)]


_PROGRESS_RE = re.compile(r"^\s*progress\s+(\d+)\s*/\s*(\d+)\s*$")


def parse_progress(line: str) -> Optional[tuple]:
    """道具のスクリプトの「progress 完了数/総数」の行 → (完了数, 総数)。関係ない行は None。"""
    m = _PROGRESS_RE.match(line or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def ffmpeg_frame(line: str) -> Optional[int]:
    """ffmpeg の -progress の frame=N の行 → N。"""
    key, _, value = (line or "").strip().partition("=")
    if key == "frame":
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def upscaled_path(dest, scale: int, taken=()) -> Path:
    """拡大した動画・画像の置き場所: edit_<ID>.mp4 → edit_<ID>_x2.mp4。あれば _2、_3…(前のものを上書きしない)。
    taken は、まだファイルの無い、ほかのジョブが書くことに決めた場所(同時に走る 2 つのジョブが同じ名前を選ばないように)。"""
    dest = Path(dest)
    path, n = dest.with_name(f"{dest.stem}_x{int(scale)}{dest.suffix}"), 1
    while path.exists() or path in taken:
        n += 1
        path = dest.with_name(f"{dest.stem}_x{int(scale)}_{n}{dest.suffix}")
    return path


def _count_pngs(d: Path) -> int:
    try:
        return sum(1 for f in os.scandir(d) if f.name.endswith(".png"))
    except OSError:
        return 0


# ---- 子プロセスの実行 ----

def _run(job, argv: list, procs: dict, on_line: Callable[[str], None] = None, tick: Callable[[], None] = None, env: dict = None) -> tuple:
    """子プロセス(ffmpeg・Real-ESRGAN・道具の Python)を走らせ、終わるまで待つ。procs[job.id] に登録し、中止はそこから proc.kill() で止める。
    stdout の 1 行ごとに on_line(行)、POLL_S ごとに tick()。戻りは (終了コード, stderr の末尾)。中止されていたら JobCancelled。
    起動できなければ JobFailed。進み(job.progress)が STALL_S の間変わらなければ止めて JobFailed。"""
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                errors="replace", env=env, creationflags=mv_audio._no_window())
    except OSError as e:
        raise JobFailed(f"拡大に失敗しました: {Path(argv[0]).name} を起動できません({e})") from e
    procs[job.id] = proc
    tail, stalled = deque(maxlen=LOG_TAIL_LINES), []

    def drain_err() -> None:
        for line in proc.stderr:          # text モードは \r も行の区切りにするので、Real-ESRGAN の「12.34%」の上書き表示も行になる
            if line.strip():
                tail.append(line.rstrip())

    def drain_out() -> None:
        for line in proc.stdout:
            if on_line is not None:
                try:
                    on_line(line.rstrip())
                except Exception:  # noqa: BLE001  進みの読み取りの失敗で拡大を止めない
                    log.exception("子プロセスの出力を読めませんでした")

    threads = [threading.Thread(target=drain_err, daemon=True), threading.Thread(target=drain_out, daemon=True)]
    for t in threads:
        t.start()
    try:
        if job.cancel_requested:                # 走り出す前に中止が来ていた
            proc.kill()
        last_progress, last_at = job.progress, time.monotonic()
        while proc.poll() is None:
            time.sleep(POLL_S)
            if tick is not None:
                tick()
            if job.cancel_requested:
                proc.kill()
            if job.progress != last_progress:
                last_progress, last_at = job.progress, time.monotonic()
            elif time.monotonic() - last_at > STALL_S:
                stalled.append(True)
                proc.kill()
    finally:
        procs.pop(job.id, None)
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        for t in threads:
            t.join(timeout=5)
        for pipe in (proc.stdout, proc.stderr):
            pipe.close()
    if job.cancel_requested:
        raise JobCancelled()
    if stalled:
        raise JobFailed(f"拡大が {STALL_S // 60} 分以上進まないので止めました({Path(argv[0]).name})", "\n".join(tail))
    return proc.returncode, "\n".join(tail)


def _check(code: int, tail: str, out: Path, what: str) -> None:
    """終了コードと出力のファイルを確かめ、失敗なら JobFailed(stderr の最後の行を添える)。"""
    if code != 0 or not out.exists() or (out.is_file() and out.stat().st_size == 0):
        last = tail.splitlines()[-1] if tail else f"終了コード {code}"
        raise JobFailed(f"拡大に失敗しました({what}): {last}", tail)


def _msg_upscaling(label: str, scale: int, frac: float) -> str:
    return f"拡大しています({label} ×{scale}) {round(frac * 100)}%"


def _msg_encoding(frac: float) -> str:
    return f"拡大した動画を作っています {round(frac * 100)}%"


def _ffmpeg_progress(job, total: int, lo: float, hi: float) -> Callable[[str], None]:
    """ffmpeg の frame=N を、進み lo〜hi に写す on_line。"""
    def on_line(line: str) -> None:
        n = ffmpeg_frame(line)
        if n is not None and total > 0:
            frac = min(1.0, n / total)
            job.progress, job.message = round(lo + (hi - lo) * frac, 4), _msg_encoding(frac)
    return on_line


def _env() -> dict:
    """道具の Python に渡す環境: 進みの行をすぐ流し(PYTHONUNBUFFERED)、2 枚以上の GPU では管理する GPU に寄せる。"""
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    env.update(gpu.pin_env(env))
    return env


def _prepare(work_dir: Path, dst: Path) -> Path:
    shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = mv_audio._part(dst)
    tmp.unlink(missing_ok=True)
    return tmp


def run_realesrgan(job, exe, src, dst, scale: int, fps, work_dir, ffmpeg, procs: dict, audio: str = AUDIO_COPY) -> Path:
    """Real-ESRGAN で src を scale 倍にして dst に書く(音声は src から。audio は encode_args)。コマの PNG は work_dir/in・out に置き、終わったら(失敗しても)消す。"""
    src, dst, work_dir, scale = Path(src), Path(dst), Path(work_dir), int(scale)
    label = METHODS["realesrgan"]["ja"]
    in_dir, out_dir = work_dir / "in", work_dir / "out"
    tmp = _prepare(work_dir, dst)
    try:
        in_dir.mkdir()
        out_dir.mkdir()
        job.progress, job.message = 0.0, f"拡大の準備をしています({label} ×{scale})"
        code, tail = _run(job, extract_args(ffmpeg, src, in_dir), procs)
        _check(code, tail, in_dir, "コマの取り出し、ffmpeg")
        total = _count_pngs(in_dir)
        if total == 0:
            raise JobFailed("拡大に失敗しました: 動画からコマを取り出せませんでした", tail)

        def tick() -> None:
            frac = min(1.0, _count_pngs(out_dir) / total)
            job.progress, job.message = round(0.05 + 0.85 * frac, 4), _msg_upscaling(label, scale, frac)
        job.progress, job.message = 0.05, _msg_upscaling(label, scale, 0.0)
        code, tail = _run(job, realesrgan_argv(exe, in_dir, out_dir, scale), procs, tick=tick)
        _check(code, tail, out_dir, label)
        if _count_pngs(out_dir) < total:
            raise JobFailed(f"拡大に失敗しました({label}): 拡大できたコマが足りません({_count_pngs(out_dir)}/{total})", tail)
        job.progress, job.message = 0.9, _msg_encoding(0.0)
        code, tail = _run(job, encode_args(ffmpeg, out_dir, src, tmp, fps, audio), procs, on_line=_ffmpeg_progress(job, total, 0.9, 0.99))
        _check(code, tail, tmp, "動画の作成、ffmpeg")
        os.replace(tmp, dst)
        return dst
    finally:
        tmp.unlink(missing_ok=True)
        shutil.rmtree(work_dir, ignore_errors=True)


def run_rtxvsr(job, python, src, dst, scale: int, fps, work_dir, ffmpeg, procs: dict, quality: str = RTXVSR_QUALITY_DEFAULT,
               audio: str = AUDIO_COPY) -> Path:
    """RTX Video Super Resolution(scripts/upscale_rtxvsr.py を道具の venv の Python で)で src を scale 倍にして dst に書く。音声は ffmpeg で src から写す
    (audio は mux_args)。奇数の寸法は、スクリプトが偶数に切り下げる。"""
    src, dst, work_dir, scale = Path(src), Path(dst), Path(work_dir), int(scale)
    label = METHODS["rtxvsr"]["ja"]
    video_only = work_dir / "video_only.mp4"
    tmp = _prepare(work_dir, dst)
    try:
        job.progress, job.message = 0.0, f"拡大の準備をしています({label} ×{scale})"

        def on_line(line: str) -> None:
            p = parse_progress(line)
            if p is not None and p[1] > 0:
                frac = min(1.0, p[0] / p[1])
                job.progress, job.message = round(0.02 + 0.9 * frac, 4), _msg_upscaling(label, scale, frac)
        code, tail = _run(job, rtxvsr_argv(python, src, video_only, scale, quality), procs, on_line=on_line, env=_env())
        _check(code, tail, video_only, label)
        job.progress, job.message = 0.93, "拡大した動画に音声を付けています"
        code, tail = _run(job, mux_args(ffmpeg, video_only, src, tmp, audio), procs)
        _check(code, tail, tmp, "音声の結合、ffmpeg")
        os.replace(tmp, dst)
        return dst
    finally:
        tmp.unlink(missing_ok=True)
        shutil.rmtree(work_dir, ignore_errors=True)


# ---- 画像 1 枚の拡大(ギャラリーの画像) ----

def _msg_upscaling_image(label: str, scale: int) -> str:
    return f"拡大しています({label} ×{scale})"          # 1 枚なので進みの割合は出せない(道具が 1 回で終える)


def prepare_image_input(src, work_dir) -> Path:
    """道具に渡す画像: そのまま読める形式(IMAGE_INPUTS)で、EXIF の向きが無いならそのまま。ほか(GIF・BMP など)と、向きの付いた JPEG
    (スマートフォンの写真。道具は向きを見ないので、横倒しのまま拡大される)は、向きを直した PNG を work_dir に作ってそれを返す。止まる処理。"""
    from PIL import Image, ImageOps   # 画像を扱うときだけ読み込む
    src, work_dir = Path(src), Path(work_dir)
    with Image.open(src) as im:
        if src.suffix.lower() in IMAGE_INPUTS and im.getexif().get(0x0112, 1) in (None, 1):     # 0x0112 = Orientation
            return src
        im = ImageOps.exif_transpose(im)
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA" if "A" in im.getbands() or "transparency" in im.info else "RGB")
        work_dir.mkdir(parents=True, exist_ok=True)
        out = work_dir / "input.png"
        im.save(out, "PNG")
    return out


def _run_image(job, argv: list, dst: Path, scale: int, label: str, procs: dict, env: dict = None) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = image_part(dst)
    tmp.unlink(missing_ok=True)
    try:
        job.progress, job.message = 0.0, _msg_upscaling_image(label, scale)
        code, tail = _run(job, argv, procs, env=env)
        _check(code, tail, tmp, label)
        os.replace(tmp, dst)
        return dst
    finally:
        tmp.unlink(missing_ok=True)


def run_realesrgan_image(job, exe, src, dst, scale: int, procs: dict) -> Path:
    """Real-ESRGAN で画像 src を scale 倍にして dst(PNG)に書く。作りかけ(.part.png)は残さない。"""
    dst, scale = Path(dst), int(scale)
    return _run_image(job, realesrgan_image_argv(exe, src, image_part(dst), scale), dst, scale, METHODS["realesrgan"]["ja"], procs)


def run_rtxvsr_image(job, python, src, dst, scale: int, procs: dict, quality: str = RTXVSR_QUALITY_DEFAULT) -> Path:
    """RTX Video Super Resolution(scripts/upscale_rtxvsr.py --image)で画像 src を scale 倍にして dst(PNG。透過は捨てる)に書く。"""
    dst, scale = Path(dst), int(scale)
    return _run_image(job, rtxvsr_image_argv(python, src, image_part(dst), scale, quality), dst, scale, METHODS["rtxvsr"]["ja"], procs, env=_env())
