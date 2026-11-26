"""MiniMax-H3 の量子化キャッシュ(<エンジン>/models/prequant)の有無と、参照モード(Ref2VA / Ref2I)にキャッシュが要るかどうか。

エンジン(minimax-h3 の core/runner.py)は、重みを量子化キャッシュのファイルから流す構成(H3_GROUP_STREAM_HOST=file。Windows と、
搭載 RAM が 48GB 未満の PC で自動で選ばれる)で参照モード用のキャッシュ(transformer_ref_int8)が無いと、Ref2VA / Ref2I を断る
(bf16 の 66GB から RAM の上で量子化できないため)。MV は曲の分離と演出に数分かけてから最初のシーンで断られる(2026-11-26、Windows の
実機の報告)ので、管理役が先に確かめて、作り方(エンジンの scripts/prepare_lowvram.py --ref)を出す。setup(scripts/setup.py)も、
既に入れてあるエンジンのキャッシュをここで確かめる。判定はエンジンの実装を写したもの(エンジン側が変わったら合わせる)。
"""
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Optional

from . import config
from .config import Settings
from .i18n import msg

COMPONENT = "h3"
BASE_CACHE, REF_CACHE = "transformer_int8", "transformer_ref_int8"     # 本体(T2VA / FL2VA / T2I)と参照モード用
FILE_STREAM_BELOW_GB = 48.0         # エンジンの H3_GROUP_STREAM_FILE_BELOW_GB の既定(10 進の GB)
ENGINE_REFUSAL = "int8 cache of transformer_ref"      # エンジンが参照モードを断るときの英文(RuntimeError)の一部
MISSING_TEMPLATE = ("MiniMax-H3 の参照モード用のキャッシュ(transformer_ref_int8、約 32GB)がありません。MV・参照画像を使うショート動画・"
                    "動画タブの参照モードに要ります。スタジオを止めてから、次を実行して作ってください(数時間。途中で止めると、作りかけの分は"
                    "再実行で最初から作り直します): {command}。または setup.bat --only h3(Linux は ./setup.sh --only h3)でも作れます")


def _is_win(plat: str = None) -> bool:
    return (plat or sys.platform) == "win32"


def engine_env(cfg: Settings = None) -> dict:
    """エンジンが受け取る環境変数。管理役は自分の環境に studio.toml の [components.h3.env] を重ねて渡す(procman.start)。"""
    cfg = cfg or config.settings
    return {**os.environ, **cfg.component_env(COMPONENT)}


# ---- キャッシュの場所と有無 ----

def prequant_dir(h3_dir, env: dict = None) -> Path:
    """量子化キャッシュの置き場所。エンジンと同じく H3_TRANSFORMER_PREQUANT_DIR → H3_TE_PREQUANT_DIR → <エンジン>/models/prequant
    (相対パスはエンジンのフォルダから。エンジンはそこで起動する)。"""
    env = os.environ if env is None else env
    for key in ("H3_TRANSFORMER_PREQUANT_DIR", "H3_TE_PREQUANT_DIR"):
        value = str(env.get(key) or "").strip()
        if value:
            return Path(h3_dir) / value       # 絶対パスならそのまま
    return Path(h3_dir) / "models" / "prequant"


# 32B のテキストエンコーダの量子化済み(Qwen3-VL-32B を 51 層にして nf4 にしたもの)。MiniMax-H3 は 32gb / 48gb 級(H3_TE_PRUNE=1。
# およそ VRAM 20〜48GB のカード、またはスタジオの共有モード)で動いたときに、ここへ保存する(core/runner.py _te_prequant_dir)。
# 80gb / 96gb 級は層を減らさない別の名前(te_bnb-4bit_prune0)で保存し、prepare_lowvram.py は作らない(8〜16GB の級は小さい代わりの
# エンコーダ(4B + 投影)を使う)。画像のエンジン(MiniMax-H3-Image)のテキストエンコーダと同じもので、setup はあれば画像のエンジンにも
# 使わせる(66.7GB の取得と量子化を省く)
SHARED_TE_NAME = "te_bnb-4bit_prune1"


