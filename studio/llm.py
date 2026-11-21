"""管理役が持つ LLM: llama.cpp の llama-server(CUDA 版)で Gemma 4 E4B を動かす。プロンプト強化と作詞に使う。

考え方は ACE-Step-1.5-fast の easy-music(services/local_llm_service.py)をそのまま引き継ぐ:
- 最初に使うときに起こす(lazy)。`--fit on --fit-target 1536` で、いま GPU に載っている部品の隣の空き VRAM に
  入る分だけ GPU に載せ、残りは CPU で動かす。
- GPU で生成する直前に止めて VRAM を返し(release_before_gpu)、生成の最中は起こさない(chat は待つ)。
- 子プロセスは procman で起動するので、管理役が死ねば道連れになる(Linux は PDEATHSIG、Windows は Job Object)。
"""
import asyncio
import contextlib
import logging
import os
import platform
import re
import shutil
import sys
import tarfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Union

import httpx

from . import procman
from .config import Settings
from .gpu import smi_mib

log = logging.getLogger("studio.llm")

TAG = "b12475"             # Gemma 4 E4B Q4_0 で動作確認した llama.cpp のリリース
CUDA_WIN, CUDA_LINUX = "12.4", "12.8"
MODEL_REPO, MODEL_FILE, MODEL_SIZE_HINT = "ggml-org/gemma-4-E4B-it-GGUF", "gemma-4-E4B-it-Q4_0.gguf", "約 4.4GB"
FIT_TARGET_MIB = 1536      # --fit が GPU に残す余白(MiB)
N_CTX = 8192
HEALTH_TIMEOUT_S = 300     # 初回はモデルの読み込みに時間がかかる
REQUEST_TIMEOUT_S = 600    # CPU に溢れた分の推論は遅い
SESSION_WAIT_S = 300       # release_before_gpu が、使用中の chat の終わりを待つ最長
SETTLE_S = 2.5             # GPU 版を止めたあと、nvidia-smi の監視(2 秒おき)が空きを反映するのを待つ

# Gemma 4 の思考チャネル / <think> ブロック(念のため両方除去)
THOUGHT_RE = re.compile(r"<\|channel>thought\n.*?<channel\|>\s*|<think>.*?</think>\s*", flags=re.DOTALL)


class LlmError(Exception):
    """LLM が使えない・失敗した。message は利用者向けの日本語。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


# ---- 設定の読み取り ----

def parse_gpu_setting(value) -> Union[str, int]:
    """[llm] gpu の値を "auto" / "off" / "all" / 層数(int)にそろえる。読めなければ auto。"""
    v = str(value).strip().lower()
    if v in ("off", "false", "no", "cpu", "0"):
        return "off"
    if v in ("", "auto", "on", "true"):
        return "auto"
    if v == "all":
        return "all"
    if v.isdigit():
        return int(v)
    log.warning("[llm] gpu = %r は読めません。auto として扱います", value)
    return "auto"


def use_gpu(mode, plat: str = None, machine: str = None, free_mib: Callable = None) -> bool:
    """CUDA 版を使うか。auto は x64 の Windows / Linux で nvidia-smi が見えるとき。"""
    if mode == "off":
        return False
    plat, machine = plat or sys.platform, (machine or platform.machine()).lower()
    if machine not in ("x86_64", "amd64") or not (plat == "win32" or plat.startswith("linux")):
        return False
    if mode == "auto":
        return (free_mib or smi_mib)("free") is not None
    return True


def release_asset(gpu: bool, tag: str = TAG, plat: str = None, machine: str = None) -> Optional[tuple]:
    """(アーカイブ名, llama-server の名前, cudart アーカイブ名 or None)。未対応のプラットフォームは None。"""
    plat, machine = plat or sys.platform, (machine or platform.machine()).lower()
    x64, arm = machine in ("x86_64", "amd64"), machine in ("aarch64", "arm64")
    if plat.startswith("linux"):
        if gpu and x64:
            return (f"llama-{tag}-bin-ubuntu-cuda-{CUDA_LINUX}-x64.tar.gz", "llama-server",
                    f"cudart-llama-{tag}-bin-ubuntu-cuda-{CUDA_LINUX}-x64.tar.gz")
        if x64 or arm:
            return f"llama-{tag}-bin-ubuntu-{'x64' if x64 else 'arm64'}.tar.gz", "llama-server", None
    elif plat == "win32":
        if gpu and x64:
            return (f"llama-{tag}-bin-win-cuda-{CUDA_WIN}-x64.zip", "llama-server.exe",
                    f"cudart-llama-bin-win-cuda-{CUDA_WIN}-x64.zip")
        if x64 or arm:
            return f"llama-{tag}-bin-win-cpu-{'x64' if x64 else 'arm64'}.zip", "llama-server.exe", None
    return None


# ---- llama-server の取得 ----

def extract_archive(archive: Path, dest_dir: Path) -> None:
    """zip / tar.gz を dest_dir に展開し、1 段のディレクトリに入っていれば中身を持ち上げる(.so / .dll は exe と同じ場所に要る)。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    before = set(os.listdir(dest_dir))
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            z.extractall(dest_dir)
    else:
        with tarfile.open(archive, mode="r:gz") as t:
            try:
                t.extractall(dest_dir, filter="data")
            except TypeError:      # filter 引数が無い古い Python
                t.extractall(dest_dir)
    added = [x for x in os.listdir(dest_dir) if x not in before]
    if len(added) == 1 and (dest_dir / added[0]).is_dir():
        sub = dest_dir / added[0]
        for item in os.listdir(sub):
            if not (dest_dir / item).exists():
                shutil.move(str(sub / item), str(dest_dir / item))
        shutil.rmtree(sub, ignore_errors=True)


