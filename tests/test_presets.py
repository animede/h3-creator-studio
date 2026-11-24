import html.parser
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from studio import main, presets
from studio.config import REPO_ROOT
from studio.ledger import Ledger

WEB = REPO_ROOT / "web"


class Form(html.parser.HTMLParser):
    """web/index.html から、select の選択肢(id -> option の値)と、欄の属性(id -> 属性の辞書)を集める。"""

    def __init__(self, text: str):
        super().__init__()
        self.options, self.attrs, self._select = {}, {}, None
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("id"):
            self.attrs[a["id"]] = a
        if tag == "select":
            self._select = a.get("id")
            self.options[self._select] = []
        elif tag == "option" and self._select:
            self.options[self._select].append(a.get("value", ""))

    def handle_endtag(self, tag):
        if tag == "select":
            self._select = None


def write_json(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


class BuiltinFilesTest(unittest.TestCase):
    """リポジトリに同梱の presets/*.json。画面の入力欄の選択肢と合っているかも確かめる(画面を変えたら、ここが気づかせる)。"""

    @classmethod
    def setUpClass(cls):
        cls.html = (WEB / "index.html").read_text(encoding="utf-8")
        cls.form = Form(cls.html)
        cls.music_js = (WEB / "music.js").read_text(encoding="utf-8")
        cls.builtins = presets.load_builtins()

    def modes(self, box: str) -> list:
        """作り方のボタン(<div id="xx-modes"> の data-mode)。"""
        block = re.search(rf'id="{box}".*?</div>', self.html, re.S).group(0)
        return re.findall(r'data-mode="(\w+)"', block)

    def music_list(self, name: str, pairs: bool = False) -> list:
        block = re.search(rf"const {name} = (\[.*?\]);", self.music_js, re.S).group(1)
        return re.findall(r'\["([^"]+)", "', block) if pairs else re.findall(r'"([^"]+)"', block)

    def text_ok(self, value, field: str) -> bool:
        return isinstance(value, str) and len(value) <= int(self.form.attrs[field]["maxlength"])

    def number_ok(self, value, field: str, integer: bool = False) -> bool:
        a = self.form.attrs[field]
        return (isinstance(value, int) if integer else isinstance(value, (int, float))) and not isinstance(value, bool) \
            and float(a["min"]) <= value <= float(a["max"])

    def problems(self, tab: str, v: dict) -> list:
        """値が、そのタブの入力欄に入れられるものか(入れられないものの項目名を返す)。"""
        f, opt = self.form, self.form.options
        seed_ok = v.get("seed") is None or (isinstance(v["seed"], int) and v["seed"] >= 0)
        if tab == "image":
            checks = {
                "mode": v.get("mode") in self.modes("im-modes"), "prompt": self.text_ok(v.get("prompt"), "im-prompt"),
                "width": self.number_ok(v.get("width"), "im-w", True) and v.get("width", 0) % 32 == 0,
                "height": self.number_ok(v.get("height"), "im-h", True) and v.get("height", 0) % 32 == 0,
                "steps": v.get("steps") is None or str(v.get("steps")) in opt["im-steps"], "seed": seed_ok}
        elif tab == "video":
            checks = {
                "mode": v.get("mode") in self.modes("vd-modes"), "prompt": self.text_ok(v.get("prompt"), "vd-prompt"),
                "resolution": v.get("resolution") in opt["vd-res"], "seconds": self.number_ok(v.get("seconds"), "vd-secs", True),
                "match_audio": isinstance(v.get("match_audio"), bool), "vocal_lock": isinstance(v.get("vocal_lock"), bool), "seed": seed_ok}
        elif tab == "music":
            checks = {
                "mode": v.get("mode") in self.modes("mu-modes"), "genre": v.get("genre") in self.music_list("GENRES"),
                "language": v.get("language") in opt["mu-lang"], "duration": str(v.get("duration")) in opt["mu-dur"],
                "instrumental": isinstance(v.get("instrumental"), bool),
                "query": self.text_ok(v.get("query"), "mu-query"), "theme": self.text_ok(v.get("theme"), "mu-theme"),
                "lyrics": self.text_ok(v.get("lyrics"), "mu-lyrics"), "caption": self.text_ok(v.get("caption"), "mu-caption"),
                "caption_auto": isinstance(v.get("caption_auto"), bool),
                "mood": v.get("mood") in ["", *self.music_list("MOODS", pairs=True)],
                "vocal": v.get("vocal") in ["", *self.music_list("VOCALS", pairs=True)],
                "model": isinstance(v.get("model"), str),       # モデルの一覧は ACE が動いているときだけ分かる
                "steps": str(v.get("steps")) in opt["mu-steps"], "batch_size": str(v.get("batch_size")) in opt["mu-batch"],
                "thinking": isinstance(v.get("thinking"), bool), "seed": seed_ok, "negative_prompt": self.text_ok(v.get("negative_prompt"), "mu-neg"),
                "shift": v.get("shift") in opt["mu-shift"], "infer_method": v.get("infer_method") in opt["mu-infer"]}
        else:
            checks = {
                "character": self.text_ok(v.get("character"), "mv-char"), "concept": self.text_ok(v.get("concept"), "mv-concept"),
                "aspect": v.get("aspect") in opt["mv-aspect"], "quality": v.get("quality") in opt["mv-quality"],
                "min_s": self.number_ok(v.get("min_s"), "mv-min"), "max_s": self.number_ok(v.get("max_s"), "mv-max"), "seed": seed_ok}
        # 書かれている項目だけを確かめる(プリセットは、一部の項目だけを持ってよい)
        bad = [k for k in v if k in checks and not checks[k]] + [k for k in v if k not in checks]
        if tab == "mv" and "min_s" in v and "max_s" in v and v["min_s"] > v["max_s"]:
            bad.append("min_s > max_s")
        return bad

    def test_every_tab_ships_3_to_6_presets(self):
        for tab in presets.TABS:
            names = [p["name"] for p in self.builtins if p["tab"] == tab]
            self.assertTrue(3 <= len(names) <= 6, (tab, names))
            self.assertEqual(len(names), len(set(names)), f"{tab}: 名前が重なっています")

    def test_nothing_is_skipped_and_ids_are_unique(self):
        raw = sum(len(presets._entries(json.loads(p.read_text(encoding="utf-8")))) for p in (REPO_ROOT / "presets").glob("*.json"))
        self.assertEqual(len(self.builtins), raw)                                       # 検査で落ちた項目が無い
        ids = [p["id"] for p in self.builtins]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(i.startswith("builtin:") and p["builtin"] for i, p in zip(ids, self.builtins)))
        self.assertEqual([p["tab"] for p in self.builtins], sorted((p["tab"] for p in self.builtins), key=presets.TABS.index))

    def test_values_fit_the_fields_of_each_tab(self):
        for p in self.builtins:
            self.assertEqual(self.problems(p["tab"], p["values"]), [], f"{p['tab']}: {p['name']}")

    def test_the_checks_notice_a_bad_value(self):
        """上の検査が飛ばしていないか: 選択肢に無い値・知らない項目・範囲外は、問題として出る。"""
        self.assertEqual(self.problems("video", {"resolution": "768x448"}), ["resolution"])        # 動画の解像度の選択肢に 768×448 は無い
        self.assertEqual(self.problems("image", {"width": 1000, "steps": 4, "bogus": 1}), ["width", "steps", "bogus"])
        self.assertEqual(self.problems("music", {"genre": "Lo-fi", "duration": 45}), ["genre", "duration"])
        self.assertEqual(self.problems("mv", {"min_s": 9, "max_s": 6}), ["min_s > max_s"])

    def test_each_tab_script_saves_and_applies_every_field(self):
        """プリセットの項目の一覧(この検査の field 一覧)が、各タブの getPresetValues と applyPresetValues の両方に出てくる。"""
        fields = {"image": ("mode", "prompt", "width", "height", "steps", "seed"),
                  "video": ("mode", "prompt", "resolution", "seconds", "match_audio", "vocal_lock", "seed"),
                  "music": ("mode", "genre", "language", "duration", "instrumental", "query", "theme", "lyrics", "caption", "caption_auto", "mood",
                            "vocal", "model", "steps", "batch_size", "thinking", "seed", "negative_prompt", "shift", "infer_method"),
                  "mv": ("character", "concept", "aspect", "quality", "min_s", "max_s", "seed")}
        files = {"image": "image.js", "video": "video.js", "music": "music.js", "mv": "mv.js"}
        for tab, keys in fields.items():
            src = (WEB / files[tab]).read_text(encoding="utf-8")
            for fn in ("getPresetValues", "applyPresetValues"):
                body = re.search(rf"^( *)function {fn}\([^)]*\) \{{\n(.*?)^\1\}}$", src, re.S | re.M).group(2)      # 閉じの } は、function と同じ字下げの行
                for key in keys:
                    self.assertRegex(body, rf"\b{key}\b", f"{files[tab]} の {fn} に {key} がありません")
            self.assertIn(f'wirePresets({{ tab: "{tab}"', src)

    def test_page_has_a_preset_bar_per_tab_and_loads_the_script_first(self):
        for box in ("im-presets", "vd-presets", "mu-presets", "mv-presets"):
            self.assertIn(f'<div class="presetbar" id="{box}"></div>', self.html)
        order = re.findall(r'<script src="([\w.]+)"></script>', self.html)
        self.assertLess(order.index("presets.js"), min(order.index(f) for f in ("image.js", "video.js", "music.js", "mv.js")))
        self.assertLess(max(order.index(f) for f in ("app.js", "gallery.js", "jobs.js")), order.index("presets.js"))      # el・studio・createTracker の後