def te_prequant_dir(h3_dir, env: dict = None) -> Path:
    """テキストエンコーダの量子化済みの置き場所。エンジンと同じく H3_TE_PREQUANT_DIR → <エンジン>/models/prequant。"""
    env = os.environ if env is None else env
    value = str(env.get("H3_TE_PREQUANT_DIR") or "").strip()
    return Path(h3_dir) / value if value else Path(h3_dir) / "models" / "prequant"


def shared_te(h3_dir, env: dict = None) -> Optional[Path]:
    """動画のエンジンが作った 32B のテキストエンコーダ(できあがったもの。保存は一時フォルダからの名前の変更なので、config.json が
    あれば揃っている)。無ければ None。"""
    d = te_prequant_dir(h3_dir, env) / SHARED_TE_NAME
    return d if (d / "config.json").is_file() else None


def cache_ready(d) -> bool:
    """キャッシュのフォルダができあがっているか。prepare_lowvram.py もエンジンも <名前>.tmp に作ってから名前を変えるので、作りかけは
    この名前にならない。エンジンの読み込みは config.json と meta.json の両方を要る(meta.json が無いと bf16 から作り直そうとする)。"""
    return (Path(d) / "config.json").is_file() and (Path(d) / "meta.json").is_file()


def cache_status(h3_dir, env: dict = None) -> dict:
    """{"base": 本体のキャッシュがあるか, "ref": 参照モード用があるか, "dir": 置き場所}。"""
    d = prequant_dir(h3_dir, env)
    return {"base": cache_ready(d / BASE_CACHE), "ref": cache_ready(d / REF_CACHE), "dir": d}


# ---- 準備(prepare_lowvram.py)で残った bf16 の原本 ----
# prepare はシャードを 1 つずつ落として int8 化し、読み終わったら消すが、Windows では Defender の検査などがファイルを掴んでいて
# 消せないことがあり、警告を出して残したまま続ける(1 本 約 4.6GB。2026-11-26、Windows 10 の実機でディスクが足りなくなった)。
# 新しいエンジンは最後にもう一度消す(sweep_sources)が、古いエンジンで作った分と、それでも残った分を setup が片づける。
# 消すのは、int8 キャッシュが完成していて、かつエンジンがそのキャッシュから流す構成(file streaming: Windows か RAM 48GB 未満)のときだけ。
# bf16 を直接読む構成(RAM 48GB 以上の Linux、96GB 級)では原本をエンジンが使うので触らない。

HF_MODEL_DIR = "models--MiniMaxAI--MiniMax-H3"
SHARD_GLOB = "diffusion_pytorch_model-*-of-*.safetensors"
TE_SHARD_GLOB = "model-*-of-*.safetensors"          # text_encoder(32B、14 本 66.7GB)。画像のエンジンの準備がここから量子化する
BF16_TIER_MIB = 90 * 1024          # 96gb 級(エンジンの core/profile.py TIERS: 総 VRAM 90GB 以上)は量子化せず bf16 を読む
PROJ_TE_MAX_MIB = 19073            # 16gb / 8gb 級(総 VRAM 20GB(10 進)未満)は 32B のテキストエンコーダを使わない(4B + 投影)


def hf_hub_cache(env: dict = None) -> Path:
    """Hugging Face のキャッシュ(hub)の場所。huggingface_hub と同じ: HF_HUB_CACHE → HF_HOME/hub → ~/.cache/huggingface/hub(Windows も同じ)。"""
    env = os.environ if env is None else env
    value = str(env.get("HF_HUB_CACHE") or "").strip()
    if value:
        return Path(value)
    home = str(env.get("HF_HOME") or "").strip()
    return (Path(home) if home else Path.home() / ".cache" / "huggingface") / "hub"