def http_fetch(url: str, dest: Path) -> None:
    """url を dest へ取得する(0.8GB 級なのでメモリに載せず、ファイルへ流す)。"""
    log.info("取得しています: %s", url)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with httpx.stream("GET", url, follow_redirects=True, timeout=httpx.Timeout(60.0, read=600.0)) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
    tmp.replace(dest)


def install_binary(dest_dir: Path, gpu: bool, fetch: Callable = http_fetch, tag: str = TAG) -> Path:
    """GitHub Releases の固定タグから llama-server(GPU なら CUDA 版 + cudart)を取得して dest_dir に展開する。"""
    asset = release_asset(gpu, tag)
    if asset is None:
        raise LlmError(f"この環境({sys.platform}/{platform.machine()})用の llama-server がありません。"
                       "studio.local.toml の [llm] llama_server に実行ファイルのパスを書いてください")
    name, exe_name, cudart = asset
    base = f"https://github.com/ggml-org/llama.cpp/releases/download/{tag}/"
    dl = dest_dir / "_download"
    dl.mkdir(parents=True, exist_ok=True)
    try:
        for archive in [name] + ([cudart] if cudart else []):
            fetch(base + archive, dl / archive)
            extract_archive(dl / archive, dest_dir)
    finally:
        shutil.rmtree(dl, ignore_errors=True)
    exe = dest_dir / exe_name
    if not exe.exists():
        raise LlmError(f"展開しても {exe} がありません(アーカイブの構成が変わったかもしれません)")
    if sys.platform != "win32":
        exe.chmod(exe.stat().st_mode | 0o111)
    log.info("llama-server を用意しました: %s(%s)", exe, "CUDA" if gpu else "CPU")
    return exe


def binary_dir(tools_dir: Path, gpu: bool) -> Path:
    return tools_dir / (TAG + ("-cuda" if gpu else ""))


def find_binary(cfg: Settings, gpu: bool) -> Optional[Path]:
    """[llm] llama_server → tools_dir 内 → (CPU 版のみ)PATH の順。無ければ None。上書きがあるのに無いときは LlmError。"""
    override = str(cfg.llm["llama_server"]).strip()
    if override:
        p = Path(override).expanduser()
        if not p.is_file():
            raise LlmError(f"[llm] llama_server のファイルがありません: {p}")
        return p
    local = binary_dir(tools_dir_of(cfg), gpu) / ("llama-server.exe" if sys.platform == "win32" else "llama-server")
    if local.is_file():
        return local
    found = None if gpu else shutil.which("llama-server")     # PATH の llama-server は CPU 版かもしれないので GPU では使わない
    return Path(found) if found else None


# ---- モデルの取得 ----

def tools_dir_of(cfg: Settings) -> Path:
    p = Path(str(cfg.llm["tools_dir"])).expanduser()
    return p if p.is_absolute() else cfg.root / p


def find_gguf(cfg: Settings) -> Optional[Path]:
    """[llm] gguf → tools_dir → 他の部品がすでに持っているコピー(ACE の easy-music、h3 の models/llm)の順。無ければ None。"""
    override = str(cfg.llm["gguf"]).strip()
    if override:
        p = Path(override).expanduser()
        if not p.is_file():
            raise LlmError(f"[llm] gguf のファイルがありません: {p}")
        return p
    for p in (tools_dir_of(cfg) / MODEL_FILE,
              cfg.component_dir("ace") / "easy-music" / "models" / MODEL_FILE,
              cfg.component_dir("h3") / "models" / "llm" / MODEL_FILE):
        if p.is_file():
            return p
    return None


