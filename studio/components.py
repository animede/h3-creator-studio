"""部品カタログ: 4 つの部品の起動コマンド・準備確認・GPU の空け方を 1 か所にまとめる。"""
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .config import Settings, settings as default_settings
from .gpu import smi_mib


class ComponentNotInstalled(Exception):
    """部品のフォルダや実行ファイルが見つからない。hint に次にやることを日本語で入れる。"""

    def __init__(self, name: str, hint: str):
        super().__init__(f"{name}: {hint}")
        self.name = name
        self.hint = hint


def _is_win() -> bool:
    return sys.platform == "win32"


def _find_first(candidates: list[Path]) -> Optional[Path]:
    for c in candidates:
        if c.is_file():
            return c
    return None


def _require_dir(name: str, d: Path) -> None:
    if not d.is_dir():
        raise ComponentNotInstalled(
            name, f"フォルダ {d} がありません。setup を実行するか、studio.local.toml の [components.{name}] の dir に実際の場所を書いてください。")


def _venv_python(name: str, d: Path, order: tuple[str, ...]) -> str:
    _require_dir(name, d)
    sub = "Scripts/python.exe" if _is_win() else "bin/python"
    py = _find_first([d / v / sub for v in order])
    if py is None:
        raise ComponentNotInstalled(
            name, f"{d} に仮想環境({' か '.join(order)})の python がありません。その部品の setup を実行してください。")
    return str(py)


def _uvicorn_cmd(name: str, d: Path, port: int, app: str, order: tuple[str, ...]) -> list[str]:
    py = _venv_python(name, d, order)
    return [py, "-u", "-m", "uvicorn", app, "--host", "127.0.0.1", "--port", str(port)]


@dataclass
class ComponentSpec:
    name: str
    label: str
    dir: Path
    port: int
    health_path: str
    health_ok: Callable[[object], bool]
    uses_gpu: bool
    can_unload: bool
    unload_path: Optional[str]
    reload_path: Optional[str]
    startup_timeout_s: int
    # build_command(dir) と env(dir, port) は部品ごとに差し替える
    build_command: Callable[[Path], list] = None
    env: Callable[[Path, int], dict] = lambda d, p: {}
    extra_env: dict = field(default_factory=dict)   # studio.toml の env
    vram_need_mib: int = 0                          # 1 回の生成に要る VRAM の目安(同居できるかの判断に使う)
    vram_budget_gb: Optional[float] = None          # share のときの VRAM の予算の上書き(studio.toml の vram_budget_gb)
    budget_env: Callable[[int], dict] = lambda mib: {}   # share で起動するとき、予算(MiB)を環境変数にして渡す(exclusive では使わない)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def health_url(self) -> str:
        return self.base_url + self.health_path


# ---- 部品ごとの起動コマンド ----

def _h3_command(d: Path, port: int) -> list:
    return _uvicorn_cmd("h3", d, port, "app:app", ("venv", ".venv"))


def _h3image_command(d: Path, port: int) -> list:
    return _uvicorn_cmd("h3image", d, port, "app.main:app", (".venv", "venv"))


def _ace_command(d: Path) -> list:
    _require_dir("ace", d)
    script = "run_api_server_fast.bat" if _is_win() else "run_api_server_fast.sh"
    if not (d / script).is_file():
        raise ComponentNotInstalled("ace", f"{d} に {script} がありません。setup を実行するか、studio.local.toml で dir を指定してください。")
    return ["cmd", "/c", script] if _is_win() else ["bash", script]


def _aivis_command(d: Path, port: int) -> list:
    _require_dir("aivis", d)
    exe, sub = ("run.exe", "Windows-x64") if _is_win() else ("run", "Linux-x64")
    path = _find_first([d / exe, d / sub / exe])
    if path is None:
        raise ComponentNotInstalled(
            "aivis", f"{d} に {exe} がありません(配布版が必要です)。setup を実行するか、studio.local.toml で配布版のフォルダを dir に指定してください。")
    return [str(path), "--host", "127.0.0.1", "--port", str(port)]   # --use_gpu は付けない(CPU で動かす)


LOW_VRAM_MIB = 10 * 1024


def ace_gpu_env(total_mib: Optional[int]) -> dict:
    """VRAM が 10GiB 未満(8GB のカード)のときだけ、実機で確かめた ACE-Step の省メモリ設定を足す。

    それ以上は何も指定しない(ACE の起動スクリプトが 12gb / 16gb のプロファイルを自動で選ぶ)。
    """
    if total_mib is None or total_mib >= LOW_VRAM_MIB:
        return {}
    return {"ACESTEP_GPU_PROFILE": "12gb", "ACESTEP_LM_OFFLOAD_TO_CPU": "1", "ACESTEP_RESIDENT_COMPONENTS": "none",
            "ACESTEP_VAE_DECODE_CHUNK_SIZE": "128", "ACESTEP_QUANTIZATION": "int8_weight_only"}


# ---- share のときの VRAM の予算 ----

H3_PROFILES = (96, 80, 48, 32, 16, 8)       # H3 の H3_PROFILE が受ける級(GiB)