def stale_snapshots(env: dict = None) -> list:
    """いま使っていない版の snapshots/<コミット> フォルダ。Hugging Face は版ごとにフォルダを作り、refs/main がいまの版を指す。
    Windows はリンクを使わず実体をコピーするので、配布元が更新されるたびに版ごと重複する(2026-11-26、Windows 10 の実機: 72GB × 2)。
    refs/main が読めなければ [](どれが使われているか分からないので触らない)。"""
    env = os.environ if env is None else env
    root = hf_hub_cache(env) / HF_MODEL_DIR
    try:
        current = (root / "refs" / "main").read_text(encoding="utf-8").strip()
    except OSError:
        return []
    if not current or not (root / "snapshots" / current).is_dir():
        return []
    return sorted(p for p in (root / "snapshots").iterdir() if p.is_dir() and p.name != current)


def te_source_unused(h3_dir, env: dict = None, total_mib: Optional[int] = None) -> bool:
    """動画のエンジンが 32B のテキストエンコーダの bf16 の原本を読まない構成か: 16gb / 8gb 級(4B + 投影を使う)か、
    量子化済み(te_bnb-4bit_prune1)を持っている。それ以外(32gb / 48gb 級で量子化済みがまだ無い、80gb 以上)は原本を読むことがある。"""
    env = os.environ if env is None else env
    if total_mib is not None and total_mib < PROJ_TE_MAX_MIB:
        return True
    return shared_te(h3_dir, env) is not None


def leftover_shards(h3_dir, env: dict = None, plat: str = None, total_mib: Optional[int] = None, te_ready: bool = False) -> dict:
    """消してよい原本 {"transformer": [...], "transformer_ref": [...], "text_encoder": [...]}(完成したキャッシュがある分だけ。無ければ {})。
    transformer / transformer_ref は int8 キャッシュ、text_encoder は画像のエンジンの量子化済み(te_ready)があり、かつ動画のエンジンが
    原本を読まない構成(te_source_unused)のとき。エンジンが bf16 を直接読む構成(file streaming でない、または 96gb 級)では {}。
    いまの版の snapshots だけを見る(使っていない版は stale_snapshots で丸ごと消す)。"""
    env = os.environ if env is None else env
    if not file_streaming(env, plat) or (total_mib is not None and total_mib >= BF16_TIER_MIB):
        return {}
    st = cache_status(h3_dir, env)
    base = hf_hub_cache(env) / HF_MODEL_DIR / "snapshots"
    stale = {p.name for p in stale_snapshots(env)}
    out = {}
    for sub, ok, glob in (("transformer", st["base"], SHARD_GLOB), ("transformer_ref", st["ref"], SHARD_GLOB),
                          ("text_encoder", te_ready and te_source_unused(h3_dir, env, total_mib), TE_SHARD_GLOB)):
        if not ok or not base.is_dir():
            continue
        files = sorted(p for p in base.glob(f"*/{sub}/{glob}") if (p.is_file() or p.is_symlink()) and p.parent.parent.name not in stale)
        if files:
            out[sub] = files
    return out


def sweep_leftovers(h3_dir, env: dict = None, plat: str = None, total_mib: Optional[int] = None, te_ready: bool = False) -> tuple:
    """残った原本(symlink と実体の両方)と、使っていない版のフォルダを消す。(空けた GB, 消せなかったパス)。"""
    import shutil

    freed, failed = 0.0, []
    for snap in stale_snapshots(env):
        size = sum(f.stat().st_size for f in snap.rglob("*") if f.is_file() and not f.is_symlink())    # リンクの先(blob)は数えない
        shutil.rmtree(snap, ignore_errors=True)
        if snap.exists():
            failed.append(snap)
        else:
            freed += size / 1e9
    for files in leftover_shards(h3_dir, env, plat, total_mib, te_ready).values():
        for p in files:
            real = Path(os.path.realpath(p))
            size = real.stat().st_size if real.exists() else 0
            ok = True
            for q in (p, real):
                try:
                    q.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    ok = False
            if ok:
                freed += size / 1e9
            else:
                failed.append(p)
    return freed, failed


