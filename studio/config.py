"""設定の読み込み: studio.toml(既定)→ studio.local.toml(個人の上書き)。"""
import copy
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 以前
    import tomli as tomllib

REPO_ROOT = Path(__file__).resolve().parent.parent


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


LLM_DEFAULTS = {"port": 8701, "gpu": "auto", "gguf": "", "llama_server": "", "tools_dir": "components/llm"}


class Settings:
    def __init__(self, data: dict, root: Path):
        self.root = Path(root)
        self.host = str(data.get("host", "127.0.0.1"))
        self.port = int(data.get("port", 8700))
        self.components_dir = str(data.get("components_dir", "components"))
        self.components = {name: dict(tbl) for name, tbl in data.get("components", {}).items()}
        self.llm = {**LLM_DEFAULTS, **data.get("llm", {})}     # 管理役が持つ LLM(llama-server)の設定
        self.llm_port = int(self.llm["port"])

    def component_names(self) -> list[str]:
        return [n for n, c in self.components.items() if c.get("enabled", True)]

    def component_dir(self, name: str) -> Path:
        """部品のフォルダ。dir があればそれ(相対ならリポジトリ直下から)、無ければ <components_dir>/<repo>。"""
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


def load_settings(root: Path = REPO_ROOT) -> Settings:
    root = Path(root)
    data = deep_merge(_read_toml(root / "studio.toml"), _read_toml(root / "studio.local.toml"))
    return Settings(data, root)


settings = load_settings()


def component_dir(name: str) -> Path:
    return settings.component_dir(name)
