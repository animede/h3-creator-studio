"""画面の日本語 / English の切り替え(web/i18n.js)。
- i18n.js が最初に読まれる。
- index.html の data-i18n* と、web/*.js の t("…") の鍵が、英語の辞書 I18N_EN にすべてある。
- web/*.js に、t(...) に包まれていない日本語の文字列が残っていない(サーバに送る値などは、行に i18n-skip を付けて許す)。
- node があれば、偽の環境で t() の動き(ja はそのまま、en は英語、{名前} の埋め込み)を確かめる。"""
import json
import re
import shutil
import subprocess
import unittest

from studio.config import REPO_ROOT

WEB = REPO_ROOT / "web"
JP = re.compile(r"[぀-ヿ㐀-鿿！-～]")
NODE = shutil.which("node")

# i18n.js を偽の環境で読み、辞書を JSON で出す(LANG は localStorage の値で決める)
DUMP = r"""
const fs = require("fs"), vm = require("vm");
const ctx = { localStorage: { getItem: () => "en", setItem() {} }, navigator: { language: "en-US" }, console };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[process.argv.length - 1], "utf8") + "\n;globalThis.__D = I18N_EN;", ctx);
process.stdout.write(JSON.stringify(ctx.__D));
"""

BEHAVIOUR = r"""
const fs = require("fs"), vm = require("vm"), assert = require("assert");
const src = fs.readFileSync(process.argv[process.argv.length - 1], "utf8");
function load(lang, nav) {
  const debug = [];
  const ctx = { localStorage: { getItem: () => lang, setItem() {} }, navigator: { language: nav }, console: { debug: (...a) => debug.push(a) } };
  vm.createContext(ctx);
  vm.runInContext(src + "\n;globalThis.__T = { t, LANG, langHeaders, I18N_EN };", ctx);
  return Object.assign(ctx.__T, { debug });
}
const en = load("en", "ja-JP"), ja = load("ja", "en-US");
assert.strictEqual(en.LANG, "en");
assert.strictEqual(ja.LANG, "ja");
assert.strictEqual(load(null, "ja-JP").LANG, "ja");      // 覚えた選択が無ければ、ブラウザの言語
assert.strictEqual(load(null, "en-GB").LANG, "en");
assert.strictEqual(load("xx", "fr").LANG, "en");
assert.strictEqual(en.t("使い方"), "Help");
assert.strictEqual(ja.t("使い方"), "使い方");
// {名前} の埋め込み(ja でも埋める)
en.I18N_EN["テスト {n} / {total}"] = "Test {n} of {total}";
assert.strictEqual(en.t("テスト {n} / {total}", { n: 2, total: 5 }), "Test 2 of 5");
assert.strictEqual(ja.t("テスト {n} / {total}", { n: 2, total: 5 }), "テスト 2 / 5");
assert.strictEqual(ja.t("{x} と {y}", { x: 1 }), "1 と {y}");      // 無い名前はそのまま
// 辞書に無いときは日本語のまま、知らせるのは 1 回だけ
assert.strictEqual(en.t("辞書に無い文"), "辞書に無い文");
en.t("辞書に無い文");
assert.strictEqual(en.debug.length, 1);
assert.strictEqual(en.langHeaders({ "Content-Type": "application/json" })["Accept-Language"], "en");
assert.strictEqual(en.langHeaders({ "Content-Type": "application/json" })["Content-Type"], "application/json");
assert.strictEqual(ja.langHeaders()["Accept-Language"], "ja");
console.log("ok");
"""


def js_tokens(src, base=0):
    """JS を、文字列(' " `)とコメントとそれ以外に大まかに分ける。テンプレートの ${...} の中は式として、入れ子の文字列も拾う
    (テンプレートの文字の部分は、${...} を除いて返す)。正規表現のリテラル(/…/)は、直前の記号で見分けて飛ばす。"""
    out, i, n = [], 0, len(src)
    while i < n:
        if src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
            out.append(("comment", base + i, src[i:j]))
            i = j
        elif src.startswith("/*", i):
            j = src.find("*/", i) + 2
            out.append(("comment", base + i, src[i:j]))
            i = j
        elif src[i] == "/" and re.search(r"(^|[(,=:\[!&|?{};+>]|\breturn)\s*$", src[max(0, i - 12):i]):
            j, cls = i + 1, False
            while j < n and src[j] != "\n" and (cls or src[j] != "/"):
                if src[j] == "\\":
                    j += 1
                elif src[j] == "[":
                    cls = True
                elif src[j] == "]":
                    cls = False
                j += 1
            i = j + 1
        elif src[i] in "'\"`":
            q, j, text = src[i], i + 1, src[i]
            while j < n and src[j] != q:
                if src[j] == "\\":
                    text += src[j:j + 2]
                    j += 2
                    continue
                if q == "`" and src.startswith("${", j):      # 式の中は、括弧の数で終わりを探して、中を同じように分ける
                    depth, k = 1, j + 2
                    while k < n and depth:
                        depth += {"{": 1, "}": -1}.get(src[k], 0)
                        k += 1
                    out.extend(js_tokens(src[j + 2:k - 1], base + j + 2))
                    text += "${}"
                    j = k
                    continue
                text += src[j]
                j += 1
            out.append(("string", base + i, text + q))
            i = j + 1
        else:
            i += 1
    return out


