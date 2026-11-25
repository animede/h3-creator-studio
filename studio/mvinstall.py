"""MV の音声処理の道具を画面から入れる(MV タブ・ショート動画タブの「道具を入れる」ボタン)。

setup と同じ動きにするため、管理役の venv の Python で `scripts/setup.py --only mvtools --no-models` を子プロセスとして動かす
(導入の手順は setup.py だけに書く)。出力は logs/mvtools-install.log に書き、最後の 1 行を進み具合として画面に見せる。
同時に 1 つだけ。管理役を止めるときは、子プロセス(uv や pip を含む木ごと)を止める。
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
LOG_PATH = REPO_ROOT / "logs" / "mvtools-install.log"
BUSY_MESSAGE = "MV の音声処理の道具を入れている最中です。終わるまでお待ちください"
FAILED_MESSAGE = "MV の音声処理の道具を入れられませんでした(終了コード {})。logs/mvtools-install.log を見てください"
START_FAILED_MESSAGE = "MV の音声処理の道具の導入を始められません: {}"


class InstallBusy(Exception):
    pass


def command() -> list:
    """setup と同じ導入を、管理役の Python で動かすコマンド(Demucs の重みは最初に分離するときに取得する)。"""
    return [sys.executable, str(SETUP_PY), "--only", "mvtools", "--no-models"]


class ToolsInstaller:
    """道具の導入を 1 つだけ裏で動かし、進み具合(最後の出力の行)と失敗を覚えておく。"""

    def __init__(self, popen=subprocess.Popen, log_path: Path = LOG_PATH):
        self._popen, self.log_path = popen, log_path
        self._lock = threading.Lock()
        self.proc = None
        self.progress_text: Optional[str] = None
        self.error: Optional[str] = None
        self._thread = None

    @property
    def installing(self) -> bool:
        return self.proc is not None

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
                raise InstallBusy(BUSY_MESSAGE)
            self.error, self.progress_text = None, None
            env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
            kwargs = {}
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["start_new_session"] = True          # 止めるときに uv / pip ごと止められるように
            try:
                self.proc = self._popen(command(), cwd=str(REPO_ROOT), env=env, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs)
            except OSError as e:
                self.error = START_FAILED_MESSAGE.format(e)
                raise
            self._thread = threading.Thread(target=self._pump, args=(self.proc,), name="mvtools-install", daemon=True)
            self._thread.start()
        log.info("MV の音声処理の道具の導入を始めました(pid %s)", getattr(self.proc, "pid", "?"))

    def _pump(self, proc) -> None:
        """出力をログに書き、最後の行を覚える。終わったら終了コードを見る。"""
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "ab") as lf:
                lf.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} 開始: {' '.join(command())}\n".encode("utf-8"))
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
            self.error = START_FAILED_MESSAGE.format(e)
        with self._lock:
            if rc != 0 and not self.error:
                self.error = FAILED_MESSAGE.format(rc)
            self.proc = None
        log.info("MV の音声処理の道具の導入が終わりました(終了コード %s)", rc)

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
