"""サンプル: 同梱の「作り方の見本」(リポジトリの samples/<id>/)。入力欄の値と、参照に使う画像・曲、できあがりの動画を 1 つにまとめたもの。

samples/<id>/sample.json = {id, tab, title, description, values, assets[{role, kind, file, description?, params?}], video, thumb, made, outline, license}。
values はそのタブの入力欄の値(プリセットと同じ形)、assets はその入力に使う画像・曲(role は person / scenery / song)。
読み込み(load)は、assets をギャラリーに 1 回だけ登録する(同じサンプルをもう一度読み込んでも、増やさない)。入力欄への反映は画面(web/samples.js・各タブの js)の仕事で、
ここは一覧と、素材のギャラリーへの登録だけを持つ。
"""
import json
import logging
import re
import shutil
import threading
from pathlib import Path

from . import presets
from .config import REPO_ROOT
from .ledger import Ledger

log = logging.getLogger("studio.samples")

SAMPLES_DIR = REPO_ROOT / "samples"
ROLE_KIND = {"person": "image", "scenery": "image", "song": "audio"}       # 素材の役割 -> 台帳の kind
NOT_FOUND = "そのサンプルは見つかりません"
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

_lock = threading.Lock()               # 素材のギャラリーへの登録(あるかの確認とコピー)を、同時に走らせない


class SampleError(Exception):
    """利用者に見せる失敗(日本語の理由)。status は HTTP ステータス。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


# ---- 読み込み(起動時) ----

def _file(directory: Path, name, what: str) -> str:
    """samples/<id>/ の中のファイル名(フォルダを含まない名前)で、実在するものか確かめる。"""
    if not isinstance(name, str) or not name or Path(name).name != name or name.startswith("."):
        raise ValueError(f"{what} のファイル名が不正です: {name!r}")
    if not (directory / name).is_file():
        raise ValueError(f"{what} のファイルがありません: {name}")
    return name


def _text(v, what: str, required: bool = False) -> str:
    if v is None and not required:
        return ""
    if not isinstance(v, str) or (required and not v.strip()):
        raise ValueError(f"{what} は文字にしてください")
    return v


def _parse(directory: Path, data) -> dict:
    """sample.json 1 つを確かめて、内部の形(dir を持つ)にする。不正なら ValueError。"""
    if not isinstance(data, dict):
        raise ValueError("sample.json は JSON のオブジェクトにしてください")
    sid = data.get("id")
    if not isinstance(sid, str) or not _ID.match(sid) or sid != directory.name:
        raise ValueError(f"id がフォルダ名({directory.name})と合いません: {sid!r}")
    tab = data.get("tab")
    if tab not in presets.TABS:
        raise ValueError(f"tab は {' / '.join(presets.TABS)} のいずれかです: {tab!r}")
    try:
        values = presets.check_values(data.get("values") or {})
    except presets.PresetError as e:
        raise ValueError(f"values: {e.message}")
    assets = []
    for i, a in enumerate(data.get("assets") or [], 1):
        if not isinstance(a, dict) or a.get("role") not in ROLE_KIND:
            raise ValueError(f"assets の {i} 件目: role は {' / '.join(ROLE_KIND)} のいずれかです")
        if a.get("kind") != ROLE_KIND[a["role"]]:
            raise ValueError(f"assets の {i} 件目: role {a['role']} の kind は {ROLE_KIND[a['role']]} です")
        params = a.get("params") or {}
        if not isinstance(params, dict):
            raise ValueError(f"assets の {i} 件目: params は辞書にしてください")
        assets.append({"role": a["role"], "kind": a["kind"], "file": _file(directory, a.get("file"), f"assets の {i} 件目"),
                       "description": _text(a.get("description"), "description"), "params": params})
    made = data.get("made")
    return {"id": sid, "tab": tab, "title": _text(data.get("title"), "title", required=True), "description": _text(data.get("description"), "description"),
            "values": values, "assets": assets, "video": _file(directory, data["video"], "video") if data.get("video") else None,
            "thumb": _file(directory, data["thumb"], "thumb") if data.get("thumb") else None,
            "made": made if isinstance(made, dict) else {}, "license": _text(data.get("license"), "license"), "dir": directory}


def load_samples(directory: Path = None) -> list:
    """samples/*/sample.json(フォルダ名の順)。読めないもの・不正なものは警告を出して飛ばす(同梱の誤りで管理役を止めない)。"""
    out, seen = [], set()
    base = Path(directory or SAMPLES_DIR)
    for path in sorted(base.glob("*/sample.json")):
        try:
            sample = _parse(path.parent, json.loads(path.read_text(encoding="utf-8")))
            if sample["id"] in seen:
                raise ValueError("id が重なっています")
        except (OSError, ValueError) as e:
            log.warning("サンプル %s を飛ばします: %s", path.parent.name, e)
            continue
        seen.add(sample["id"])
        out.append(sample)
    return out


# ---- 画面に返す形 ----

def public(sample: dict) -> dict:
    """画面に返す形。ファイルは /samples/<id>/<ファイル名> で配る(管理役が読み取り専用で公開する)。"""
    base = f"/samples/{sample['id']}/"
    return {"id": sample["id"], "tab": sample["tab"], "title": sample["title"], "description": sample["description"],
            "values": sample["values"],
            "assets": [{**{k: a[k] for k in ("role", "kind", "file", "description", "params")}, "url": base + a["file"]} for a in sample["assets"]],
            "video_url": base + sample["video"] if sample["video"] else None, "thumb_url": base + sample["thumb"] if sample["thumb"] else None,
            "made": sample["made"], "license": sample["license"]}


def list_samples(samples: list, tab: str = None) -> list:
    """tab があれば、そのタブのサンプルだけ。"""
    if tab:
        try:
            presets.check_tab(tab)
        except presets.PresetError as e:
            raise SampleError(e.status, e.message)
    return [public(s) for s in samples if not tab or s["tab"] == tab]


# ---- ギャラリーへの登録 ----

def register_assets(ledger: Ledger, sample: dict) -> list:
    """サンプルの素材(画像・曲)をギャラリーに登録して、[{role, output}] を返す。output は台帳の行(画面に返す形にするのは呼び出し側)。
    すでに登録したもの(params の sample と sample_file が同じ)は、コピーし直さずにそれを使う。ファイルは outputs/samples/<id>/ に置く。"""
    out = []
    with _lock:
        for a in sample["assets"]:
            rec = ledger.find_output_by_params(sample=sample["id"], sample_file=a["file"])
            if rec is None or not ledger.abs_path(rec["path"]).is_file():
                rec = _add(ledger, sample, a)
            out.append({"role": a["role"], "output": rec})
    return out


def _add(ledger: Ledger, sample: dict, a: dict) -> dict:
    src = sample["dir"] / a["file"]
    dest = ledger.abs_path(f"samples/{sample['id']}/{a['file']}")
    part = dest.with_name(dest.name + ".part")       # 途中で止まっても、台帳に載らない名前(起動時に消される)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, part)
        part.replace(dest)
    except OSError as e:
        part.unlink(missing_ok=True)
        raise SampleError(400, f"{a['file']} をギャラリーにコピーできません: {e.strerror or type(e).__name__}")
    try:
        return ledger.add_output(a["kind"], dest, sample["tab"], filename=a["file"], params={**a["params"], "sample": sample["id"], "sample_file": a["file"]})
    except Exception as e:  # noqa: BLE001  壊れた画像・音声など
        dest.unlink(missing_ok=True)
        raise SampleError(400, f"{a['file']} を読み込めません: {type(e).__name__}")