def download_model(cfg: Settings) -> Path:
    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        raise LlmError("huggingface_hub がありません。`pip install huggingface_hub` するか、"
                       "studio.local.toml の [llm] gguf に GGUF のパスを書いてください")
    dest = tools_dir_of(cfg)
    dest.mkdir(parents=True, exist_ok=True)
    log.info("モデルを取得しています: %s/%s(%s)", MODEL_REPO, MODEL_FILE, MODEL_SIZE_HINT)
    return Path(hf_hub_download(repo_id=MODEL_REPO, filename=MODEL_FILE, local_dir=str(dest)))


def build_command(exe: Path, gguf: Path, port: int, gpu: bool, mode) -> list:
    cmd = [str(exe), "-m", str(gguf), "--host", "127.0.0.1", "--port", str(port), "-c", str(N_CTX),
           "-t", str(max(4, (os.cpu_count() or 8) // 2)), "--reasoning", "off"]
    if gpu:
        if mode == "auto":     # 空き VRAM に入る層数だけ GPU へ(他の部品が載っていても共存できる)
            cmd += ["--fit", "on", "--fit-target", str(FIT_TARGET_MIB)]
        else:
            cmd += ["-ngl", str(mode), "--fit", "off"]
    return cmd


@dataclass
class LlmSpec:
    """procman.start が読む、ComponentSpec と同じ形の最小限。"""
    dir: Path
    port: int
    cmd: list
    name: str = "llm"
    label: str = "LLM(Gemma 4 E4B)"
    extra_env: dict = field(default_factory=dict)

    def build_command(self, d) -> list:
        return self.cmd

    def env(self, d, p) -> dict:
        return {}


class Llm:
    """llama-server の子プロセス 1 つ。busy() が True の間(GPU で生成している間)は、起こさずに待つ。"""

    def __init__(self, cfg: Settings, busy: Callable[[], bool] = lambda: False, transport=None,
                 poll_s: float = 0.5, settle_s: float = SETTLE_S):
        self.cfg, self.busy, self.transport = cfg, busy, transport
        self.port, self.mode = cfg.llm_port, parse_gpu_setting(cfg.llm["gpu"])
        self.poll_s, self.settle_s = poll_s, settle_s
        self.starts = 0
        self._proc: Optional[procman.Proc] = None
        self._external = False        # 同じポートで、すでに別の llama-server が動いていた
        self._gpu = False             # いまの子プロセスが CUDA 版か
        self._starting = False
        self._error: Optional[str] = None
        self._users = 0               # chat / 強化の最中の数(release_before_gpu はこれが 0 になるのを待つ)
        self._start_lock = asyncio.Lock()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def running(self) -> bool:
        return self._external or (self._proc is not None and procman.is_alive(self._proc))

    async def _healthy(self, timeout: float = 2.0) -> bool:
        try:
            async with httpx.AsyncClient(timeout=timeout, transport=self.transport) as c:
                return (await c.get(self.base_url + "/health")).status_code == 200
        except httpx.HTTPError:
            return False

    # ---- 起動と停止 ----

    async def ensure_running(self) -> str:
        """llama-server を使える状態にして、base URL を返す。起動に失敗したら LlmError。"""
        async with self._start_lock:
            if self._proc is not None and not procman.is_alive(self._proc):
                self._proc = None
            if self.running() and await self._healthy():
                return self.base_url
            if self._proc is not None:
                await self.stop()             # 生きているのに応答しない
            elif await self._healthy():
                log.info("ポート %d で llama-server が既に動いているので、それを使います(停止はしません)", self.port)
                self._external = True
                return self.base_url
            self._external, self._error, self._starting = False, None, True
            try:
                await self._start()
            except LlmError as e:
                self._error = e.message
                raise
            finally:
                self._starting = False
            return self.base_url

    async def _start(self) -> None:
        gpu = await asyncio.to_thread(use_gpu, self.mode)
        try:
            exe = find_binary(self.cfg, gpu)
            if exe is None:
                exe = await asyncio.to_thread(install_binary, binary_dir(tools_dir_of(self.cfg), gpu), gpu)
            gguf = find_gguf(self.cfg)
            if gguf is None:
                gguf = await asyncio.to_thread(download_model, self.cfg)
        except LlmError:
            raise
        except Exception as e:  # noqa: BLE001  ネットワーク・ディスクなど
            raise LlmError(f"LLM の用意に失敗しました: {type(e).__name__}: {e}"
                           "(studio.local.toml の [llm] に llama_server と gguf のパスを書けば、取得は不要です)")
        log.info("LLM: %s / %s / GPU 設定 %s / 空き VRAM %s MiB", exe, gguf.name, self.mode, await asyncio.to_thread(smi_mib, "free"))
        spec = LlmSpec(dir=exe.parent, port=self.port, cmd=build_command(exe, gguf, self.port, gpu, self.mode))
        self._proc, self._gpu = procman.start(spec), gpu      # Linux の PDEATHSIG のため、イベントループのスレッドから起動する
        self.starts += 1
        waited = 0.0
        while waited < HEALTH_TIMEOUT_S:
            if not procman.is_alive(self._proc):
                code = self._proc.popen.returncode if self._proc.popen else "?"
                self._proc = None
                raise LlmError(f"llama-server が途中で終了しました(終了コード {code})。{procman.log_tail('llm', 8)}")
            if await self._healthy():
                log.info("LLM の準備ができました(%.0f 秒、%s)", waited, "GPU あり" if gpu else "CPU")
                return
            await asyncio.sleep(self.poll_s)
            waited += self.poll_s
        await self.stop()
        raise LlmError(f"llama-server が {HEALTH_TIMEOUT_S} 秒以内に準備できませんでした")

    async def stop(self) -> bool:
        """自分で起動した子プロセスを止める。止めたら True(外部の llama-server には触れない)。"""
        proc, self._proc = self._proc, None
        if proc is None:
            return False
        await asyncio.to_thread(procman.stop, proc)
        return True

    async def release_before_gpu(self) -> bool:
        """GPU 部品を載せる直前に呼ぶ。CUDA 版の子プロセスが居れば止めて VRAM を返す(次の chat で起こし直す)。

        chat / 強化の最中なら、その終わりを待ってから止める。CPU だけで動いている子は止めない。
        """
        waited = 0.0
        while self._users > 0 and waited < SESSION_WAIT_S:
            await asyncio.sleep(self.poll_s)
            waited += self.poll_s
        if self._proc is None or not self._gpu:
            return False
        log.info("LLM を止めて VRAM を返します")
        stopped = await self.stop()
        if stopped and self.settle_s:
            await asyncio.sleep(self.settle_s)
        return stopped

    # ---- 使う ----

    async def _wait_gpu_idle(self) -> None:
        if not self.busy():
            return
        log.info("LLM: GPU で生成中なので、終わるまで待ちます")
        while self.busy():
            await asyncio.sleep(self.poll_s)

    @contextlib.asynccontextmanager
    async def session(self):
        """LLM を使う区間。GPU で生成している間は入れず、入っている間は release_before_gpu が止めるのを待つ。"""
        await self._wait_gpu_idle()
        self._users += 1       # busy() の確認から加算までに await が無いので、すれ違わない
        try:
            yield
        finally:
            self._users -= 1

    async def chat(self, system: str, user: str, max_tokens: int = 2000, temperature: float = 0.8) -> str:
        async with self.session():
            base = await self.ensure_running()
            payload = {"messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                       "max_tokens": max_tokens, "temperature": temperature, "top_p": 0.95, "top_k": 64, "repeat_penalty": 1.1}
            try:
                async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S, transport=self.transport) as c:
                    r = await c.post(base + "/v1/chat/completions", json=payload)
            except httpx.HTTPError as e:
                raise LlmError(f"LLM と通信できません: {type(e).__name__}: {e}")
            if r.status_code != 200:
                raise LlmError(f"LLM がエラーを返しました(HTTP {r.status_code}): {r.text[:200]}")
            try:
                content = r.json()["choices"][0]["message"]["content"]
            except (ValueError, KeyError, IndexError, TypeError):
                raise LlmError("LLM の応答を読めませんでした")
            return THOUGHT_RE.sub("", content).strip()

    def status(self) -> dict:
        """画面用の状態。nvidia-smi を呼ぶので、イベントループの外(同期ルート)から呼ぶこと。"""
        running = self.running()
        try:
            gguf, binary = find_gguf(self.cfg), find_binary(self.cfg, self._gpu if running else use_gpu(self.mode))
        except LlmError as e:
            gguf, binary = None, None
            self._error = self._error or e.message
        return {"running": running, "starting": self._starting, "external": self._external,
                "gpu": self._gpu if running else use_gpu(self.mode), "gpu_setting": str(self.mode),
                "free_vram_mib": smi_mib("free"), "starts": self.starts, "port": self.port,
                "gguf": str(gguf) if gguf else None, "binary": str(binary) if binary else None,
                "in_use": self._users, "waiting": bool(self.busy()), "error": self._error}