def default_budget_gb(name: str, total_mib: int) -> Optional[float]:
    """share のときの、部品ごとの既定の予算(GiB)。総 VRAM T に対して H3 = min(T×0.5, 48)、H3-Image = min(T×0.35, 31)、ACE = 14。CPU の部品は None。"""
    total_gb = total_mib / 1024
    return {"h3": min(total_gb * 0.5, 48), "h3image": min(total_gb * 0.35, 31), "ace": 14}.get(name)


def h3_profile(budget_gb: float) -> str:
    """予算(GiB)に収まる最大の級(96gb / 80gb / 48gb / 32gb / 16gb / 8gb)。8GiB に満たなければ最小の 8gb。"""
    return f"{next((g for g in H3_PROFILES if g <= budget_gb), H3_PROFILES[-1])}gb"


def _gb_text(mib: int) -> str:
    gb = mib / 1024
    return str(int(gb)) if gb == int(gb) else f"{gb:.1f}"


def h3_budget_env(budget_mib: int) -> dict:
    return {"H3_PROFILE": h3_profile(budget_mib / 1024), "H3_VRAM_LIMIT_GB": _gb_text(budget_mib)}


def h3image_budget_env(budget_mib: int) -> dict:
    return {"H3IMG_VRAM_GB": str(int(budget_mib // 1024))}      # 整数の GiB(切り捨て。予算を超えない)


def ace_budget_env(budget_mib: int) -> dict:
    """ACE のプロファイルは、予算が 14GiB 以上なら 16gb、それ未満は 12gb。10GiB 未満は 8GB のカード向けの省メモリ設定(ace_gpu_env)も足す。"""
    return {"ACESTEP_GPU_PROFILE": "16gb" if budget_mib >= 14 * 1024 else "12gb", **ace_gpu_env(budget_mib)}


def get_spec(name: str, cfg: Settings = None) -> ComponentSpec:
    """設定(dir / port / env)を反映した ComponentSpec を返す。"""
    cfg = cfg or default_settings
    d = cfg.component_dir(name)
    extra = cfg.component_env(name)

    def make(label, default_port, **kw):
        port = cfg.component_port(name, default_port)
        spec = ComponentSpec(name=name, label=label, dir=d, port=port, extra_env=extra, vram_budget_gb=cfg.component_budget_gb(name), **kw)
        return spec

    if name == "h3":
        spec = make("MiniMax-H3(動画・静止画)", 8611, health_path="/api/status", health_ok=lambda j: True,
                    uses_gpu=True, can_unload=True, unload_path="/api/admin/unload", reload_path="/api/admin/reload", vram_need_mib=12000,
                    startup_timeout_s=900)
        spec.build_command = lambda dd: _h3_command(dd, spec.port)
        spec.budget_env = h3_budget_env
        # 部品の内蔵 LLM は使わず、管理役の LLM(llama-server)を使う
        spec.env = lambda dd, p: {"H3_LLM_LOCAL": "0", "H3_LLM_URL": f"http://127.0.0.1:{cfg.llm_port}",
                                      "PYTORCH_CUDA_ALLOC_CONF": os.environ.get("PYTORCH_CUDA_ALLOC_CONF") or "expandable_segments:True"}
    elif name == "h3image":
        spec = make("MiniMax-H3-Image(画像)", 8633, health_path="/api/health", health_ok=lambda j: True,
                    uses_gpu=True, can_unload=True, unload_path="/api/admin/unload", reload_path="/api/admin/load", vram_need_mib=13500,
                    startup_timeout_s=900)
        spec.build_command = lambda dd: _h3image_command(dd, spec.port)
        spec.budget_env = h3image_budget_env
        spec.env = lambda dd, p: {"H3IMG_LLM_BASE_URL": f"http://127.0.0.1:{cfg.llm_port}/v1", "H3IMG_LLM_MODEL": "gemma-4-E4B",
                                      "PYTORCH_CUDA_ALLOC_CONF": os.environ.get("PYTORCH_CUDA_ALLOC_CONF") or "expandable_segments:True"}
    elif name == "ace":
        spec = make("ACE-Step(音楽)", 8001, health_path="/health",
                    health_ok=lambda j: bool(isinstance(j, dict) and (j.get("data") or {}).get("models_initialized")),
                    uses_gpu=True, can_unload=False, unload_path=None, reload_path=None, startup_timeout_s=1200, vram_need_mib=14000)
        spec.build_command = _ace_command
        spec.budget_env = ace_budget_env
        spec.env = lambda dd, p: {"ACESTEP_API_HOST": "127.0.0.1", "ACESTEP_API_PORT": str(p), **ace_gpu_env(smi_mib("total"))}
    elif name == "aivis":
        spec = make("AivisSpeech(音声合成)", 10101, health_path="/version", health_ok=lambda j: True,
                    uses_gpu=False, can_unload=False, unload_path=None, reload_path=None, startup_timeout_s=180)
        spec.build_command = lambda dd: _aivis_command(dd, spec.port)
    else:
        raise KeyError(f"未知の部品: {name}")
    return spec


def all_specs(cfg: Settings = None) -> dict:
    cfg = cfg or default_settings
    return {n: get_spec(n, cfg) for n in cfg.component_names() if n in ("h3", "h3image", "ace", "aivis")}