def load_dict():
    r = subprocess.run([NODE, "-e", DUMP, str(WEB / "i18n.js")], capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise AssertionError(r.stderr)
    return json.loads(r.stdout)


T_CALL = re.compile(r"""\bt\(\s*("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|`(?:[^`\\$]|\\.)*`)""")


def unquote(lit):
    if lit[0] == "`":
        return lit[1:-1].replace("\\`", "`").replace("\\\\", "\\")
    return json.loads('"' + lit[1:-1].replace('\\"', '"').replace('"', '\\"') + '"') if lit[0] == "'" else json.loads(lit)


class I18nStaticTest(unittest.TestCase):
    def test_i18n_js_is_loaded_first(self):
        html = (WEB / "index.html").read_text(encoding="utf-8")
        scripts = re.findall(r'<script src="([^"]+)"', html)
        self.assertEqual(scripts[0], "i18n.js", scripts)

    def test_language_switch_is_in_the_topbar(self):
        html = (WEB / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="lang-select"', html)
        self.assertIn('value="en"', html)

    def test_no_japanese_literal_outside_t(self):
        bad = []
        for f in sorted(WEB.glob("*.js")):
            if f.name == "i18n.js":
                continue
            src = f.read_text(encoding="utf-8")
            lines = src.split("\n")
            for kind, pos, text in js_tokens(src):
                if kind != "string" or not JP.search(text):
                    continue
                if re.search(r"\bt\(\s*$", src[max(0, pos - 20):pos]):
                    continue
                line = src.count("\n", 0, pos)
                if "i18n-skip" in lines[line]:
                    continue
                bad.append(f"{f.name}:{line + 1}: {text[:60]}")
        self.assertEqual(bad, [], "t(...) に包むか、行に // i18n-skip を付ける:\n" + "\n".join(bad))


@unittest.skipUnless(NODE, "node が無い")
class I18nDictionaryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = load_dict()

    def test_every_t_literal_has_an_entry(self):
        missing = []
        for f in sorted(WEB.glob("*.js")):
            if f.name == "i18n.js":
                continue
            for m in T_CALL.finditer(f.read_text(encoding="utf-8")):
                key = unquote(m.group(1))
                if key not in self.d:
                    missing.append(f"{f.name}: {key}")
        self.assertEqual(missing, [], "\n".join(missing))

    def test_every_html_attribute_has_an_entry(self):
        html = (WEB / "index.html").read_text(encoding="utf-8")
        missing = []
        # data-i18n(中身)
        for m in re.finditer(r"<(\w+)([^>]*?)\sdata-i18n(?:=\"([^\"]*)\")?(?=[\s>])([^>]*)>(.*?)</\1>", html, re.S):
            key = m.group(3) or re.sub(r"\s+", " ", m.group(5)).strip()
            if key not in self.d:
                missing.append("text: " + key)
        for attr, real in (("title", "title"), ("placeholder", "placeholder"), ("aria", "aria-label")):
            for m in re.finditer(r"<[^>]*\bdata-i18n-" + attr + r"\b[^>]*>", html):
                tag = m.group(0)
                v = re.search(r"\sdata-i18n-" + attr + r'="([^"]+)"', tag) or re.search(r"\s" + real + r'="([^"]*)"', tag)
                key = v.group(1) if v else ""
                if key not in self.d:
                    missing.append(f"{attr}: {key}")
        self.assertEqual(missing, [], "\n".join(missing))

    def test_no_japanese_left_untagged_in_html(self):
        html = (WEB / "index.html").read_text(encoding="utf-8")
        html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
        bad = []
        for m in re.finditer(r">([^<>]+)<", html):      # 文字の部分: 直前のタグに data-i18n が要る
            text = m.group(1).strip()
            if not JP.search(text):
                continue
            start = html.rfind("<", 0, m.start() + 1)
            if "data-i18n" not in html[start:m.start() + 1] and text != "日本語":
                bad.append("text: " + text[:50])
        for attr, mark in (("title", "data-i18n-title"), ("placeholder", "data-i18n-placeholder"), ("aria-label", "data-i18n-aria")):
            for m in re.finditer(r"<[^>]*\s" + attr + r'="([^"]*)"[^>]*>', html):
                if JP.search(m.group(1)) and mark not in m.group(0) and "lang-select" not in m.group(0):
                    bad.append(f"{attr}: {m.group(1)[:50]}")
        self.assertEqual(bad, [], "\n".join(bad))

    def test_placeholders_match(self):
        bad = [k for k, v in self.d.items() if sorted(re.findall(r"\{(\w+)\}", k)) != sorted(re.findall(r"\{(\w+)\}", v))]
        self.assertEqual(bad, [])

    def test_glossary(self):
        for ja, en in {"使い方": "Help", "生成物": "Outputs", "履歴": "History", "ギャラリー": "Gallery", "横長 16:9": "Landscape 16:9",
                       "横 4:5": "Landscape 5:4", "手動入力": "Custom", "低 VRAM": "Low VRAM", "高画質": "High", "結合し直す": "Re-assemble",
                       "元に戻す": "Undo", "やり直す": "Redo"}.items():
            if ja in self.d:
                self.assertEqual(self.d[ja], en, ja)
        self.assertIn("使い方", self.d)

    def test_t_behaviour(self):
        r = subprocess.run([NODE, "-e", BEHAVIOUR, str(WEB / "i18n.js")], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
