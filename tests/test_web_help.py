"""「使い方」の窓(web/help.js)と web/index.html の整合: ボタンと窓の要素がある、スクリプトの読み込みの順が合っている。
node があれば、Markdown の小さな変換(renderMarkdown)を、偽の DOM で help.js を読み込んで確かめる。"""
import re
import shutil
import subprocess
import unittest

from studio.config import REPO_ROOT

WEB = REPO_ROOT / "web"

# 偽の DOM で help.js を読み込み、HELP_LOGIC を取り出す(R = renderMarkdown)
NODE_HARNESS = r"""
const fs = require("fs"), vm = require("vm"), assert = require("assert");
const src = fs.readFileSync(process.argv[process.argv.length - 1], "utf8");
const fake = () => {
  const base = { value: "", dataset: {}, style: {}, hidden: true, textContent: "", classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } } };
  const p = new Proxy(base, { get: (t, k) => (k in t ? t[k] : k === "then" ? undefined : () => p), set: (t, k, v) => { t[k] = v; return true; } });
  return p;
};
const ctx = {
  $: () => fake(), el: () => fake(), document: fake(), window: fake(), localStorage: { getItem() { return null; }, setItem() {} },
  ResizeObserver: class { observe() {} }, fetch() {}, setTimeout, clearTimeout, console,
};
vm.createContext(ctx);
const L = vm.runInContext(src + "\n;HELP_LOGIC", ctx);
const R = L.renderMarkdown;

// 見出し: 4 段まで、id は GitHub 風(小文字・記号を除く・空白は -・日本語はそのまま)、同じ見出しは -1
let h = R("# 使い方\n\n## 画像タブ\n### Step 1: Hello World!\n#### MV(ミュージックビデオ)タブ\n## 画像タブ\n##### 五段は段落");
assert(h.includes('<h1 id="使い方">使い方</h1>'), h);
assert(h.includes('<h2 id="画像タブ">画像タブ</h2>'), h);
assert(h.includes('<h3 id="step-1-hello-world">Step 1: Hello World!</h3>'), h);
assert(h.includes('<h4 id="mvミュージックビデオタブ">'), h);
assert(h.includes('<h2 id="画像タブ-1">'), h);
assert(h.includes("<p>##### 五段は段落</p>"), h);
assert.strictEqual(L.helpSlug("**太字** と `code` と [リンク](http://x)"), "太字-と-code-と-リンク");

// 段落(行は空白 1 つでつなぐ)・太字・斜体・コード・水平線・引用
h = R("一行目\n二行目 **太** と *斜* と `a*b*c`\n\n---\n\n> 引用 1\n> 引用 2");
assert(h.includes("<p>一行目 二行目 <strong>太</strong> と <em>斜</em> と <code>a*b*c</code></p>"), h);
assert(h.includes("<hr>"), h);
assert(h.includes("<blockquote>引用 1 引用 2</blockquote>"), h);

// 箇条書き: 入れ子 1 段、番号つき、続きの行、項目の間の空行
h = R("- 親 1\n  - 子 a\n  - 子 b\n- 親 2\n  続き\n\n* 親 3\n\n1. 一\n2. 二\n   - 入れ子");
assert(h.includes("<ul><li>親 1<ul><li>子 a</li><li>子 b</li></ul></li><li>親 2 続き</li><li>親 3</li></ul>"), h);
assert(h.includes("<ol><li>一</li><li>二<ul><li>入れ子</li></ul></li></ol>"), h);

// 表: 見出し行と |---| の区切り
h = R("| 名前 | 説明 |\n|---|:---:|\n| **a** | `b` |\n| c | d \\| e |\n\n表のあと");
assert(h.includes("<thead><tr><th>名前</th><th>説明</th></tr></thead>"), h);
assert(h.includes("<tbody><tr><td><strong>a</strong></td><td><code>b</code></td></tr><tr><td>c</td><td>d | e</td></tr></tbody>"), h);
assert(h.includes("<p>表のあと</p>"), h);

// コードブロック: 中は記法も HTML も効かせない
h = R("```bash\necho '<b>x</b>' && **y**\n  indent\n```\n後ろ");
assert(h.includes("<pre><code>echo '&lt;b&gt;x&lt;/b&gt;' &amp;&amp; **y**\n  indent</code></pre>"), h);
assert(!h.includes("<b>"), h);

// HTML は先にエスケープする
h = R('<script>alert("x")</script> と <img src=x onerror=1>');
assert(!h.includes("<script>") && !h.includes("<img"), h);
assert(h.includes("&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;"), h);

// 画像: docs/img/ → /docs/img/(説明書からの img/ や ./ も)。ほかはそのまま
h = R("![画面](docs/img/screen-mv.png)\n\n![b](img/a.gif) ![c](../docs/img/c.png) ![d](https://example.com/d.png)");
assert(h.includes('<img src="/docs/img/screen-mv.png" alt="画面" loading="lazy">'), h);
assert(h.includes('src="/docs/img/a.gif"') && h.includes('src="/docs/img/c.png"') && h.includes('src="https://example.com/d.png"'), h);

// リンク: 外部は別タブ、#アンカーは窓の中、危険な形・相対は文字だけ
h = R("[GitHub](https://github.com/a/b?x=1&y=2) と [節へ](#画像タブ) と [悪](javascript:alert(1)) と [別](docs/SPEC.md)");
assert(h.includes('<a href="https://github.com/a/b?x=1&amp;y=2" target="_blank" rel="noopener noreferrer">GitHub</a>'), h);
assert(h.includes('<a href="#画像タブ" data-anchor>節へ</a>'), h);
assert(!h.includes("javascript:alert(1)\"") && !h.includes('href="javascript') && !h.includes('href="docs'), h);
console.log("OK");
"""


class HelpPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (WEB / "index.html").read_text(encoding="utf-8")
        cls.ids = set(re.findall(r'id="([^"]+)"', cls.html))
        cls.js = (WEB / "help.js").read_text(encoding="utf-8")

    def test_the_button_and_the_window_exist(self):
        for i in ("help-btn", "help-win", "help-head", "help-close", "help-gh", "help-toc", "help-toc-sel", "help-content"):
            self.assertIn(i, self.ids)
        topbar = re.search(r'<header class="topbar">.*?</header>', self.html, re.S).group(0)
        self.assertIn('id="help-btn"', topbar)
        self.assertRegex(self.html, r'<div id="help-win"[^>]* hidden>')
        self.assertIn("https://github.com/animede/h3-creator-studio/blob/main/docs/USAGE.md", self.html)

    def test_every_id_used_by_help_js_is_in_the_page(self):
        used = set(re.findall(r'\$\("([\w-]+)"\)', self.js))
        self.assertEqual(sorted(u for u in used if u not in self.ids), [])

    def test_help_js_loads_after_app_js(self):
        order = re.findall(r'<script src="([\w.]+)"></script>', self.html)
        self.assertIn("help.js", order)
        self.assertLess(order.index("app.js"), order.index("help.js"))
        self.assertLess(order.index("gallery.js"), order.index("help.js"))      # studio.tab を読む

    def test_the_tab_headings_cover_every_tab(self):
        tabs = re.findall(r'data-tab="(\w+)" role="tab"', self.html)
        block = re.search(r"HELP_TAB_HEADING = \{(.*?)\};", self.js, re.S).group(1)
        self.assertEqual(sorted(re.findall(r"(\w+): \"", block)), sorted(tabs))

    @unittest.skipUnless(shutil.which("node"), "node が無い")
    def test_help_js_passes_node_check(self):
        r = subprocess.run(["node", "--check", str(WEB / "help.js")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    @unittest.skipUnless(shutil.which("node"), "node が無い")
    def test_render_markdown(self):
        r = subprocess.run(["node", "-e", NODE_HARNESS, str(WEB / "help.js")], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("OK", r.stdout)
