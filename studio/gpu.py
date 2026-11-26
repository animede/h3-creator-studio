"""GPU / CPU メモリ監視: nvidia-smi を 2 秒おきに読み、最新の値を保持する。"""
import base64
import collections
import os
import shutil
import subprocess
import sys
import logging
import threading
import time
from typing import Callable, Optional

import psutil

log = logging.getLogger(__name__)

POLL_S = 2.0
SHARED_CACHE_S = 10.0
SHARED_WINDOW_S = 10.0
SHARED_GROW_MIB = 1024
WARN_SPILL = ("VRAM があふれて共有 GPU メモリに退避しています(極端に遅くなります)。"
              "画角を小さくするか、他の GPU 利用アプリを閉じてください")
WARN_NO_SMI = "nvidia-smi が見つかりません"
SHARED_MAX_FAILS = 3   # 連続でこの回数 None なら、共有 GPU メモリの監視をあきらめる


def find_nvidia_smi() -> Optional[str]:
    found = shutil.which("nvidia-smi")
    if found:
        return found
    if sys.platform == "win32":
        # 新しいドライバは System32、古いドライバは NVIDIA Corporation\NVSMI(PATH に無い)
        for p in (os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvidia-smi.exe"),
                  os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "NVIDIA Corporation", "NVSMI", "nvidia-smi.exe")):
            if os.path.isfile(p):
                return p
    return None


# ---- 管理する GPU(2 枚以上ある PC。2026-11-26) ----
# 管理役が VRAM を数えるのは 1 枚だけ。既定(auto)では、すべてのエンジンに CUDA_VISIBLE_DEVICES=<その番号> を渡して同じ 1 枚に寄せる
# (MiniMax-H3-Image は GPU が 2 枚見えると TE を 2 枚目に置くので、小さい 2 枚目で OOM になった)。
_devices = "auto"        # [gpu] devices: "auto" / "all" / 番号("1" など)
_count_cache: Optional[int] = None


def parse_gpu_devices(value) -> str:
    """[gpu] devices の値を "auto" / "all" / 番号の文字列にそろえる。読めなければ auto。"""
    v = str(value).strip().lower()
    if v in ("auto", "all") or v.isdigit():
        return v
    log.warning("[gpu] devices = %r は読めません(auto / all / GPU の番号 のいずれか)。auto として扱います", value)
    return "auto"


def configure_devices(value) -> None:
    global _devices
    _devices = parse_gpu_devices(value)


def _gpu_index() -> int:
    """管理する GPU の物理番号。[gpu] devices が番号ならそれ、無ければ CUDA_VISIBLE_DEVICES の先頭、無ければ 0。"""
    if _devices.isdigit():
        return int(_devices)
    first = os.environ.get("CUDA_VISIBLE_DEVICES", "").split(",")[0].strip()
    return int(first) if first.isdigit() else 0


