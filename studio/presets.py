"""プリセット: 画像・動画・音楽・MV の各タブの入力欄の値に名前を付けて保存したもの。

同梱のプリセット(リポジトリの presets/*.json。変更できない。複製して編集する)と、自分のプリセット(台帳の presets 表)を
1 つの一覧にして扱う。値は入力欄の値(文字・数・真偽値・それらの配列)だけで、参照画像・曲・フレームなどの生成物は含めない。
入力欄への反映(適用)は画面(web/presets.js)の仕事で、ここは保存・名前・読み込み・書き出しだけを持つ。
"""
import json
import logging
import math
import threading
from pathlib import Path

from .config import REPO_ROOT
from .ledger import Ledger

log = logging.getLogger("studio.presets")

TABS = ("image", "video", "music", "mv")
PRESETS_DIR = REPO_ROOT / "presets"
BUILTIN = "builtin:"                   # 同梱のプリセットの ID の頭(builtin:<tab>:<そのタブでの番号>)
MAX_NAME = 60
MAX_VALUES_BYTES = 32 * 1024
MAX_IMPORT = 200                       # 1 回に読み込める件数
LOCKED = "同梱のプリセットは変更できません。複製してから編集してください"
NOT_FOUND = "そのプリセットは見つかりません(削除されたかもしれません)"

_lock = threading.Lock()               # 名前の重複の確認と登録を、同時に走らせない