class BuiltinLoadingTest(unittest.TestCase):
    """presets/*.json の読み方(テスト用のフォルダで)。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_both_shapes_and_the_file_name_as_the_default_tab(self):
        write_json(self.dir / "image.json", {"presets": [{"name": "A", "values": {"prompt": "a"}}, {"tab": "image", "name": "B", "values": {}}]})
        write_json(self.dir / "music.json", [{"name": "M", "values": {"genre": "Rock"}}])
        write_json(self.dir / "extra.json", [{"tab": "mv", "name": "V", "values": {"aspect": "9:16"}}])
        got = presets.load_builtins(self.dir)
        self.assertEqual([(p["id"], p["tab"], p["name"]) for p in got],
                         [("builtin:image:1", "image", "A"), ("builtin:image:2", "image", "B"), ("builtin:music:1", "music", "M"), ("builtin:mv:1", "mv", "V")])
        self.assertTrue(all(p["builtin"] and p["created_at"] is None for p in got))
        self.assertEqual(got[0]["values"], {"prompt": "a"})

    def test_bad_files_and_entries_are_skipped_with_a_warning(self):
        (self.dir / "broken.json").write_text("{not json", encoding="utf-8")
        write_json(self.dir / "shape.json", {"oops": 1})
        write_json(self.dir / "image.json", {"presets": [
            {"name": "ok", "values": {"prompt": "x"}},
            {"name": "", "values": {}},                                        # 名前が空
            {"name": "x" * 61, "values": {}},                                  # 長すぎる
            {"tab": "audio", "name": "tab", "values": {}},                     # 知らないタブ
            {"name": "nested", "values": {"a": {"b": 1}}},                     # 入れ子
            {"name": "notdict", "values": [1, 2]},                             # 値が辞書でない
            "string",                                                          # 項目が辞書でない
            {"name": "ok2", "values": {"seed": None, "list": [1, "a", True]}}]})
        with self.assertLogs("studio.presets", "WARNING") as logs:
            got = presets.load_builtins(self.dir)
        self.assertEqual([p["name"] for p in got], ["ok", "ok2"])
        self.assertEqual([p["id"] for p in got], ["builtin:image:1", "builtin:image:2"])
        self.assertEqual(len(logs.output), 8)                                  # 壊れたファイル 2 つと、飛ばした項目 6 つ

    def test_missing_folder_is_empty(self):
        self.assertEqual(presets.load_builtins(self.dir / "nothing"), [])


class ValidationTest(unittest.TestCase):
    def test_name(self):
        self.assertEqual(presets.check_name("  夜 の   歌 "), "夜 の 歌")             # 前後を削り、途中の空白は 1 つに
        self.assertEqual(presets.check_name("あ" * 60), "あ" * 60)
        for bad in ("", "   ", "あ" * 61, None, 5, ["x"]):
            with self.assertRaises(presets.PresetError, msg=repr(bad)) as cm:
                presets.check_name(bad)
            self.assertEqual((cm.exception.status, cm.exception.message), (400, "名前は 1〜60 文字で入力してください"))

    def test_values(self):
        ok = {"s": "x", "i": 1, "f": 0.5, "b": False, "n": None, "list": [1, "a", None, True], "empty": []}
        self.assertEqual(presets.check_values(ok), ok)
        self.assertEqual(presets.check_values({}), {})
        for bad in ([], "x", None, 3, {"a": {"b": 1}}, {"a": [[1]]}, {"a": [{"b": 1}]}, {"a": float("nan")}, {"a": [float("inf")]}, {"": 1}):
            with self.assertRaises(presets.PresetError, msg=repr(bad)) as cm:
                presets.check_values(bad)
            self.assertEqual(cm.exception.status, 400)

    def test_values_size_limit_is_32kb_of_utf8(self):
        presets.check_values({"t": "a" * (32 * 1024 - 10)})                        # 収まる
        with self.assertRaises(presets.PresetError) as cm:
            presets.check_values({"t": "a" * (32 * 1024)})
        self.assertIn("32 KB まで", cm.exception.message)
        with self.assertRaises(presets.PresetError):                                # 文字数ではなく、UTF-8 のバイト数で数える
            presets.check_values({"t": "あ" * 11000})

    def test_tab(self):
        for tab in presets.TABS:
            self.assertEqual(presets.check_tab(tab), tab)
        for bad in ("", None, "audio", "Image"):
            with self.assertRaises(presets.PresetError):
                presets.check_tab(bad)

    def test_unique_name(self):
        self.assertEqual(presets.unique_name("A", set()), "A")
        self.assertEqual(presets.unique_name("A", {"A"}), "A(2)")
        self.assertEqual(presets.unique_name("A", {"A", "A(2)", "A(3)"}), "A(4)")
        self.assertEqual(presets.unique_name("A", {"A", "A(3)"}), "A(2)")           # 空いている番号を使う
        long = "あ" * 60
        got = presets.unique_name(long, {long})
        self.assertEqual((len(got), got.endswith("(2)")), (60, True))               # 60 文字を超えない


class PresetsApiTest(unittest.TestCase):
    """プリセットの API。lifespan は使わず、台帳だけを差し替える。同梱のプリセットは、テスト用の小さなフォルダにする。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.ledger = Ledger(root / "outputs")
        self.addCleanup(self.ledger.close)
        missing = object()
        saved = getattr(main.app.state, "ledger", missing)
        self.addCleanup(lambda: delattr(main.app.state, "ledger") if saved is missing else setattr(main.app.state, "ledger", saved))
        main.app.state.ledger = self.ledger
        write_json(root / "presets" / "image.json", {"presets": [{"name": "同梱の画像 1", "values": {"prompt": "style"}},
                                                                 {"name": "同梱の画像 2", "values": {"width": 768, "height": 1344}}]})
        write_json(root / "presets" / "music.json", [{"name": "同梱の音楽", "values": {"genre": "Rock"}}])
        patcher = mock.patch.object(presets, "PRESETS_DIR", root / "presets")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(main.app)

    def create(self, name="自分の画像", tab="image", values=None, **kw):
        r = self.client.post("/api/presets", json={"tab": tab, "name": name, "values": {"prompt": "p"} if values is None else values, **kw})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def names(self, tab=None) -> list:
        return [p["name"] for p in self.client.get("/api/presets", params={"tab": tab} if tab else None).json()]

    # ---- 一覧 ----

    def test_list_has_builtins_first_then_mine_with_the_builtin_flag(self):
        mine = self.create("自分の画像", "image")
        music = self.create("自分の音楽", "music", {"genre": "Jazz"})
        self.create("自分の画像 B", "image")
        items = self.client.get("/api/presets").json()
        self.assertEqual([p["name"] for p in items], ["同梱の画像 1", "同梱の画像 2", "同梱の音楽", "自分の画像", "自分の音楽", "自分の画像 B"])   # 同梱が先、自分のは作った順
        self.assertEqual([p["builtin"] for p in items], [True, True, True, False, False, False])
        self.assertEqual([p["id"] for p in items[:3]], ["builtin:image:1", "builtin:image:2", "builtin:music:1"])
        self.assertEqual((items[0]["created_at"], items[0]["values"]), (None, {"prompt": "style"}))
        self.assertEqual((items[3]["id"], items[4]["id"]), (mine["id"], music["id"]))
        self.assertEqual(set(items[3]), {"id", "tab", "name", "values", "builtin", "created_at", "updated_at"})

    def test_list_filters_by_tab(self):
        self.create("自分の画像", "image")
        self.create("自分の音楽", "music", {"genre": "Jazz"})
        self.assertEqual(self.names("image"), ["同梱の画像 1", "同梱の画像 2", "自分の画像"])
        self.assertEqual(self.names("music"), ["同梱の音楽", "自分の音楽"])
        self.assertEqual(self.names("video"), [])
        self.assertEqual(self.names("mv"), [])
        r = self.client.get("/api/presets", params={"tab": "audio"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("tab は image / video / music / mv のいずれかです", r.json()["detail"])

    def test_the_real_files_are_served_too(self):
        """同梱のフォルダを差し替えなければ、リポジトリの presets/*.json がそのまま一覧に出る。"""
        with mock.patch.object(presets, "PRESETS_DIR", REPO_ROOT / "presets"):
            for tab in presets.TABS:
                got = self.client.get("/api/presets", params={"tab": tab}).json()
                self.assertTrue(got and all(p["builtin"] and p["tab"] == tab for p in got), tab)

    # ---- 作る・変える・消す ----

    def test_create_returns_the_preset_and_it_survives_a_reload(self):
        js = self.create("夜の画像", "image", {"prompt": "night", "width": 1024, "steps": None, "seed": 7, "tags": ["a", "b"]})
        self.assertEqual((js["tab"], js["name"], js["builtin"]), ("image", "夜の画像", False))
        self.assertEqual(js["values"], {"prompt": "night", "width": 1024, "steps": None, "seed": 7, "tags": ["a", "b"]})
        self.assertTrue(js["id"] and not js["id"].startswith("builtin:"))
        self.assertEqual(js["created_at"], js["updated_at"])
        self.assertEqual(self.ledger.get_preset(js["id"])["values"], js["values"])             # 台帳に入っている
        self.assertEqual(self.names("image")[-1], "夜の画像")

    def test_create_trims_the_name(self):
        self.assertEqual(self.create("  余分な   空白  ")["name"], "余分な 空白")

    def test_update_name_and_values_separately(self):
        js = self.create("A", "video", {"seconds": 5, "prompt": "p"})
        r = self.client.put(f"/api/presets/{js['id']}", json={"values": {"seconds": 10}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["name"], r.json()["values"], r.json()["tab"]), ("A", {"seconds": 10}, "video"))        # 値は全体を置き換える
        self.assertGreaterEqual(r.json()["updated_at"], js["updated_at"])
        r = self.client.put(f"/api/presets/{js['id']}", json={"name": "B"})
        self.assertEqual((r.status_code, r.json()["name"], r.json()["values"]), (200, "B", {"seconds": 10}))                # 名前だけなら値はそのまま
        r = self.client.put(f"/api/presets/{js['id']}", json={"name": " C ", "values": {}})
        self.assertEqual((r.json()["name"], r.json()["values"]), ("C", {}))
        self.assertEqual(self.names("video"), ["C"])

    def test_update_needs_something_and_validates_it(self):
        js = self.create()
        r = self.client.put(f"/api/presets/{js['id']}", json={})
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "名前か値のどちらかを指定してください"))
        for body, word in (({"name": ""}, "名前は 1〜60 文字"), ({"name": "x" * 61}, "名前は 1〜60 文字"), ({"values": [1]}, "values は"),
                           ({"values": {"a": {"b": 1}}}, "values に入れられるのは")):
            r = self.client.put(f"/api/presets/{js['id']}", json=body)
            self.assertEqual(r.status_code, 400, body)
            self.assertIn(word, r.json()["detail"])
        self.assertEqual(self.ledger.get_preset(js["id"])["name"], "自分の画像")              # 断ったら変えない

    def test_delete(self):
        js = self.create()
        r = self.client.delete(f"/api/presets/{js['id']}")
        self.assertEqual((r.status_code, r.json()), (200, {"ok": True}))
        self.assertEqual(self.names("image"), ["同梱の画像 1", "同梱の画像 2"])
        r = self.client.delete(f"/api/presets/{js['id']}")
        self.assertEqual((r.status_code, r.json()["detail"]), (404, "そのプリセットは見つかりません(削除されたかもしれません)"))

    def test_unknown_ids_are_404(self):
        for path in ("/api/presets/nope", "/api/presets/builtin:image:99"):
            self.assertEqual(self.client.put(path, json={"name": "x"}).status_code, 404, path)
            self.assertEqual(self.client.delete(path).status_code, 404, path)

    def test_builtins_are_read_only(self):
        want = "同梱のプリセットは変更できません。複製してから編集してください"
        for method, kw in (("put", {"json": {"name": "改名"}}), ("put", {"json": {"values": {"prompt": "x"}}}), ("put", {"json": {}}), ("delete", {})):
            r = getattr(self.client, method)("/api/presets/builtin:image:1", **kw)
            self.assertEqual((r.status_code, r.json()["detail"]), (400, want), (method, kw))
        self.assertEqual(self.names("image"), ["同梱の画像 1", "同梱の画像 2"])                 # 何も変わっていない
        self.assertEqual(self.client.get("/api/presets", params={"tab": "image"}).json()[0]["values"], {"prompt": "style"})

    def test_a_builtin_can_be_duplicated_then_edited(self):
        src = self.client.get("/api/presets", params={"tab": "image"}).json()[0]
        copy = self.create("同梱の画像 1 のコピー", "image", src["values"])
        r = self.client.put(f"/api/presets/{copy['id']}", json={"values": {"prompt": "edited"}})
        self.assertEqual((r.status_code, r.json()["values"]), (200, {"prompt": "edited"}))

    # ---- 名前の重なり ----

    def test_same_names_get_a_number_on_create(self):
        self.assertEqual([self.create("A")["name"] for _ in range(3)], ["A", "A(2)", "A(3)"])
        self.assertEqual(self.create("A", "music")["name"], "A")                           # 別のタブは別の名前の置き場
        self.assertEqual(self.create("同梱の画像 1")["name"], "同梱の画像 1(2)")                # 同梱の名前とも重ならない
        self.assertEqual(self.create("同梱の音楽", "image")["name"], "同梱の音楽")             # 同梱の別タブの名前とは重ならない

    def test_a_long_name_stays_within_60_chars_when_numbered(self):
        long = "長" * 60
        self.assertEqual(self.create(long)["name"], long)
        second = self.create(long)["name"]
        self.assertEqual((len(second), second.endswith("(2)")), (60, True))

    def test_renaming_to_a_taken_name_is_refused_but_keeping_your_own_name_is_fine(self):
        a, b = self.create("A"), self.create("B")
        r = self.client.put(f"/api/presets/{b['id']}", json={"name": "A"})
        self.assertEqual((r.status_code, r.json()["detail"]), (409, "「A」という名前のプリセットがすでにあります"))
        r = self.client.put(f"/api/presets/{b['id']}", json={"name": "同梱の画像 2"})          # 同梱の名前も
        self.assertEqual(r.status_code, 409)
        r = self.client.put(f"/api/presets/{b['id']}", json={"name": "B", "values": {"prompt": "again"}})
        self.assertEqual((r.status_code, r.json()["name"]), (200, "B"))
        r = self.client.put(f"/api/presets/{a['id']}", json={"name": "B"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.names("image")[-2:], ["A", "B"])

    # ---- 入力の検査 ----

    def test_create_validation_errors_are_japanese_400s(self):
        cases = [({"tab": "image", "name": "", "values": {}}, "名前は 1〜60 文字"), ({"tab": "image", "name": "   ", "values": {}}, "名前は 1〜60 文字"),
                 ({"tab": "image", "name": "x" * 61, "values": {}}, "名前は 1〜60 文字"), ({"tab": "image", "values": {}}, "名前は 1〜60 文字"),
                 ({"tab": "audio", "name": "x", "values": {}}, "tab は image / video / music / mv のいずれかです"),
                 ({"name": "x", "values": {}}, "tab は"), ({"tab": "image", "name": "x"}, "values は"), ({"tab": "image", "name": "x", "values": [1]}, "values は"),
                 ({"tab": "image", "name": "x", "values": "text"}, "values は"),
                 ({"tab": "image", "name": "x", "values": {"a": {"b": 1}}}, "文字・数・真偽値と、それらの配列だけ"),
                 ({"tab": "image", "name": "x", "values": {"a": [[1]]}}, "文字・数・真偽値と、それらの配列だけ"),
                 ({"tab": "image", "name": "x", "values": {"t": "a" * (33 * 1024)}}, "32 KB まで")]
        for body, word in cases:
            r = self.client.post("/api/presets", json=body)
            self.assertEqual(r.status_code, 400, body if len(str(body)) < 200 else "big")
            self.assertIn(word, r.json()["detail"])
        self.assertEqual(self.ledger.list_presets(), [])                                   # 断ったら何も作らない

    def test_nan_in_values_is_refused(self):
        r = self.client.post("/api/presets", content='{"tab": "image", "name": "x", "values": {"a": NaN}}', headers={"content-type": "application/json"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.ledger.list_presets(), [])

    # ---- 書き出し・読み込み ----

    def test_export_has_my_presets_only_in_the_import_shape(self):
        self.create("画像 A", "image", {"prompt": "a"})
        self.create("音楽 A", "music", {"genre": "Jazz", "instrumental": True})
        r = self.client.get("/api/presets/export")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "application/json")
        self.assertEqual(r.headers["content-disposition"], 'attachment; filename="h3-presets.json"')
        self.assertEqual(r.json(), {"presets": [{"tab": "image", "name": "画像 A", "values": {"prompt": "a"}},
                                                {"tab": "music", "name": "音楽 A", "values": {"genre": "Jazz", "instrumental": True}}]})
        self.assertIn("画像 A", r.text)                                                    # 日本語はそのまま(\u エスケープにしない)
        r = self.client.get("/api/presets/export", params={"tab": "music"})
        self.assertEqual(r.headers["content-disposition"], 'attachment; filename="h3-presets-music.json"')
        self.assertEqual([p["name"] for p in r.json()["presets"]], ["音楽 A"])
        r = self.client.get("/api/presets/export", params={"tab": "video"})                # 無ければ空の一覧
        self.assertEqual(r.json(), {"presets": []})
        self.assertEqual(self.client.get("/api/presets/export", params={"tab": "audio"}).status_code, 400)

    def test_export_can_include_builtins(self):
        self.create("画像 A", "image")
        got = self.client.get("/api/presets/export", params={"include_builtin": 1}).json()["presets"]
        self.assertEqual([p["name"] for p in got], ["同梱の画像 1", "同梱の画像 2", "同梱の音楽", "画像 A"])
        self.assertTrue(all(set(p) == {"tab", "name", "values"} for p in got))
        got = self.client.get("/api/presets/export", params={"include_builtin": "true", "tab": "image"}).json()["presets"]
        self.assertEqual([p["name"] for p in got], ["同梱の画像 1", "同梱の画像 2", "画像 A"])

    def test_export_then_import_round_trip(self):
        self.create("画像 A", "image", {"prompt": "a", "width": 768, "seed": None, "tags": ["x", 1]})
        self.create("動画 A", "video", {"mode": "ref2va", "vocal_lock": True, "seconds": 7})
        exported = self.client.get("/api/presets/export")
        for p in self.ledger.list_presets():
            self.client.delete(f"/api/presets/{p['id']}")
        self.assertEqual(self.ledger.list_presets(), [])
        r = self.client.post("/api/presets/import", content=exported.content, headers={"content-type": "application/json"})
        self.assertEqual(r.status_code, 200, r.text)
        created = r.json()
        self.assertEqual([(p["tab"], p["name"], p["builtin"]) for p in created], [("image", "画像 A", False), ("video", "動画 A", False)])
        self.assertEqual(self.client.get("/api/presets/export").json(), exported.json())      # 書き出し直すと同じ
        self.assertEqual(self.ledger.get_preset(created[0]["id"])["values"], {"prompt": "a", "width": 768, "seed": None, "tags": ["x", 1]})

    def test_import_accepts_a_bare_list_and_numbers_duplicates(self):
        self.create("X", "image")
        body = [{"tab": "image", "name": "X", "values": {"prompt": "1"}}, {"tab": "image", "name": "X", "values": {"prompt": "2"}},
                {"tab": "music", "name": "X", "values": {}}, {"tab": "image", "name": "同梱の画像 1", "values": {}}]
        r = self.client.post("/api/presets/import", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([(p["tab"], p["name"]) for p in r.json()], [("image", "X(2)"), ("image", "X(3)"), ("music", "X"), ("image", "同梱の画像 1(2)")])
        again = self.client.post("/api/presets/import", json={"presets": body[:1]})
        self.assertEqual(again.json()[0]["name"], "X(4)")

    def test_a_built_in_exported_with_include_builtin_comes_back_as_a_copy(self):
        exported = self.client.get("/api/presets/export", params={"include_builtin": 1, "tab": "image"}).json()
        created = self.client.post("/api/presets/import", json=exported).json()
        self.assertEqual([p["name"] for p in created], ["同梱の画像 1(2)", "同梱の画像 2(2)"])
        self.assertTrue(all(not p["builtin"] for p in created))                            # 取り込んだものは自分のプリセット(編集できる)

    def test_import_is_all_or_nothing(self):
        good = {"tab": "image", "name": "ok", "values": {}}
        for bad, word in (({"tab": "image", "name": "", "values": {}}, "2 件目: 名前は 1〜60 文字"), ({"tab": "audio", "name": "x", "values": {}}, "2 件目: tab は"),
                          ({"tab": "image", "name": "x", "values": {"a": {}}}, "2 件目: values に入れられるのは"), ("text", "2 件目: プリセットは {tab, name, values} の形")):
            r = self.client.post("/api/presets/import", json=[good, bad, good])
            self.assertEqual(r.status_code, 400, bad)
            self.assertIn(word, r.json()["detail"])
            self.assertEqual(self.ledger.list_presets(), [], "1 件でも不正なら何も作らない")

    def test_import_rejects_bad_bodies(self):
        r = self.client.post("/api/presets/import", content="{not json", headers={"content-type": "application/json"})
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "JSON として読めません"))
        for body in ('{"foo": 1}', '"text"', "3", "null", '{"presets": "x"}'):
            r = self.client.post("/api/presets/import", content=body, headers={"content-type": "application/json"})
            self.assertEqual((r.status_code, "プリセットの配列か" in r.json()["detail"]), (400, True), body)
        for body in ([], {"presets": []}):
            r = self.client.post("/api/presets/import", json=body)
            self.assertEqual((r.status_code, r.json()["detail"]), (400, "読み込めるプリセットがありません"))
        r = self.client.post("/api/presets/import", json=[{"tab": "image", "name": f"p{i}", "values": {}} for i in range(presets.MAX_IMPORT + 1)])
        self.assertEqual(r.status_code, 400)
        self.assertIn(f"{presets.MAX_IMPORT} 件まで", r.json()["detail"])
        self.assertEqual(self.ledger.list_presets(), [])
        r = self.client.post("/api/presets/import", json=[{"tab": "image", "name": f"p{i}", "values": {}} for i in range(presets.MAX_IMPORT)])
        self.assertEqual((r.status_code, len(r.json())), (200, presets.MAX_IMPORT))        # 上限ちょうどは読み込める


class WebScriptsTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node が PATH に無い")
    def test_every_script_passes_node_check(self):
        scripts = sorted(WEB.glob("*.js"))
        self.assertIn("presets.js", [p.name for p in scripts])
        for path in scripts:
            r = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, f"{path.name}: {r.stderr}")


if __name__ == "__main__":
    unittest.main()
