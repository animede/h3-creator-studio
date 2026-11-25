"""管理役が持つ LLM: llama.cpp の llama-server(CUDA 版)で Gemma 4 E4B を動かす。プロンプト強化と作詞に使う。

考え方は ACE-Step-1.5-fast の easy-music(services/local_llm_service.py)を引き継ぐ:
- 最初に使うときに起こす(lazy)。`--fit on --fit-target 1536` で、いま GPU に載っているエンジンの隣の空き VRAM に
  入る分だけ GPU に載せ、残りは CPU で動かす。
- GPU で生成する直前に、GPU を使っている子を止めて VRAM を返す(release_before_gpu)。
- GPU の仕事が走っている(順番を待っている)間は、chat を待たせず、CUDA を隠した CPU 専用の子で動かす(遅くはなる)。
  仕事が無くなって次に使うときに、GPU 版へ戻す(行き来しないよう、仕事が走っても待ってもいないときだけ)。
- 子プロセスは procman で起動するので、管理役が死ねば道連れになる(Linux は PDEATHSIG、Windows は Job Object)。
- 画像・音声を読む(MV の人物の説明と曲の聞き取り)には、投影ファイル(mmproj。Gemma 4 E4B のものは画像と音声の入力を持つ)を付けて起動する。
  あれば常に付け(CPU 専用でも)、無ければ画像・音声を頼まれたときに 1 回だけ取得する。投影ファイルが無くても、テキストの chat は動く。
"""
import asyncio
import base64
import contextlib
import io
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
MMPROJ_FILE, MMPROJ_SIZE_HINT = "mmproj-gemma-4-E4B-it-Q8_0.gguf", "約 0.56GB"       # 画像・音声を読む投影ファイル(同じリポジトリ)
IMAGE_MAX_SIDE = 1024      # LLM に渡す画像は、長辺をこれまでに縮める
FIT_TARGET_MIB = 1536      # --fit が GPU に残す余白(MiB)
N_CTX = 8192
HEALTH_TIMEOUT_S = 300     # 初回はモデルの読み込みに時間がかかる
REQUEST_TIMEOUT_S = 600    # CPU に溢れた分の推論は遅い
SESSION_WAIT_S = 300       # release_before_gpu が、使用中の chat の終わりを待つ最長
SETTLE_S = 2.5             # GPU 版を止めたあと、nvidia-smi の監視(2 秒おき)が空きを反映するのを待つ
CPU_ONLY_ENV = {"CUDA_VISIBLE_DEVICES": "-1"}   # CPU 専用で起動するときは、GPU を見せない(CUDA の文脈も作らせない)。空文字は Windows で未設定と同じ扱いになることがあるので -1

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
    """[llm] gguf → tools_dir → 他のエンジンがすでに持っているコピー(ACE の easy-music、h3 の models/llm)の順。無ければ None。"""
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


def find_mmproj(cfg: Settings, gguf: Optional[Path] = None) -> Optional[Path]:
    """画像・音声を読む投影ファイル。[llm] mmproj → 見つかった GGUF の隣 → tools_dir の順。無ければ None(無くてもテキストの chat は動く)。
    [llm] mmproj のファイルが無いときは LlmError(find_gguf と同じ。起動では画像・音声を読めないだけにして、テキストの chat は続ける)。"""
    override = str(cfg.llm["mmproj"]).strip()
    if override:
        p = Path(override).expanduser()
        if not p.is_file():
            raise LlmError(f"[llm] mmproj のファイルがありません: {p}")
        return p
    if gguf is None:
        try:
            gguf = find_gguf(cfg)
        except LlmError:
            gguf = None
    for d in ((gguf.parent,) if gguf else ()) + (tools_dir_of(cfg),):
        if (d / MMPROJ_FILE).is_file():
            return d / MMPROJ_FILE
    return None