class PresetError(Exception):
    """利用者に見せる失敗(日本語の理由)。status は HTTP ステータス。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


# ---- 検証 ----

def check_tab(tab) -> str:
    if tab not in TABS:
        raise PresetError(400, "tab は " + " / ".join(TABS) + " のいずれかです")
    return tab


def check_name(name) -> str:
    """前後の空白を削り、途中の空白は 1 つにして、1〜60 文字か確かめる。"""
    text = " ".join(name.split()) if isinstance(name, str) else ""
    if not 1 <= len(text) <= MAX_NAME:
        raise PresetError(400, f"名前は 1〜{MAX_NAME} 文字で入力してください")
    return text


def _scalar(v) -> bool:
    return v is None or isinstance(v, (str, bool, int)) or (isinstance(v, float) and math.isfinite(v))


def check_values(values) -> dict:
    """入力欄の値の辞書か確かめる。値は文字・数・真偽値・null と、それらだけの配列。JSON にして 32 KB まで。"""
    if not isinstance(values, dict):
        raise PresetError(400, "values は入力欄の値の辞書(JSON のオブジェクト)にしてください")
    for key, val in values.items():
        if not isinstance(key, str) or not key:
            raise PresetError(400, "values の項目名は空でない文字列にしてください")
        if not (_scalar(val) or (isinstance(val, list) and all(_scalar(x) for x in val))):
            raise PresetError(400, f"values に入れられるのは文字・数・真偽値と、それらの配列だけです: {key}")
    if len(json.dumps(values, ensure_ascii=False).encode("utf-8")) > MAX_VALUES_BYTES:
        raise PresetError(400, f"プリセットの値が大きすぎます({MAX_VALUES_BYTES // 1024} KB まで)")
    return values


def unique_name(name: str, taken) -> str:
    """同じ名前があれば、「名前(2)」「名前(3)」…にする(60 文字を超えないよう、元の名前のほうを切る)。"""
    if name not in taken:
        return name
    n = 2
    while True:
        suffix = f"({n})"
        candidate = name[:MAX_NAME - len(suffix)] + suffix
        if candidate not in taken:
            return candidate
        n += 1


def _entries(data) -> list:
    """プリセットの配列か {"presets": [...]} から、項目の配列を取り出す(読み込みと同梱のファイルで同じ形)。"""
    if isinstance(data, dict):
        data = data.get("presets")
    if not isinstance(data, list):
        raise PresetError(400, "プリセットの配列か、{\"presets\": [...]} の形にしてください")
    return data


def _parse(entry, default_tab: str = None) -> tuple:
    """項目 1 つ({tab, name, values})を確かめて (tab, name, values) にする。"""
    if not isinstance(entry, dict):
        raise PresetError(400, "プリセットは {tab, name, values} の形にしてください")
    return check_tab(entry.get("tab", default_tab)), check_name(entry.get("name")), check_values(entry.get("values"))


# ---- 同梱のプリセット ----

def load_builtins(directory: Path = None) -> list:
    """presets/*.json(ファイル名の順)の同梱のプリセット。ファイルは配列か {"presets": [...]}、項目は {tab, name, values}
    (tab は、ファイル名が tab ならそれを既定にできる)。読めないファイルと不正な項目は警告を出して飛ばす(同梱の誤りで管理役を止めない)。"""
    out, count = [], {}
    for path in sorted(Path(directory or PRESETS_DIR).glob("*.json")):
        try:
            entries = _entries(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, PresetError) as e:
            log.warning("同梱のプリセット %s を読めません: %s", path.name, getattr(e, "message", e))
            continue
        for i, entry in enumerate(entries, 1):
            try:
                tab, name, values = _parse(entry, path.stem)
            except PresetError as e:
                log.warning("同梱のプリセット %s の %d 件目を飛ばします: %s", path.name, i, e.message)
                continue
            count[tab] = count.get(tab, 0) + 1
            out.append({"id": f"{BUILTIN}{tab}:{count[tab]}", "tab": tab, "name": name, "values": values, "builtin": True,
                        "created_at": None, "updated_at": None})
    return sorted(out, key=lambda p: TABS.index(p["tab"]))       # タブの順(同じタブの中は、ファイルの順のまま)


# ---- 一覧・作成・変更・削除 ----

def _user(rec: dict) -> dict:
    return {"id": rec["id"], "tab": rec["tab"], "name": rec["name"], "values": rec["values"], "builtin": False,
            "created_at": rec["created_at"], "updated_at": rec["updated_at"]}


def list_presets(ledger: Ledger, tab: str = None) -> list:
    """同梱のプリセット(ファイルの順)、自分のプリセット(作った順)の順。tab があれば、そのタブのものだけ。"""
    if tab:
        check_tab(tab)
    return [p for p in load_builtins() if not tab or p["tab"] == tab] + [_user(r) for r in ledger.list_presets(tab or None)]


def _taken(ledger: Ledger, tab: str) -> set:
    """そのタブで使われている名前(同梱も自分のも)。"""
    return {p["name"] for p in load_builtins() if p["tab"] == tab} | {r["name"] for r in ledger.list_presets(tab)}


def _own(ledger: Ledger, pid: str) -> dict:
    """自分のプリセットの行。同梱のものは変更できない(400)。無ければ 404。"""
    if str(pid).startswith(BUILTIN):
        if any(p["id"] == pid for p in load_builtins()):
            raise PresetError(400, LOCKED)
        raise PresetError(404, NOT_FOUND)
    rec = ledger.get_preset(pid)
    if rec is None:
        raise PresetError(404, NOT_FOUND)
    return rec


def create_preset(ledger: Ledger, tab, name, values) -> dict:
    """自分のプリセットを作る。同じ名前があれば、「名前(2)」「名前(3)」…にする。"""
    tab, name, values = check_tab(tab), check_name(name), check_values(values)
    with _lock:
        return _user(ledger.create_preset(tab, unique_name(name, _taken(ledger, tab)), values))


def update_preset(ledger: Ledger, pid: str, name=None, values=None) -> dict:
    """名前と値のうち、渡したものだけを書き換える。同梱のプリセットは変更できない。
    名前を変えるときは、そのタブのほかのプリセットと同じ名前にはできない(新しく作るときと違い、番号は付けず断る)。"""
    with _lock:
        rec = _own(ledger, pid)
        if name is None and values is None:
            raise PresetError(400, "名前か値のどちらかを指定してください")
        if name is not None:
            name = check_name(name)
            if name != rec["name"] and name in _taken(ledger, rec["tab"]):
                raise PresetError(409, f"「{name}」という名前のプリセットがすでにあります")
        if values is not None:
            values = check_values(values)
        return _user(ledger.update_preset(pid, name, values))


def delete_preset(ledger: Ledger, pid: str) -> None:
    _own(ledger, pid)
    ledger.delete_preset(pid)


# ---- 読み込み・書き出し ----

def import_presets(ledger: Ledger, data) -> list:
    """書き出したものと同じ形(配列か {"presets": [...]})から、自分のプリセットを作って返す。同じ名前は「名前(2)」…にする。
    1 件でも不正なら、何も作らない。"""
    entries = _entries(data)
    if not entries:
        raise PresetError(400, "読み込めるプリセットがありません")
    if len(entries) > MAX_IMPORT:
        raise PresetError(400, f"一度に読み込めるのは {MAX_IMPORT} 件までです")
    parsed = []
    for i, entry in enumerate(entries, 1):
        try:
            parsed.append(_parse(entry))
        except PresetError as e:
            raise PresetError(400, f"{i} 件目: {e.message}")
    created, taken = [], {}
    with _lock:
        for tab, name, values in parsed:
            if tab not in taken:
                taken[tab] = _taken(ledger, tab)
            name = unique_name(name, taken[tab])
            taken[tab].add(name)
            created.append(_user(ledger.create_preset(tab, name, values)))
    return created


def export_presets(ledger: Ledger, tab: str = None, include_builtin: bool = False) -> dict:
    """自分のプリセット(include_builtin なら同梱のものも)を、読み込みにそのまま使える形 {"presets": [{tab, name, values}]} にする。"""
    items = [p for p in list_presets(ledger, tab) if include_builtin or not p["builtin"]]
    return {"presets": [{"tab": p["tab"], "name": p["name"], "values": p["values"]} for p in items]}
