"""MiniMax-H3-Image の transformer キャッシュ(<エンジン>/models/cache/<量子化>_adaln_table<ステップ数の接尾辞>)の有無。

エンジン(minimax-h3-image-server の app/engine.py _load_transformer)は、ステップ数ごとに AdaLN テーブル + 量子化済みの transformer を
キャッシュに置く。無いと、最初にそのステップ数で生成したときに bf16 の transformer(約 62GB)をシャード 1 つずつ取得して量子化し、
保存する(1 時間以上。RAM 16GB の PC ではメモリが埋まりきる。2026-11-26、Windows の実機 8GB VRAM / 16GB RAM の報告)。
setup(scripts/setup.py)は prepare_models.py --build-cache で先に作り、管理役は画像タブにキャッシュの無いステップ数を知らせる。

量子化(int8wo / ck-w4a8)はエンジンが起動時に空き VRAM から選ぶ(48gb の構成だけ int8wo、32gb/16gb/8gb は ck-w4a8)ので、
起動前には分からない。H3IMG_TRANSFORMER_QUANT で決め打ちしていればその量子化だけを見る。auto(既定)なら、どちらかの量子化の
キャッシュがあれば「ある」とみなす(大きい GPU で int8wo を作った PC が、空きの少ないときに ck-w4a8 を選ぶことはあるが、まれなので
知らせない側に倒す)。判定はエンジンの実装を写したもの(エンジン側が変わったら合わせる)。
"""
import os
import sys
from pathlib import Path

from . import config
from .config import Settings

COMPONENT = "h3image"
QUANT_DIRS = {"ck-w4a8": "ckw4a8_adaln_table", "int8wo": "int8wo_adaln_table"}   # engine.py _load_transformer
STEP_TAGS = {6: "", 3: "_pdmd3", 2: "_pdmd2"}                                    # config.py STEP_PRESETS の接尾辞
DEFAULT_STEPS = 3                                                                # config.py H3IMG_STEPS の既定
# できあがりの印(pruned_image.py の STATE_FILENAME_ST / STATE_FILENAME)。save_cache は <名前>.tmp に書いてから名前を変えるので、
# 作りかけはこの名前のフォルダにならない
MARKERS = ("transformer_adaln_table.safetensors", "transformer_int8wo_adaln_table.pt")
CACHE_GB = {"ck-w4a8": 11, "int8wo": 19}                                         # ディスク上の大きさの目安(1 ステップ数あたり)


def _is_win(plat: str = None) -> bool:
    return (plat or sys.platform) == "win32"


def engine_env(cfg: Settings = None) -> dict:
    """エンジンが受け取る環境変数(管理役の環境に studio.toml の [components.h3image.env] を重ねたもの。procman.start と同じ)。"""
    cfg = cfg or config.settings
    return {**os.environ, **cfg.component_env(COMPONENT)}


def default_steps(env: dict = None) -> int:
    """エンジンの既定のステップ数(H3IMG_STEPS、無いか不正なら 3)。"""
    env = os.environ if env is None else env
    try:
        v = int(str(env.get("H3IMG_STEPS") or DEFAULT_STEPS).strip())
    except ValueError:
        return DEFAULT_STEPS
    return v if v in STEP_TAGS else DEFAULT_STEPS


def cache_dir(img_dir, env: dict = None) -> Path:
    """キャッシュの置き場所。エンジンと同じく H3IMG_CACHE_DIR → H3IMG_MODELS_DIR/cache → <エンジン>/models/cache
    (相対パスはエンジンのフォルダから。エンジンはそこで起動する)。"""
    env = os.environ if env is None else env
    value = str(env.get("H3IMG_CACHE_DIR") or "").strip()
    if value:
        return Path(img_dir) / value
    models = str(env.get("H3IMG_MODELS_DIR") or "").strip()
    return (Path(img_dir) / models if models else Path(img_dir) / "models") / "cache"


def quants(env: dict = None) -> list:
    """見る量子化。H3IMG_TRANSFORMER_QUANT で決め打ちならそれだけ、auto なら両方。"""
    env = os.environ if env is None else env
    q = str(env.get("H3IMG_TRANSFORMER_QUANT") or "auto").strip()
    return [q] if q in QUANT_DIRS else list(QUANT_DIRS)


def cache_ready(d) -> bool:
    return any((Path(d) / m).is_file() for m in MARKERS)


def status(img_dir, env: dict = None) -> dict:
    """{"dir": 置き場所, "quant": 決め打ちの量子化か None(auto), "default_steps": 既定のステップ数,
    "ready": {ステップ数: あるか}, "by_quant": {量子化: {ステップ数: あるか}}}。"""
    env = os.environ if env is None else env
    d = cache_dir(img_dir, env)
    qs = quants(env)
    by_quant = {q: {s: cache_ready(d / (QUANT_DIRS[q] + tag)) for s, tag in STEP_TAGS.items()} for q in QUANT_DIRS}
    ready = {s: any(by_quant[q][s] for q in qs) for s in STEP_TAGS}
    return {"dir": d, "quant": qs[0] if len(qs) == 1 else None, "default_steps": default_steps(env),
            "ready": ready, "by_quant": by_quant}


def steps_ready(img_dir, steps, env: dict = None) -> bool:
    """そのステップ数(None ならエンジンの既定)のキャッシュがあるか。"""
    st = status(img_dir, env)
    return st["ready"].get(int(steps) if steps else st["default_steps"], True)


def prepare_argv(steps, plat: str = None) -> list:
    """キャッシュを作るコマンド(エンジンのフォルダで、エンジンの venv の python で)。"""
    win = _is_win(plat)
    py = ".venv\\Scripts\\python.exe" if win else ".venv/bin/python"
    script = "scripts\\prepare_models.py" if win else "scripts/prepare_models.py"
    return [py, script, "--build-cache"] + [str(s) for s in (steps if isinstance(steps, (list, tuple)) else [steps])]


def prepare_command(img_dir, steps, plat: str = None) -> str:
    """画面・setup に出す 1 行(Windows は PowerShell 向けに cd; で続ける)。"""
    argv = " ".join(prepare_argv(steps, plat))
    if _is_win(plat):
        return f"cd \"{img_dir}\"; .\\{argv}"
    return f"cd '{img_dir}' && {argv}"