# ---- 参照モードにキャッシュが要る構成か ----

def total_ram_gb() -> Optional[float]:
    """搭載 RAM(GB、10 進)。エンジンの ram_gb() と同じ値: /proc/meminfo があればその MemTotal(kB)/ 1e6、無ければ psutil の
    total / 1e9(どちらも小数 1 桁に丸める)。読めなければ None。"""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return round(int(line.split()[1]) / 1e6, 1)
    except (OSError, ValueError, IndexError):
        pass
    try:
        import psutil
        return round(psutil.virtual_memory().total / 1e9, 1)
    except Exception:  # noqa: BLE001
        return None


def file_streaming(env: dict = None, plat: str = None) -> bool:
    """エンジンが重みを量子化キャッシュのファイルから流す構成(file)になるか。エンジンの _group_stream_host_hint() と同じ:
    H3_GROUP_STREAM_HOST の明示(pinned / file)があればそれ。無ければ(auto)Windows は常に file(WDDM ではピン留めの転送が遅いため)、
    それ以外は搭載 RAM が H3_GROUP_STREAM_FILE_BELOW_GB(既定 48、10 進の GB)未満なら file。RAM が分からなければ file としない。"""
    env = os.environ if env is None else env
    host = str(env.get("H3_GROUP_STREAM_HOST") or "auto").strip().lower()
    if host in ("pinned", "file"):
        return host == "file"
    if _is_win(plat):
        return True
    try:
        below = float(str(env.get("H3_GROUP_STREAM_FILE_BELOW_GB") or "").strip() or FILE_STREAM_BELOW_GB)
    except ValueError:
        below = FILE_STREAM_BELOW_GB
    ram = total_ram_gb()
    return ram is not None and ram < below


def group_offload(env: dict = None) -> bool:
    """参照モードを group(ブロックごとに GPU へ流す)の経路で読むか。キャッシュが無いと断るのはこの経路だけ。H3_LOWVRAM を group 以外
    ("0" / "1")に明示していれば使わない。書いていなければ、エンジンが VRAM の級で決める(32GB 以下の級は group)ので、使うとみなす。"""
    env = os.environ if env is None else env
    return str(env.get("H3_LOWVRAM") or "").strip().lower() in ("", "group")


# ---- 作り方 ----

def _venv_python(h3_dir, win: bool) -> str:
    """エンジンの venv の python(エンジンのフォルダからの相対)。components.py と同じく venv → .venv の順で探し、無ければ venv。"""
    for v in ("venv", ".venv"):
        if (Path(h3_dir) / v / ("Scripts/python.exe" if win else "bin/python")).is_file():
            return f".\\{v}\\Scripts\\python.exe" if win else f"{v}/bin/python"
    return ".\\venv\\Scripts\\python.exe" if win else "venv/bin/python"


def _ps_literal(text: str) -> str:
    """PowerShell の文字列 '...'(中の ' は 2 つ重ねる。\\ はそのまま)。"""
    return "'" + text.replace("'", "''") + "'"


def _ps_arg(text: str) -> str:
    """PowerShell の引数。空白や記号があるときだけ '...' で囲む。"""
    return _ps_literal(text) if re.search(r"[\s'\"`$&;(){}@#,|<>]", text) else text


def moved_prequant_dir(h3_dir, env: dict = None) -> Optional[Path]:
    """置き場所を環境変数で変えていれば、その場所(prepare_lowvram.py にも H3_TE_PREQUANT_DIR で渡す。prepare はそれだけを見る)。"""
    target = prequant_dir(h3_dir, env)
    return None if target == Path(h3_dir) / "models" / "prequant" else target


