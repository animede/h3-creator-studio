"""GPU 排他の状態機械: 今 GPU にいる部品を空けてから、次の部品を載せる。"""
import asyncio
import logging
import time
import uuid
from typing import Optional

import httpx

from . import procman
from .components import ComponentNotInstalled, all_specs

log = logging.getLogger("studio.scheduler")

STATES = ("stopped", "starting", "ready", "unloaded", "stopping", "error")
GPU_FREE_MARGIN_MIB = 1536     # 空き確認: 基準 + 1.5GiB まで下がれば OK
GPU_FREE_WAIT_S = 60


class SwitchError(Exception):
    """載せ替え・起動の失敗。log_tail は部品のログ末尾(画面に出す)。"""

    def __init__(self, message: str, log_tail: str = ""):
        super().__init__(message)
        self.message = message
        self.log_tail = log_tail


class Busy(SwitchError):
    pass


# ---- HTTP(テストではここを差し替える) ----

async def http_get_json(url: str, timeout: float = 3.0):
    """(ステータス, JSON または None)。接続できなければ (None, None)。"""
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.get(url)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, None
    except httpx.HTTPError:
        return None, None


async def http_post(url: str, timeout: float = 30.0) -> Optional[int]:
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            return (await c.post(url)).status_code
    except httpx.HTTPError:
        return None


class Operation:
    def __init__(self, kind: str, name: str):
        self.id = uuid.uuid4().hex[:8]
        self.kind, self.name = kind, name
        self.steps: list = []
        self.done = False
        self.error: Optional[str] = None
        self.log_tail = ""
        self.started = time.time()

    def add(self, msg: str) -> None:
        self.steps.append({"t": round(time.time() - self.started, 1), "msg": msg})
        log.info("[%s %s] %s", self.kind, self.name, msg)

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "name": self.name, "steps": self.steps,
                "done": self.done, "error": self.error, "log_tail": self.log_tail}


class Component:
    def __init__(self, spec):
        self.spec = spec
        self.state = "stopped"
        self.proc = None
        self.external = False
        self.last_error: Optional[str] = None
        self.since = time.time()