def gpu_count() -> int:
    """nvidia-smi で見える GPU の数(一度だけ数える)。nvidia-smi が無ければ 0。"""
    global _count_cache
    if _count_cache is None:
        exe = find_nvidia_smi()
        n = 0
        if exe:
            try:
                out = subprocess.run([exe, "--query-gpu=index", "--format=csv,noheader"], capture_output=True, text=True,
                                     timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                n = sum(1 for x in out.stdout.splitlines() if x.strip().isdigit()) if out.returncode == 0 else 0
            except (OSError, subprocess.SubprocessError):
                n = 0
        _count_cache = n
    return _count_cache


def pin_env(env: dict, count: Optional[int] = None) -> dict:
    """エンジンを管理する GPU に寄せる環境変数。env(子に渡す予定の環境)に CUDA_VISIBLE_DEVICES があれば何もしない
    (管理役自身の環境・[components.<name>.env]・CPU 専用の LLM の -1)。devices = "all" なら寄せない。
    auto は GPU が 2 枚以上のときだけ、番号の指定は常に寄せる。"""
    if "CUDA_VISIBLE_DEVICES" in env or _devices == "all":
        return {}
    if not _devices.isdigit() and (gpu_count() if count is None else count) <= 1:
        return {}
    # 番号は nvidia-smi の並び(PCI バス順)。CUDA の既定の並び(速い順)とずれる PC があるので、CUDA_DEVICE_ORDER もそろえる
    out = {"CUDA_VISIBLE_DEVICES": str(_gpu_index())}
    if "CUDA_DEVICE_ORDER" not in env:
        out["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
    return out


def smi_mib(field: str) -> Optional[int]:
    """最初に見える GPU(CUDA_VISIBLE_DEVICES の先頭)の memory.<field>(free / total)を MiB で返す。nvidia-smi が無ければ None。"""
    exe = find_nvidia_smi()
    if exe is None:
        return None
    try:
        out = subprocess.run([exe, "-i", str(_gpu_index()), f"--query-gpu=memory.{field}", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return int(float(out.stdout.strip().splitlines()[0]))
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def parse_smi(text: str, index: int = 0) -> Optional[dict]:
    """nvidia-smi の csv(index,name,used,total,util)から指定の GPU を取り出す。"""
    rows = []
    for line in text.strip().splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) >= 5 and parts[0].isdigit():
            rows.append(parts)
    if not rows:
        return None
    row = next((r for r in rows if int(r[0]) == index), rows[0])

    def num(s):
        try:
            return int(float(s))
        except ValueError:
            return 0
    return {"index": int(row[0]), "count": len(rows), "name": row[1], "used_mib": num(row[2]), "total_mib": num(row[3]), "util": num(row[4])}


PERFLIB = r"HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Perflib"
EN_PATH = r"\GPU Process Memory(*)\Shared Usage"
SPLIT = "=====SPLIT====="
# Perflib の Counter 値(英語の 009 と現在の言語)を、区切り線をはさんで UTF-8 で出す PowerShell
_PERFLIB_SCRIPT = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
                   "$k='" + PERFLIB + "\\';"
                   "(Get-ItemProperty ($k+'009')).Counter -join \"`n\";"
                   "'" + SPLIT + "';"
                   "(Get-ItemProperty ($k+'CurrentLanguage')).Counter -join \"`n\"")
_counter_path: Optional[str] = None


def parse_perflib(text: str) -> dict[int, str]:
    """Perflib の Counter 値(「番号」「名前」の行が交互に並ぶ)を {番号: 名前} にする。ずれても次の数字行で立て直す。"""
    lines = [x.strip() for x in text.splitlines()]
    out, i = {}, 0
    while i < len(lines) - 1:
        if lines[i].isdigit():
            out.setdefault(int(lines[i]), lines[i + 1])
            i += 2
        else:
            i += 1
    return out


def localized_counter_path(english: str, local: str, set_name: str = "GPU Process Memory",
                           counter: str = "Shared Usage") -> Optional[str]:
    """英語の名前から番号を引き、同じ番号の現地語の名前でカウンターのパスを作る。見つからなければ None。"""
    en, loc = parse_perflib(english), parse_perflib(local)
    idx = {name: i for i, name in sorted(en.items(), reverse=True)}   # 同名が複数あれば番号の小さい方
    if set_name not in idx or counter not in idx:
        return None
    s, c = loc.get(idx[set_name]), loc.get(idx[counter])
    return f"\\{s}(*)\\{c}" if s and c else None


def _powershell(cmd: str, timeout: float = 15) -> Optional[str]:
    try:
        # 引用符や日本語の受け渡しで悩まないよう、スクリプトは UTF-16LE の base64 で渡す
        enc = base64.b64encode(cmd.encode("utf-16-le")).decode("ascii")
        out = subprocess.run(["powershell", "-NoProfile", "-EncodedCommand", enc], capture_output=True, timeout=timeout,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.decode("utf-8", "replace") if out.returncode == 0 else None


def resolve_counter_path() -> str:
    """日本語版などではカウンター名が現地語になるので、Perflib から引く(1 回だけ)。失敗したら英語のパス。"""
    global _counter_path
    if _counter_path is None:
        text = _powershell(_PERFLIB_SCRIPT)
        path = None
        if text and SPLIT in text:
            en, loc = text.split(SPLIT, 1)
            path = localized_counter_path(en, loc)
        _counter_path = path or EN_PATH
    return _counter_path


def shared_total_mib(ram_total_bytes: int, plat: str = None) -> Optional[int]:
    """Windows の「共有 GPU メモリ」の上限(MiB)。WDDM はシステムのメモリの半分を共有 GPU メモリに割り当てる(タスク マネージャーの
    「共有 GPU メモリ」の分母と同じ規則)。Perflib のカウンター(GPU Process Memory の Shared Usage)は使用量だけで上限を持たないので、
    ここでは RAM の合計の半分として計算する(DXGI の SharedSystemMemory を読むより安い)。Linux には無いので None。"""
    if (plat or sys.platform) != "win32" or not ram_total_bytes:
        return None
    return int(ram_total_bytes / 2 / 1048576)


def read_shared_mib() -> Optional[int]:
    """Windows の「共有 GPU メモリ」合計(MiB)。取れなければ None。Linux には無い。"""
    if sys.platform != "win32":
        return None
    cmd = (f"(Get-Counter '{resolve_counter_path()}').CounterSamples | "
           "Measure-Object CookedValue -Sum | Select-Object -ExpandProperty Sum")
    out = _powershell(cmd)
    try:
        return int(float(out.strip()) / 1048576) if out and out.strip() else None
    except ValueError:
        return None


class GpuMonitor:
    def __init__(self, rss_provider: Callable[[], dict] = None, generating: Callable[[], bool] = None):
        self.rss_provider = rss_provider or (lambda: {})
        self.generating = generating or (lambda: False)
        self._smi = find_nvidia_smi()
        self._snap: dict = {"gpu": None, "ram": None, "procs": {}, "shared_gpu_mib": None, "shared_gpu_total_mib": None,
                            "warning": None if self._smi else WARN_NO_SMI, "ts": 0}
        self._shared_hist = collections.deque()   # (時刻, MiB)
        self._shared_at = 0.0
        self._shared = None
        self._shared_fails = 0
        self._shared_off = False
        self._warn_until = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is None:
            self.poll(read_shared=False)   # PowerShell(数秒かかる)で起動を止めない。共有 GPU メモリは最初のループで読む
            self._thread = threading.Thread(target=self._loop, name="gpu-monitor", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(POLL_S):
            try:
                self.poll()
            except Exception:  # noqa: BLE001  監視の失敗で管理役を止めない
                pass

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._snap)

    def _read_gpu(self) -> Optional[dict]:
        if not self._smi:
            return None
        try:
            out = subprocess.run([self._smi, "--query-gpu=index,name,memory.used,memory.total,utilization.gpu",
                                  "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return parse_smi(out.stdout, _gpu_index()) if out.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            return None

    def _update_shared(self, gpu: Optional[dict], now: float, read: bool = True) -> Optional[str]:
        """共有 GPU メモリの増加を見張る。警告文(または None)を返す。"""
        if sys.platform != "win32" or self._shared_off:
            return None
        if read and now - self._shared_at >= SHARED_CACHE_S:
            self._shared, self._shared_at = read_shared_mib(), time.time()
            self._shared_fails = 0 if self._shared is not None else self._shared_fails + 1
            if self._shared_fails >= SHARED_MAX_FAILS:
                self._shared_off = True
                log.warning("共有 GPU メモリを読めないため、この起動中は監視をやめます")
        if self._shared is None:
            return None
        self._shared_hist.append((now, self._shared))
        while self._shared_hist and now - self._shared_hist[0][0] > SHARED_WINDOW_S:
            self._shared_hist.popleft()
        base = min(v for _, v in self._shared_hist)
        if self.generating() and self._shared - base > SHARED_GROW_MIB:
            self._warn_until = now + 30
        return WARN_SPILL if now < self._warn_until else None

    def poll(self, read_shared: bool = True) -> None:
        now = time.time()
        gpu = self._read_gpu()
        vm = psutil.virtual_memory()
        warning = self._update_shared(gpu, now, read_shared) or (None if self._smi else WARN_NO_SMI)
        snap = {"gpu": gpu, "ram": {"used_mib": int(vm.used / 1048576), "total_mib": int(vm.total / 1048576)},
                "procs": {k: int(v / 1048576) for k, v in self.rss_provider().items() if v is not None},
                "shared_gpu_mib": self._shared if sys.platform == "win32" else None,
                "shared_gpu_total_mib": shared_total_mib(vm.total),          # 共有 GPU メモリの上限(Windows だけ。RAM の半分)
                "warning": warning, "ts": now}
        with self._lock:
            self._snap = snap