def prepare_command(h3_dir, env: dict = None, plat: str = None, ref: bool = True, keep_source: bool = False) -> str:
    """キャッシュを作る 1 行(Windows は PowerShell、それ以外は bash)。ref で参照モード用も(--ref)、keep_source で bf16 の重みを
    消さずに残す(--keep-source。prepare は既定で、量子化したシャードを HF のキャッシュから消す)。できているキャッシュは prepare が飛ばす。"""
    win = _is_win(plat)
    d = Path(h3_dir)
    moved = moved_prequant_dir(d, env)
    args = (" --ref" if ref else "") + (" --keep-source" if keep_source else "")
    py = _venv_python(d, win)
    if win:
        env_part = f"$env:H3_TE_PREQUANT_DIR={_ps_literal(str(moved))}; " if moved else ""
        return f"cd {_ps_arg(str(d))}; {env_part}{py} scripts\\prepare_lowvram.py{args}"
    env_part = f"H3_TE_PREQUANT_DIR={shlex.quote(str(moved))} " if moved else ""
    return f"cd {shlex.quote(str(d))} && {env_part}{py} scripts/prepare_lowvram.py{args}"


def command_for(cfg: Settings = None, plat: str = None) -> str:
    """設定のエンジン(h3)のフォルダで、参照モード用のキャッシュを作る 1 行。"""
    cfg = cfg or config.settings
    return prepare_command(cfg.component_dir(COMPONENT), engine_env(cfg), plat)


# ---- 管理役が使う口 ----

def has_prepare_script(h3_dir) -> bool:
    """エンジンに scripts/prepare_lowvram.py があるか(2026-11-18 から。参照モードを断るのも同じ日から。それより古いエンジンは断らない)。"""
    return (Path(h3_dir) / "scripts" / "prepare_lowvram.py").is_file()


def ref_cache_state(cfg: Settings = None, plat: str = None) -> dict:
    """参照モード用のキャッシュの様子: {"needed": 要る構成か, "ready": あるか, "dir": エンジンのフォルダ, "cache_dir": キャッシュのフォルダ,
    "command": 作る 1 行}。h3 が無効か、エンジンのフォルダが無ければ needed は False(エンジンが動かないので、そちらの案内が先)。
    prepare_lowvram.py の無い古いエンジンも False(作るコマンドが無く、エンジンもこの理由では断らない。失敗はエンジンの理由で出る)。"""
    cfg = cfg or config.settings
    d = cfg.component_dir(COMPONENT)
    env = engine_env(cfg)
    cache = prequant_dir(d, env) / REF_CACHE
    needed = (COMPONENT in cfg.component_names() and d.is_dir() and has_prepare_script(d) and group_offload(env)
              and file_streaming(env, plat))
    return {"needed": bool(needed), "ready": cache_ready(cache), "dir": str(d), "cache_dir": str(cache),
            "command": prepare_command(d, env, plat)}


def missing_message(command: str):
    """キャッシュが無いときの案内(画面・400・ジョブの失敗に出す。英訳は studio/i18n.py の EN)。"""
    return msg(MISSING_TEMPLATE, command=command)


def ref_cache_error(cfg: Settings = None) -> Optional[str]:
    """参照モードに要るキャッシュが無ければ、その案内。要らない構成か、あれば None。"""
    st = ref_cache_state(cfg)
    return missing_message(st["command"]) if st["needed"] and not st["ready"] else None


def check_ref_cache(cfg: Settings = None) -> None:
    """参照モードを使う仕事を受ける前に確かめる。キャッシュが要るのに無ければ ValueError(案内。API は 400 にする)。"""
    err = ref_cache_error(cfg)
    if err:
        raise ValueError(err)


def is_engine_refusal(detail: str) -> bool:
    """エンジンの失敗の理由が「参照モード用のキャッシュが無い」か(古い制作の再開・やり直しは、ここで案内に置き換える)。"""
    return ENGINE_REFUSAL in str(detail or "")
