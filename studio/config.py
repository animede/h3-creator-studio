"""設定の読み込み: studio.toml(既定)→ studio.local.toml(個人の上書き)。"""
import copy
import logging
from pathlib import Path
from typing import Optional

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 以前
    import tomli as tomllib

REPO_ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("studio.config")


def deep_merge(base: dict, over: dict) -> dict:
    """テーブルは再帰的に混ぜ、それ以外の値は over が勝つ。"""
    out = copy.deepcopy(base)
    for key, val in over.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], val)
        else:
            out[key] = copy.deepcopy(val)
    return out


def _read_toml(path: Path) -> dict:
    if not path.is_file():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


LLM_DEFAULTS = {"port": 8701, "gpu": "auto", "gguf": "", "mmproj": "", "llama_server": "", "tools_dir": "components/llm"}
GPU_MODES = ("auto", "exclusive", "share")


def parse_gpu_mode(value) -> str:
    """[gpu] mode の値を "auto" / "exclusive" / "share" にそろえる。読めなければ auto。"""
    v = str(value).strip().lower()
    if v in GPU_MODES:
        return v
    log.warning("[gpu] mode = %r は読めません(auto / exclusive / share のいずれか)。auto として扱います", value)
    return "auto"


class Settings:
    def __init__(self, data: dict, root: Path):
        self.root = Path(root)
        self.host = str(data.get("host", "127.0.0.1"))
        self.port = int(data.get("port", 8700))
        self.components_dir = str(data.get("components_dir", "components"))
        self.components = {name: dict(tbl) for name, tbl in data.get("components", {}).items()}
        self.llm = {**LLM_DEFAULTS, **data.get("llm", {})}     # 管理役が持つ LLM(llama-server)の設定
        self.llm_port = int(self.llm["port"])
        self.gpu_mode = parse_gpu_mode(data.get("gpu", {}).get("mode", "auto"))     # GPU に載せるエンジンを 1 つにするか、複数にするか
        self.gpu_devices = str(data.get("gpu", {}).get("devices", "auto"))   # 2 枚以上の GPU のとき、エンジンを寄せる GPU(auto / all / 番号)
        # RAM の少ない PC で、GPU の仕事の前に待機中の読み上げ・同梱 LLM を止めるか(auto = 搭載 RAM 32GiB 未満のとき / always / never)
        self.free_cpu_engines = str(data.get("gpu", {}).get("free_cpu_engines", "auto")).strip().lower()
        self.mv = dict(data.get("mv", {}))     # MV・ショート動画の音声処理などの設定。tools_python = Demucs と librosa の入った Python

    def component_names(self) -> list[str]:
        return [n for n, c in self.components.items() if c.get("enabled", True)]

    def component_dir(self, name: str) -> Path:
        """エンジンのフォルダ。dir があればそれ(相対ならリポジトリ直下から)、無ければ <components_dir>/<repo>。"""
        cfg = self.components.get(name, {})
        if cfg.get("dir"):
            p = Path(str(cfg["dir"])).expanduser()
        else:
            p = Path(self.components_dir) / str(cfg.get("repo", name))
        return p if p.is_absolute() else self.root / p

    def component_port(self, name: str, default: int) -> int:
        return int(self.components.get(name, {}).get("port", default))

    def component_env(self, name: str) -> dict:
        return {str(k): str(v) for k, v in self.components.get(name, {}).get("env", {}).items()}

    def component_budget_gb(self, name: str) -> Optional[float]:
        """share のときの VRAM の利用枠(GiB)の上書き(vram_budget_gb)。書かれていなければ None(総 VRAM から決める)。"""
        v = self.components.get(name, {}).get("vram_budget_gb")
        if v in (None, ""):
            return None
        try:
            gb = float(v)
        except (TypeError, ValueError):
            log.warning("[components.%s] vram_budget_gb = %r は数字ではありません。無視します", name, v)
            return None
        return gb if gb > 0 else None


def load_settings(root: Path = REPO_ROOT) -> Settings:
    root = Path(root)
    data = deep_merge(_read_toml(root / "studio.toml"), _read_toml(root / "studio.local.toml"))
    return Settings(data, root)


settings = load_settings()


def component_dir(name: str) -> Path:
    return settings.component_dir(name)