def _download(cfg: Settings, filename: str, hint: str, setting: str) -> Path:
    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        raise LlmError("huggingface_hub がありません。`pip install huggingface_hub` するか、"
                       f"studio.local.toml の [llm] {setting} にファイルのパスを書いてください")
    dest = tools_dir_of(cfg)
    dest.mkdir(parents=True, exist_ok=True)
    log.info("モデルを取得しています: %s/%s(%s)", MODEL_REPO, filename, hint)
    return Path(hf_hub_download(repo_id=MODEL_REPO, filename=filename, local_dir=str(dest)))


def download_model(cfg: Settings) -> Path:
    return _download(cfg, MODEL_FILE, MODEL_SIZE_HINT, "gguf")


def download_mmproj(cfg: Settings) -> Path:
    return _download(cfg, MMPROJ_FILE, MMPROJ_SIZE_HINT, "mmproj")


def shrink_image(data: bytes, mime: str, max_side: int = IMAGE_MAX_SIDE) -> tuple:
    """LLM に渡す画像を整える: (バイト列, MIME)。長辺が max_side を超えるときは縮め、llama.cpp が読めない形式(WebP など)・透明つき・
    向きの印つきの画像は JPEG にし直す(透明は白で埋める)。小さな RGB の PNG / JPEG はそのまま。Pillow が無い・読めないときも、そのまま返す。"""
    try:
        from PIL import Image, ImageOps
        with Image.open(io.BytesIO(data)) as im:
            fmt, (w, h), turned = (im.format or "").upper(), im.size, im.getexif().get(0x0112, 1) != 1
            if fmt in ("PNG", "JPEG") and im.mode in ("RGB", "L") and max(w, h) <= max_side and not turned:
                return data, "image/png" if fmt == "PNG" else "image/jpeg"
            im = ImageOps.exif_transpose(im)
            if max(im.size) > max_side:
                k = max_side / max(im.size)
                im = im.resize((max(1, round(im.width * k)), max(1, round(im.height * k))), Image.LANCZOS)
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                flat = Image.new("RGB", im.size, (255, 255, 255))
                flat.paste(im, mask=im.getchannel("A"))
                im = flat
            buf = io.BytesIO()
            im.convert("RGB").save(buf, "JPEG", quality=90)
            return buf.getvalue(), "image/jpeg"
    except Exception:  # noqa: BLE001  Pillow が無い・画像として読めない: そのまま渡す(LLM が断れば、その理由が出る)
        return data, mime


