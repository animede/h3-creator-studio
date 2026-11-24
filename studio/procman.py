"""子プロセスの起動・停止(Linux / Windows 共通)。親が死んだら子も道連れにする。"""
import atexit
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Optional

import httpx
import psutil

from .config import REPO_ROOT

log = logging.getLogger("studio.procman")
LOG_DIR = REPO_ROOT / "logs"


@dataclass
class Proc:
    spec: object                       # ComponentSpec
    popen: Optional[subprocess.Popen]  # 外部管理(external)のときは None
    external: bool = False
    pid: Optional[int] = None
    job: object = None                 # Windows の Job Object ハンドル
    started_at: float = 0.0


_started: list = []                    # atexit で止める対象(自分で起動したもの)


def log_path(name: str):
    return LOG_DIR / f"{name}.log"


def log_tail(name: str, lines: int = 100) -> str:
    p = log_path(name)
    if not p.is_file():
        return ""
    with open(p, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - 256 * 1024))   # 末尾 256KB だけ読む
        data = f.read()
    return "\n".join(data.decode("utf-8", errors="replace").splitlines()[-lines:])


# ---- 親の死に連動 ----

def _linux_preexec():
    """Linux: 親が死んだら子に SIGTERM(PR_SET_PDEATHSIG)。"""
    import ctypes
    ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)


def _windows_job_object(popen: subprocess.Popen):
    """Windows: 親の終了で子ごと kill される Job Object に入れる。失敗しても致命的ではない。"""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        # 64bit ではハンドルが int に切り詰められないよう、型を明示する
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOperationCount", "WriteOperationCount",
                                                       "OtherOperationCount", "ReadTransferCount",
                                                       "WriteTransferCount", "OtherTransferCount")]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION), ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise OSError("CreateJobObjectW failed")
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise OSError("SetInformationJobObject failed")
        if not kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(int(popen._handle))):
            raise OSError("AssignProcessToJobObject failed")
        return job
    except Exception as e:  # noqa: BLE001
        log.warning("Job Object を設定できませんでした(管理役が強制終了されると部品が残るかもしれません): %s", e)
        return None


# ---- 起動・停止 ----

def start(spec, extra_env: dict = None) -> Proc:
    """spec のコマンドを子プロセスとして起動する(出力は logs/<name>.log に追記)。

    extra_env は起動のたびに決まる環境変数(share の VRAM の予算など)。spec.env の後、studio.toml の env の前に重ねる。
    注意: Linux の PDEATHSIG は「親スレッド」の死に反応するので、イベントループのスレッドから呼ぶこと。
    """
    cmd = spec.build_command(spec.dir)           # 未導入なら ComponentNotInstalled
    env = {**os.environ, **spec.env(spec.dir, spec.port), **(extra_env or {}), **spec.extra_env, "PYTHONUNBUFFERED": "1"}
    # 標準出力をログファイルへ流すと、Windows の cp932 では日本語の print で UnicodeEncodeError になるため UTF-8 に固定する
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    if sys.platform == "win32":
        env.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")   # 開発者モードなしでは symlink を作れず、HF のキャッシュ作成が失敗する
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
        kwargs["preexec_fn"] = _linux_preexec
    with open(log_path(spec.name), "ab") as lf:
        lf.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} 起動: {' '.join(cmd)}\n".encode("utf-8"))
        lf.flush()
        popen = subprocess.Popen(cmd, cwd=str(spec.dir), env=env, stdin=subprocess.DEVNULL,
                                 stdout=lf, stderr=subprocess.STDOUT, **kwargs)
    proc = Proc(spec=spec, popen=popen, pid=popen.pid, started_at=time.time())
    if sys.platform == "win32":
        proc.job = _windows_job_object(popen)
    _started.append(proc)
    log.info("%s を起動しました(pid %d)", spec.label, popen.pid)
    return proc


def is_alive(proc: Optional[Proc]) -> bool:
    if proc is None:
        return False
    if proc.external:
        return True    # 外部管理は health で判断する
    return proc.popen is not None and proc.popen.poll() is None


def _descendants(pid: int) -> list:
    try:
        return psutil.Process(pid).children(recursive=True)
    except psutil.Error:
        return []


def stop(proc: Proc, grace_s: float = 20) -> None:
    """プロセスツリーごと止める。外部管理のものは何もしない。"""
    if proc is None or proc.external or proc.popen is None:
        return
    popen = proc.popen
    kids = _descendants(popen.pid)               # 先に子孫を控える(.bat → python など)
    if popen.poll() is None:
        try:
            if sys.platform == "win32":
                popen.terminate()
            else:
                os.killpg(os.getpgid(popen.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            popen.wait(timeout=grace_s)
        except subprocess.TimeoutExpired:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(popen.pid)], capture_output=True)
            else:
                try:
                    os.killpg(os.getpgid(popen.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
            try:
                popen.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
    # 親が消えても残った子孫を掃除する
    for p in kids:
        try:
            p.kill()
        except psutil.Error:
            pass
    if proc.job is not None:
        try:
            import ctypes
            ctypes.windll.kernel32.CloseHandle(proc.job)
        except Exception:  # noqa: BLE001
            pass
        proc.job = None
    if proc in _started:
        _started.remove(proc)
    log.info("%s を停止しました", proc.spec.label)


def _stop_all():
    for proc in list(_started):
        stop(proc, grace_s=5)


atexit.register(_stop_all)


# ---- 既に動いているものの取り込み ----

def _pid_on_port(port: int) -> Optional[int]:
    try:
        for c in psutil.net_connections(kind="tcp"):
            if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port and c.pid:
                return c.pid
    except (psutil.Error, OSError):
        pass
    return None


def adopt_if_running(spec) -> Optional[Proc]:
    """起動前に準備確認 URL が応答していたら、外部管理として使う(止めない)。"""
    try:
        r = httpx.get(spec.health_url, timeout=1.5)
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    log.info("%s は既に ポート %d で動いているので、そのまま使います(停止はしません)", spec.label, spec.port)
    return Proc(spec=spec, popen=None, external=True, pid=_pid_on_port(spec.port), started_at=time.time())


def rss_bytes(proc: Optional[Proc]) -> Optional[int]:
    """プロセスツリー全体の RSS 合計。見つからなければ None。"""
    if proc is None or proc.pid is None:
        return None
    try:
        root = psutil.Process(proc.pid)
        total = 0
        for p in [root] + root.children(recursive=True):
            try:
                total += p.memory_info().rss
            except psutil.Error:
                pass
        return total
    except psutil.Error:
        return None
