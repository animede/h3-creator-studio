"""道具を画面から入れる(MV タブ・ショート動画タブの「道具を入れる」、編集タブの拡大の「入れる」)。

setup と同じ動きにするため、管理役の venv の Python で `scripts/setup.py --only <target> --no-models` を子プロセスとして動かす
(導入の手順は setup.py だけに書く)。出力は logs/<target>-install.log に書き、最後の 1 行を進み具合として画面に見せる。
target ごとに 1 つ(同時に 1 つだけ)。管理役を止めるときは、子プロセス(uv や pip を含む木ごと)を止める。
  mvtools            : MV の音声処理の道具(Demucs と librosa の venv)
  upscale-realesrgan : Real-ESRGAN ncnn-vulkan(編集タブの拡大)
  upscale-rtxvsr     : RTX Video Super Resolution の venv(nvidia-vfx。--accept-nvidia-license を足す)
"""
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from .config import REPO_ROOT

log = logging.getLogger("studio.mvinstall")

SETUP_PY = REPO_ROOT / "scripts" / "setup.py"
MVTOOLS = "mvtools"
# target → 文言に入れる名前(「〜を入れている最中です」の〜)
LABELS = {MVTOOLS: "MV の音声処理の道具", "upscale-realesrgan": "拡大の道具(Real-ESRGAN)", "upscale-rtxvsr": "拡大の道具(RTX Video Super Resolution)"}
BUSY_TEMPLATE = "{}を入れている最中です。終わるまでお待ちください"
FAILED_TEMPLATE = "{}を入れられませんでした(終了コード {})。logs/{}-install.log を見てください"
START_FAILED_TEMPLATE = "{}の導入を始められません: {}"
# mvtools の文言(前からの名前。テストと英訳の辞書が使う)
LOG_PATH = REPO_ROOT / "logs" / f"{MVTOOLS}-install.log"
BUSY_MESSAGE = BUSY_TEMPLATE.format(LABELS[MVTOOLS])
FAILED_MESSAGE = FAILED_TEMPLATE.format(LABELS[MVTOOLS], "{}", MVTOOLS)
START_FAILED_MESSAGE = START_FAILED_TEMPLATE.format(LABELS[MVTOOLS], "{}")


class InstallBusy(Exception):
    pass


def label_of(target: str) -> str:
    return LABELS.get(target, target)


def log_path_of(target: str) -> Path:
    return REPO_ROOT / "logs" / f"{target}-install.log"


def command_for(target: str, extra_args=()) -> list:
    """setup と同じ導入を、管理役の Python で動かすコマンド(モデル・重みは取得しない。Demucs の重みは最初に分離するときに取得する)。"""
    return [sys.executable, str(SETUP_PY), "--only", target, "--no-models", *map(str, extra_args)]


def command() -> list:
    """mvtools のコマンド(前からの口)。"""
    return command_for(MVTOOLS)


class ToolsInstaller:
    """1 つの target の導入を裏で動かし、進み具合(最後の出力の行)と失敗を覚えておく。同時に 1 つだけ。"""

    def __init__(self, target: str = MVTOOLS, extra_args=(), popen=subprocess.Popen, log_path: Path = None):
        self.target, self.extra_args = target, tuple(extra_args)
        self.label = label_of(target)
        self._popen, self.log_path = popen, Path(log_path) if log_path is not None else log_path_of(target)
        self._lock = threading.Lock()
        self.proc = None
        self.progress_text: Optional[str] = None
        self.error: Optional[str] = None
        self._thread = None

    @property
    def installing(self) -> bool:
        return self.proc is not None

    def command(self) -> list:
        return command_for(self.target, self.extra_args)

    def status(self) -> dict:
        out = {"installing": self.installing}
        if self.progress_text:
            out["progress_text"] = self.progress_text
        if self.error:
            out["error"] = self.error
        return out

    def start(self) -> None:
        """導入を始める。走っている最中なら InstallBusy。"""
        with self._lock:
            if self.proc is not None:
                raise InstallBusy(BUSY_TEMPLATE.format(self.label))
            self.error, self.progress_text = None, None
            env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
            kwargs = {}
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["start_new_session"] = True          # 止めるときに uv / pip ごと止められるように
            try:
                self.proc = self._popen(self.command(), cwd=str(REPO_ROOT), env=env, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs)
            except OSError as e:
                self.error = START_FAILED_TEMPLATE.format(self.label, e)
                raise
            self._thread = threading.Thread(target=self._pump, args=(self.proc,), name=f"{self.target}-install", daemon=True)
            self._thread.start()
        log.info("%sの導入を始めました(pid %s)", self.label, getattr(self.proc, "pid", "?"))

    def _pump(self, proc) -> None:
        """出力をログに書き、最後の行を覚える。終わったら終了コードを見る。"""
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "ab") as lf:
                lf.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} 開始: {' '.join(self.command())}\n".encode("utf-8"))
                for raw in iter(proc.stdout.readline, b""):
                    lf.write(raw)
                    lf.flush()
                    line = raw.decode("utf-8", errors="replace").strip()
                    if line:
                        self.progress_text = line
            rc = proc.wait()
        except Exception as e:  # noqa: BLE001
            log.exception("導入の出力を読めませんでした")
            rc = -1
            self.error = START_FAILED_TEMPLATE.format(self.label, e)
        with self._lock:
            if rc != 0 and not self.error:
                self.error = FAILED_TEMPLATE.format(self.label, rc, self.target)
            self.proc = None
        log.info("%sの導入が終わりました(終了コード %s)", self.label, rc)

    def stop(self) -> None:
        """管理役を止めるとき: 走っている導入を子プロセスの木ごと止める。"""
        proc = self.proc
        if proc is None:
            return
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=30,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                os.killpg(proc.pid, signal.SIGTERM)
        except (OSError, subprocess.SubprocessError):
            try:
                proc.kill()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=10)