def build_command(exe: Path, gguf: Path, port: int, gpu: bool, mode, cpu_only: bool = False, mmproj: Optional[Path] = None) -> list:
    """cpu_only: CUDA 版の llama-server を、GPU に 1 層も載せずに起動する(環境変数 CPU_ONLY_ENV と組み合わせる)。
    mmproj: 画像・音声を読む投影ファイル(あれば付ける。CPU 専用でも付ける)。"""
    cmd = [str(exe), "-m", str(gguf), "--host", "127.0.0.1", "--port", str(port), "-c", str(N_CTX),
           "-t", str(max(4, (os.cpu_count() or 8) // 2)), "--reasoning", "off"]
    if mmproj:
        cmd += ["--mmproj", str(mmproj)]
    if gpu and cpu_only:
        cmd += ["-ngl", "0", "--fit", "off"]
    elif gpu:
        if mode == "auto":     # 空き VRAM に入る層数だけ GPU へ(他のエンジンが載っていても共存できる)
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
    env_add: dict = field(default_factory=dict)       # 起動のたびに決まる環境変数(CPU 専用なら CUDA を隠す)

    def build_command(self, d) -> list:
        return self.cmd

    def env(self, d, p) -> dict:
        return dict(self.env_add)


class Llm:
    """llama-server の子プロセス 1 つ。busy() が True の間(GPU の仕事が走っている・待っている間)は、CPU 専用で動かす(待たせない)。"""

    def __init__(self, cfg: Settings, busy: Callable[[], bool] = lambda: False, transport=None,
                 poll_s: float = 0.5, settle_s: float = SETTLE_S):
        self.cfg, self.busy, self.transport = cfg, busy, transport
        self.port, self.mode = cfg.llm_port, parse_gpu_setting(cfg.llm["gpu"])
        self.poll_s, self.settle_s = poll_s, settle_s
        self.starts = 0
        self._proc: Optional[procman.Proc] = None
        self._external = False        # 同じポートで、すでに別の llama-server が動いていた
        self._gpu = False             # いまの子プロセスが GPU を使っているか(VRAM を持っているか)
        self._cpu_only = False        # いまの子プロセスは CUDA 版を、GPU を隠して起動したもの(VRAM を持たない)
        self._with_projector = False        # いまの子プロセスは、投影ファイル(--mmproj)つきで起動したもの(画像・音声を読める)
        self._projector_failed = False      # 投影ファイルつきでは起動できなかった(なしで起動し直した)。画像・音声のたびに起動し直しを繰り返さない
        self._releasing = False       # release_before_gpu が GPU 版を止めようとしている(止まるまで、新しい chat は入れない)
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

    async def ensure_running(self, projector: bool = False) -> str:
        """llama-server を使える状態にして、base URL を返す。起動に失敗したら LlmError。
        projector: 画像・音声を読むために呼ぶ。投影ファイル無しで起動した自分の子プロセスは、他に使っている chat が無ければ、投影ファイルつきで起動し直す。"""
        async with self._start_lock:
            if self._proc is not None and not procman.is_alive(self._proc):
                self._proc = None
            if self.running() and await self._healthy():
                if self._cpu_only and await self._should_return_to_gpu():
                    log.info("LLM: GPU の仕事が無いので、CPU 専用の llama-server を GPU 版に戻します")
                elif projector and self._lacks_projector():
                    log.info("LLM: 画像・音声を読むため、投影ファイルつきで llama-server を起動し直します")
                else:
                    return self.base_url
                await self.stop()
            elif self._proc is not None:
                await self.stop()             # 生きているのに応答しない
            elif await self._healthy():
                log.info("ポート %d で llama-server が既に動いているので、それを使います(停止はしません)", self.port)
                self._external, self._cpu_only = True, False
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

    async def _should_return_to_gpu(self) -> bool:
        """CPU 専用で動いている子を、GPU 版に戻してよいか。GPU の仕事が走っても待ってもいなくて、他に使っている chat が無いときだけ。"""
        if self._external or self._proc is None or self._releasing or self._users > 1 or self.busy():
            return False
        return await asyncio.to_thread(use_gpu, self.mode)

    def _lacks_projector(self) -> bool:
        """自分の子プロセスが投影ファイル無しで動いていて、いまは投影ファイルがあり、起動し直しても他の chat を切らないか。"""
        if self._external or self._proc is None or self._with_projector or self._projector_failed or self._releasing or self._users > 1:
            return False
        try:
            return find_mmproj(self.cfg) is not None
        except LlmError:
            return False

    async def _start(self) -> None:
        gpu = await asyncio.to_thread(use_gpu, self.mode)
        cpu_only = gpu and bool(self.busy())       # GPU の仕事が走っている(待っている)間は、GPU を隠して CPU だけで動かす
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
        try:
            mmproj = find_mmproj(self.cfg, gguf)       # 画像・音声を読む投影ファイル。取得は、画像・音声を頼まれたときだけ(テキストの起動を遅らせない)
        except LlmError as e:
            log.warning("%s(画像・音声は読めません。テキストの chat は動きます)", e.message)
            mmproj = None
        log.info("LLM: %s / %s%s / GPU 設定 %s%s / 空き VRAM %s MiB", exe, gguf.name, f" + {mmproj.name}" if mmproj else "", self.mode,
                 "(GPU の仕事の間は CPU 専用)" if cpu_only else "", await asyncio.to_thread(smi_mib, "free"))
        try:
            await self._spawn(exe, gguf, gpu, cpu_only, mmproj)
        except LlmError as e:
            if mmproj is None or not getattr(e, "exited", False):
                raise
            # 投影ファイルを読み込めずに終了した(VRAM が足りない・壊れたファイルなど): テキストの chat を守るため、投影ファイルなしで起動し直す
            log.warning("投影ファイルつきでは起動できませんでした。画像・音声なしで起動し直します: %s", e.message)
            self._projector_failed = True
            await self._spawn(exe, gguf, gpu, cpu_only, None)

    async def _spawn(self, exe: Path, gguf: Path, gpu: bool, cpu_only: bool, mmproj: Optional[Path]) -> None:
        """llama-server の子プロセスを起動し、応答するまで待つ。途中で終了したら LlmError(exited = True)。"""
        spec = LlmSpec(dir=exe.parent, port=self.port, cmd=build_command(exe, gguf, self.port, gpu, self.mode, cpu_only, mmproj),
                       env_add=dict(CPU_ONLY_ENV) if cpu_only else {})
        self._proc, self._gpu, self._cpu_only = procman.start(spec), gpu and not cpu_only, cpu_only    # Linux の PDEATHSIG のため、イベントループのスレッドから起動する
        self._with_projector = mmproj is not None
        self.starts += 1
        waited = 0.0
        while waited < HEALTH_TIMEOUT_S:
            if not procman.is_alive(self._proc):
                code = self._proc.popen.returncode if self._proc.popen else "?"
                self._proc = None
                err = LlmError(f"llama-server が途中で終了しました(終了コード {code})。{procman.log_tail('llm', 8)}")
                err.exited = True
                raise err
            if await self._healthy():
                log.info("LLM の準備ができました(%.0f 秒、%s)", waited, "GPU あり" if self._gpu else "CPU")
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
        self._cpu_only = False
        await asyncio.to_thread(procman.stop, proc)
        return True

    async def release_before_gpu(self) -> bool:
        """GPU エンジンを載せる直前に呼ぶ。GPU を使っている子プロセスが居れば止めて VRAM を返す(次の chat で、CPU 専用の子として起こし直す)。

        chat / 強化の最中なら、その終わりを待ってから止める。CPU だけで動いている子は VRAM を持たないので、待たず、止めない。
        """
        if self._proc is None or not self._gpu:
            return False
        self._releasing = True       # 止める子に新しい chat を載せない(止まるまでの数秒だけ、入れずに待たせる)
        try:
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
        finally:
            self._releasing = False

    # ---- 使う ----

    @contextlib.asynccontextmanager
    async def session(self):
        """LLM を使う区間。GPU の仕事が走っていても入れる(CPU 専用の子で動く)。入っている間は release_before_gpu が止めるのを待つ。"""
        while self._releasing:       # GPU 版を止めている最中だけは、止まるまで待つ(止まる子に chat を載せないため)
            await asyncio.sleep(self.poll_s)
        self._users += 1       # _releasing の確認から加算までに await が無いので、すれ違わない
        try:
            yield
        finally:
            self._users -= 1

    async def chat(self, system: str, user: str, max_tokens: int = 2000, temperature: float = 0.8) -> str:
        async with self.session():
            base = await self.ensure_running()
            return await self._complete(base, [{"role": "system", "content": system}, {"role": "user", "content": user}], max_tokens, temperature)

    async def _complete(self, base: str, messages: list, max_tokens: int, temperature: float) -> str:
        payload = {"messages": messages, "max_tokens": max_tokens, "temperature": temperature, "top_p": 0.95, "top_k": 64, "repeat_penalty": 1.1}
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

    async def _ensure_projector(self) -> None:
        """画像・音声を読む投影ファイルがあるようにする。無ければ 1 回だけ取得を試み、取れなければ対処つきの LlmError。
        別の llama-server がすでに動いている(外部)ときは、そちらの設定なので何もしない。"""
        if await asyncio.to_thread(find_mmproj, self.cfg) is not None or (self._proc is None and await self._healthy()):
            return
        try:
            await asyncio.to_thread(download_mmproj, self.cfg)
        except Exception as e:  # noqa: BLE001  ネットワーク・ディスクなど
            raise LlmError(f"画像・音声を読む投影ファイル({MMPROJ_FILE}、{MMPROJ_SIZE_HINT})を取得できませんでした: {getattr(e, 'message', e)}。"
                           "ネットワークを確かめて `setup --llm` で取得するか、studio.local.toml の [llm] mmproj にファイルのパスを書いてください")

    async def chat_image(self, system: str, user: str, image_bytes: bytes, mime: str = "image/png", max_tokens: int = 700, temperature: float = 0.0) -> str:
        """画像を見て答える(MV の人物の説明)。OpenAI 互換のメッセージで、画像は data URI。大きな画像は縮めてから渡す(IMAGE_MAX_SIDE)。
        投影ファイルが無ければ、まず 1 回取得を試みる(初回は約 0.56GB)。"""
        data, mime = await asyncio.to_thread(shrink_image, image_bytes, mime)
        uri = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
        return await self._chat_media("画像", system, [{"type": "text", "text": user}, {"type": "image_url", "image_url": {"url": uri}}], max_tokens, temperature)

    async def chat_audio(self, system: str, user: str, clips: list, max_tokens: int = 400, temperature: float = 0.0) -> str:
        """音声を聞いて答える(MV の曲の聞き取り)。clips は WAV(16kHz モノラル、1 つ 30 秒まで)のバイト列の並びで、OpenAI 互換の input_audio で渡す。
        投影ファイルは画像と同じもの(Gemma 4 E4B の mmproj が音声の入力も持つ。llama.cpp では実験段階)。"""
        parts = [{"type": "text", "text": user}] + [{"type": "input_audio", "input_audio": {"data": base64.b64encode(c).decode("ascii"), "format": "wav"}} for c in clips]
        return await self._chat_media("音声", system, parts, max_tokens, temperature)

    async def _chat_media(self, what: str, system: str, parts: list, max_tokens: int, temperature: float) -> str:
        """画像・音声つきの chat(what は「画像」か「音声」)。投影ファイルを用意し、投影ファイルつきの子プロセスで頼む。"""
        async with self.session():
            await self._ensure_projector()
            base = await self.ensure_running(projector=True)
            if self._projector_failed and not self._with_projector and not self._external:
                raise LlmError(f"この LLM は{what}を読めません(投影ファイルを読み込めなかったので、画像・音声なしで起動しています)。"
                               "logs/llm.log を確かめて、投影ファイルを取得し直す(`setup --llm`)か、[llm] mmproj を直してから、管理役を再起動してください")
            try:
                return await self._complete(base, [{"role": "system", "content": system}, {"role": "user", "content": parts}], max_tokens, temperature)
            except LlmError as e:
                if any(w in e.message.lower() for w in ("mmproj", "image input", "audio input", "multimodal")):      # 投影ファイル無しで動いている llama-server
                    raise LlmError(f"この LLM は{what}を読めません(投影ファイル mmproj が読み込まれていません)。他の仕事が終わってからもう一度試すか、"
                                   "llama-server を止めて(管理役の再起動でも)投影ファイルつきで起動し直してください") from e
                raise

    def status(self) -> dict:
        """画面用の状態。nvidia-smi を呼ぶので、イベントループの外(同期ルート)から呼ぶこと。"""
        running = self.running()
        try:
            gguf, binary = find_gguf(self.cfg), find_binary(self.cfg, (self._gpu or self._cpu_only) if running else use_gpu(self.mode))
        except LlmError as e:
            gguf, binary = None, None
            self._error = self._error or e.message
        try:
            mmproj = find_mmproj(self.cfg, gguf)
        except LlmError:
            mmproj = None
        return {"running": running, "starting": self._starting, "external": self._external,
                "gpu": self._gpu if running else use_gpu(self.mode), "cpu_only": self._cpu_only if running else False,
                "gpu_setting": str(self.mode),
                "free_vram_mib": smi_mib("free"), "starts": self.starts, "port": self.port,
                "gguf": str(gguf) if gguf else None, "mmproj": str(mmproj) if mmproj else None, "binary": str(binary) if binary else None,
                "in_use": self._users, "gpu_work": bool(self.busy()), "error": self._error}