class Scheduler:
    def __init__(self, specs: dict = None, monitor=None, poll_s: float = 1.0):
        self.comps = {n: Component(s) for n, s in (specs if specs is not None else all_specs()).items()}
        self.monitor = monitor
        self.poll_s = poll_s
        self.gpu_wait_s = GPU_FREE_WAIT_S
        self.gpu_resident: Optional[str] = None
        self.baseline_mib: Optional[int] = None
        self.operations: dict = {}
        self._gpu_lock = asyncio.Lock()
        self._cpu_lock = asyncio.Lock()
        self._tasks: set = set()

    # ---- 状態 ----

    def _set(self, c: Component, state: str, error: str = None) -> None:
        c.state, c.since, c.last_error = state, time.time(), error

    def new_operation(self, kind: str, name: str) -> Operation:
        op = Operation(kind, name)
        self.operations[op.id] = op
        for old in list(self.operations)[:-20]:
            del self.operations[old]
        return op

    def current_operation(self) -> Optional[dict]:
        if not self.operations:
            return None
        ops = list(self.operations.values())
        running = [o for o in ops if not o.done]
        return (running[-1] if running else ops[-1]).to_dict()

    def is_busy(self) -> bool:
        return any(not o.done for o in self.operations.values())

    def _lock_for(self, c: Component) -> asyncio.Lock:
        return self._gpu_lock if c.spec.uses_gpu else self._cpu_lock

    def rss_map(self) -> dict:
        return {n: procman.rss_bytes(c.proc) for n, c in self.comps.items() if c.proc is not None}

    def status(self) -> dict:
        out = {}
        for n, c in self.comps.items():
            rss = procman.rss_bytes(c.proc) if c.proc is not None else None
            out[n] = {"label": c.spec.label, "state": c.state, "port": c.spec.port, "external": c.external,
                      "gpu_resident": self.gpu_resident == n, "uses_gpu": c.spec.uses_gpu,
                      "can_unload": c.spec.can_unload, "rss_mib": int(rss / 1048576) if rss else None,
                      "since": c.since, "error": c.last_error}
        return out

    # ---- 起動時: すでに動いている部品を取り込む ----

    async def adopt_running(self) -> None:
        for n, c in self.comps.items():
            proc = await asyncio.to_thread(procman.adopt_if_running, c.spec)
            if proc is None:
                continue
            status, js = await http_get_json(c.spec.health_url)
            c.proc, c.external = proc, True
            self._set(c, "ready" if status == 200 and c.spec.health_ok(js) else "starting")
            if c.spec.uses_gpu:
                if self.gpu_resident is None:
                    self.gpu_resident = n
                else:
                    log.warning("GPU 部品が複数動いています(%s と %s)。VRAM が足りなくなるかもしれません", self.gpu_resident, n)

    # ---- 実行(バックグラウンド) ----

    def launch(self, kind: str, name: str) -> Operation:
        """ensure / start / stop をバックグラウンドで走らせ、操作 ID 付きの Operation を返す。"""
        if name not in self.comps:
            raise KeyError(name)
        op = self.new_operation(kind, name)
        task = asyncio.create_task(self._run(op, getattr(self, kind)(name, op)))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return op

    async def _run(self, op: Operation, coro) -> None:
        try:
            await coro
        except SwitchError as e:
            op.error, op.log_tail = e.message, e.log_tail
            op.add(f"失敗: {e.message}")
        except Exception as e:  # noqa: BLE001
            log.exception("操作に失敗")
            op.error = f"{type(e).__name__}: {e}"
        finally:
            op.done = True

    # ---- 入口 ----

    async def ensure(self, name: str, op: Operation = None) -> None:
        """生成の前に必ず呼ぶ。この部品を使える状態にし、GPU 部品なら唯一の常駐者にする。"""
        own = op is None
        op = op or self.new_operation("ensure", name)
        c = self.comps[name]
        try:
            await self._ensure_impl(c, op)
        except SwitchError as e:
            if own:
                op.error, op.log_tail = e.message, e.log_tail
                op.add(f"失敗: {e.message}")
            raise
        finally:
            if own:
                op.done = True

    async def _ensure_impl(self, c: Component, op: Operation) -> None:
        name = c.spec.name
        async with self._lock_for(c):
            self._check_alive(c)
            if c.spec.uses_gpu:
                if self.gpu_resident == name and c.state == "ready":
                    op.add("準備完了")
                    return
                self._note_baseline()
                if self.gpu_resident and self.gpu_resident != name:
                    await self._release(self.comps[self.gpu_resident], op, target=c)
            await self._bring_up(c, op)
            op.add("準備完了")

    async def start(self, name: str, op: Operation = None) -> None:
        """部品を起動するだけ(常駐者は変えない)。GPU を使用中の別部品がいれば断る。"""
        own = op is None
        op = op or self.new_operation("start", name)
        c = self.comps[name]
        try:
            await self._start_impl(c, op)
        except SwitchError as e:
            if own:
                op.error, op.log_tail = e.message, e.log_tail
                op.add(f"失敗: {e.message}")
            raise
        finally:
            if own:
                op.done = True

    async def _start_impl(self, c: Component, op: Operation) -> None:
        name = c.spec.name
        async with self._lock_for(c):
            self._check_alive(c)
            if c.spec.uses_gpu and self.gpu_resident not in (None, name):
                raise SwitchError(f"{self.comps[self.gpu_resident].spec.label} が GPU を使っています。「準備」で切り替えてください")
            if c.spec.uses_gpu:
                self._note_baseline()
            await self._bring_up(c, op)
            op.add("起動しました")

    async def stop(self, name: str, op: Operation = None) -> None:
        own = op is None
        op = op or self.new_operation("stop", name)
        c = self.comps[name]
        try:
            await self._stop_impl(c, op)
        except SwitchError as e:
            if own:
                op.error, op.log_tail = e.message, e.log_tail
                op.add(f"失敗: {e.message}")
            raise
        finally:
            if own:
                op.done = True

    async def _stop_impl(self, c: Component, op: Operation) -> None:
        name = c.spec.name
        async with self._lock_for(c):
            if c.external:
                raise SwitchError(f"{c.spec.label} は外部で動いているため、ここからは停止しません")
            await self._stop_process(c, op)
            op.add("停止しました")

    # ---- 内部 ----

    def _check_alive(self, c: Component) -> None:
        """自分で起動した部品が落ちていたら stopped に戻す。"""
        if c.proc is not None and not c.external and not procman.is_alive(c.proc):
            c.proc = None
            if self.gpu_resident == c.spec.name:
                self.gpu_resident = None
            if c.state in ("ready", "unloaded", "starting"):
                self._set(c, "stopped")

    def _note_baseline(self) -> None:
        """どの GPU 部品も載っていない今の VRAM 使用量を、空き確認の基準として覚える。"""
        gpu = self.monitor.snapshot().get("gpu") if self.monitor else None
        if gpu and self.gpu_resident is None and not any(
                c.state in ("starting", "ready") and c.spec.uses_gpu for c in self.comps.values()):
            self.baseline_mib = gpu["used_mib"]

    def _free_mib(self) -> Optional[int]:
        gpu = self.monitor.snapshot().get("gpu") if self.monitor else None
        return (gpu["total_mib"] - gpu["used_mib"]) if gpu else None

    async def _release(self, c: Component, op: Operation, target: Component = None) -> None:
        """今 GPU にいる部品 c を空ける。unload できればプロセスは残し、できなければ止める。

        止められない外部の部品でも、GPU に target の分の空きがあれば止めずに同居する(大きな GPU の開発機向け)。
        """
        spec = c.spec
        if c.external and not spec.can_unload and target is not None:
            free, need = self._free_mib(), target.spec.vram_need_mib + 1024
            if free is not None and need and free >= need:
                op.add(f"{spec.label} は外部で動いていますが、GPU に {free // 1024}GiB の空きがあるので止めずに同居します")
                return
        if spec.can_unload:
            op.add(f"{spec.label} を GPU から降ろしています")
            code = await http_post(spec.base_url + spec.unload_path, timeout=120)
            if code == 409:
                raise Busy("生成中の部品があるので切り替えられません")
            if code is None or code >= 300:
                raise SwitchError(f"{spec.label} の unload に失敗しました(HTTP {code})", procman.log_tail(spec.name, 20))
            self._set(c, "unloaded")
        else:
            if c.external:
                free = self._free_mib()
                hint = f"(GPU の空き {free // 1024}GiB では同居できません)" if free is not None else ""
                raise SwitchError(f"{spec.label} は外部で動いていて止められません{hint}。先に自分で停止するか、"
                                  f"studio.local.toml の [components.{spec.name}] に別の port を書いて管理役に起こさせてください")
            await self._stop_process(c, op)
        self.gpu_resident = None
        await self._wait_gpu_free(op)

    async def _stop_process(self, c: Component, op: Operation) -> None:
        op.add(f"{c.spec.label} を停止しています")
        self._set(c, "stopping")
        proc, c.proc = c.proc, None
        if proc is not None:
            await asyncio.to_thread(procman.stop, proc)
        if self.gpu_resident == c.spec.name:
            self.gpu_resident = None
        self._set(c, "stopped")

    async def _wait_gpu_free(self, op: Operation) -> None:
        gpu = self.monitor.snapshot().get("gpu") if self.monitor else None
        if not gpu or self.baseline_mib is None:
            return     # 計れないときは待たない
        op.add("GPU メモリが空くのを待っています")
        limit = self.baseline_mib + GPU_FREE_MARGIN_MIB
        t0 = time.time()
        while time.time() - t0 < self.gpu_wait_s:
            gpu = self.monitor.snapshot().get("gpu")
            if gpu and gpu["used_mib"] <= limit:
                return
            await asyncio.sleep(self.poll_s)
        op.add("GPU メモリが完全には空きませんでした(続行します)")

    async def _bring_up(self, c: Component, op: Operation) -> None:
        """c を ready にする(起動 / reload / 取り込み)。"""
        spec = c.spec
        if c.state == "unloaded" and procman.is_alive(c.proc):
            op.add(f"{spec.label} を GPU に戻しています(モデルの読み込みに数分かかります)")
            code = await http_post(spec.base_url + spec.reload_path, timeout=spec.startup_timeout_s)
            if code == 409:
                raise Busy("生成中の部品があるので切り替えられません")
            if code is None or code >= 300:
                self._set(c, "error", f"reload に失敗(HTTP {code})")
                raise SwitchError(f"{spec.label} の reload に失敗しました(HTTP {code})", procman.log_tail(spec.name, 20))
        elif c.state in ("stopped", "error") or c.proc is None:
            adopted = await asyncio.to_thread(procman.adopt_if_running, spec)
            if adopted:
                c.proc, c.external = adopted, True
                op.add(f"{spec.label} は既に動いているので、そのまま使います")
            else:
                try:
                    c.proc, c.external = procman.start(spec), False
                except ComponentNotInstalled as e:
                    self._set(c, "error", e.hint)
                    raise SwitchError(e.hint)
                op.add(f"{spec.label} を起動しています(モデルの読み込みに数分かかります)")
            self._set(c, "starting")
        elif c.state != "ready":
            self._set(c, "starting")
        await self._wait_health(c)
        self._set(c, "ready")
        if spec.uses_gpu:
            self.gpu_resident = spec.name

    async def _wait_health(self, c: Component) -> None:
        spec = c.spec
        t0 = time.time()
        while True:
            if not c.external and not procman.is_alive(c.proc):
                code = c.proc.popen.returncode if c.proc and c.proc.popen else "?"
                msg = f"{spec.label} が途中で終了しました(終了コード {code})"
                c.proc = None
                self._set(c, "error", msg)
                raise SwitchError(msg, procman.log_tail(spec.name, 20))
            status, js = await http_get_json(spec.health_url)
            if status == 200 and spec.health_ok(js):
                return
            if time.time() - t0 > spec.startup_timeout_s:
                msg = f"{spec.label} が {spec.startup_timeout_s} 秒以内に準備できませんでした"
                proc, c.proc = c.proc, None
                if proc is not None and not c.external:
                    await asyncio.to_thread(procman.stop, proc)
                self._set(c, "error", msg)
                raise SwitchError(msg, procman.log_tail(spec.name, 20))
            await asyncio.sleep(self.poll_s)
