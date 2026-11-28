"""拡大(AI アップスケール)の共通部分(web/upscale.js の UPSCALE)と、生成物ビューアの「拡大」(web/gallery.js・web/index.html)の整合:
ボタンと欄がページにある、gallery.js が使う要素がページにある、使う API が契約どおり(POST /api/gallery/{id}/upscale → GET /api/jobs/{id} で追う)、
upscale.js が gallery.js・edit.js より先に読まれる。node があれば、偽の環境で upscale.js を読み込み、選びの照らし合わせ・道具の読み・導入を確かめる。"""
import re
import shutil
import subprocess
import unittest

from studio.config import REPO_ROOT

WEB = REPO_ROOT / "web"

# 偽の環境で i18n.js と upscale.js を読み、UPSCALE を確かめる(assert が外れたら exit 1)。apiJson は呼ばれた URL・本文を覚えて、answer を返す
NODE_HARNESS = r"""
const fs = require("fs"), vm = require("vm"), assert = require("assert"), path = require("path");
const dir = process.argv[process.argv.length - 1];
const src = fs.readFileSync(path.join(dir, "i18n.js"), "utf8") + "\n" + fs.readFileSync(path.join(dir, "upscale.js"), "utf8");
const J = (x) => (x === undefined ? x : JSON.parse(JSON.stringify(x)));       // 別の realm のオブジェクトを素の形にする
function load(lang, saved) {
  const env = { calls: [], answer: null, confirmed: true, confirms: 0, timers: [] };
  const ctx = {
    localStorage: { getItem: (k) => (k === "yuzu.lang" ? lang : k === "edit-upscale" ? saved : null), setItem(k, v) { env.calls.push(["set", k, v]); } },
    navigator: { language: lang }, console,
    apiJson: async (url, method = "GET", body) => { env.calls.push([method, url, J(body)]); if (env.answer instanceof Error) throw env.answer; return env.answer; },
    confirm: () => { env.confirms++; return env.confirmed; },
    setTimeout: (fn, ms) => { env.timers.push(ms); return env.timers.length; }, clearTimeout() {},
  };
  vm.createContext(ctx);
  env.U = vm.runInContext(src + "\n;UPSCALE", ctx);
  return env;
}

(async () => {
  // 覚えた選び(編集タブと同じ localStorage の edit-upscale)。壊れていれば既定(おすすめ・元のまま)
  let env = load("ja", JSON.stringify({ method: "rtxvsr", scale: 3 }));
  const U = env.U;
  assert.deepStrictEqual(J(U.pref), { method: "rtxvsr", scale: 3 });
  assert.deepStrictEqual(J(load("ja", "{oops").U.pref), { method: "auto", scale: 1 });
  U.pref.scale = 4;
  U.remember();
  assert.deepStrictEqual(env.calls.pop(), ["set", "edit-upscale", JSON.stringify({ method: "rtxvsr", scale: 4 })]);

  // 選びの照らし合わせ: EDIT_LOGIC.upscalePlan と同じ答え(U.plan はいまの U.tools と照らす)。kind が画像でも同じ、曲は拡大しない
  const tools = { methods: [{ id: "rtxvsr", label: "RTX Video Super Resolution", scales: [2, 3, 4], installed: false, usable: false, hint: "入っていません", installable: true },
    { id: "realesrgan", label: "Real-ESRGAN(アニメ向け)", label_en: "Real-ESRGAN (anime)", scales: [2, 4], installed: true, usable: true, hint: null }],
    default: "auto", auto_method: "realesrgan", installing: null, max_edge: 3840 };
  U.tools = tools;
  for (const kind of [undefined, "video", "image"]) {
    const P = (...a) => J(U.plan(...a, kind));
    let up = P("auto", 2, 1024, 576);                                  // auto はおすすめの方式に解決する
    assert.deepStrictEqual([up.method, up.scale, up.scales, up.size, up.tooBig, up.usable, up.hint], ["realesrgan", 2, [2, 4], [2048, 1152], false, true, ""]);
    up = P("auto", 3, 1024, 576);                                      // その方式に無い倍率は、元のまま(1)に戻る
    assert.deepStrictEqual([up.scale, up.size, up.tooBig], [1, [1024, 576], false]);
    up = P("realesrgan", 4, 1344, 768);                                // 長辺 5376 > 3840: 拡大できない
    assert.deepStrictEqual([up.scale, up.size, up.tooBig], [4, [5376, 3072], true]);
    up = P("rtxvsr", 2, 1024, 576);                                    // 入っていない方式: 倍率は選べるが使えない(理由は hint)
    assert.deepStrictEqual([up.method, up.scale, up.usable, up.hint], ["rtxvsr", 2, false, "入っていません"]);
    up = J(U.planFor({ ...tools, auto_method: null }, "auto", 2, 1024, 576, kind));      // 使える方式が無い: 倍率 2〜4 は選べない
    assert.deepStrictEqual([up.method, up.scale, up.scales], [null, 1, []]);
    up = J(U.planFor(null, "auto", 2, 1024, 576, kind));               // 道具の答えが無い(古いサーバ・通信失敗): 拡大しない
    assert.deepStrictEqual([up.method, up.scale, up.scales, up.tooBig], [null, 1, [], false]);
    assert.strictEqual(U.plan("auto", 2, null, null, kind).size, null);     // 寸法が分からなければ size は無し
  }
  let up = J(U.plan("auto", 2, 1024, 576, "image"));
  assert.deepStrictEqual(up.size, [2048, 1152]);                       // 画像も同じ(拡大した PNG の寸法)
  up = J(U.plan("auto", 2, 1024, 576, "audio"));                       // 曲は拡大しない
  assert.deepStrictEqual([up.method, up.scale, up.scales, up.usable], [null, 1, [], false]);
  assert.deepStrictEqual(J(U.planFor(tools, "realesrgan", 2, 512, 512, "image")), J(U.plan("realesrgan", 2, 512, 512, "image")));

  // 方式の名前(画面の言語で。ID でもレコードでも。知らない ID はそのまま)
  assert.strictEqual(U.label("realesrgan"), "Real-ESRGAN(アニメ向け)");
  assert.strictEqual(U.label(tools.methods[0]), "RTX Video Super Resolution");
  assert.strictEqual(U.label("seedvr2"), "seedvr2");
  assert.strictEqual(U.label(null), "");
  const en = load("en", null).U;
  en.tools = tools;
  assert.strictEqual(en.label("realesrgan"), "Real-ESRGAN (anime)");
  assert.strictEqual(en.label("rtxvsr"), "RTX Video Super Resolution");      // label_en が無ければ label

  // 道具の様子を読む: 読んでいる最中に重ねて呼んでも 1 回だけ読み、知らせる。導入の間は 2 秒おきに読み直す
  env = load("ja", null);
  let seen = 0;
  const off = env.U.onChange(() => { seen++; });
  env.answer = { ...tools, installing: "realesrgan", progress_text: "12%" };
  await Promise.all([env.U.load(), env.U.load()]);
  assert.deepStrictEqual(env.calls, [["GET", "/api/upscale/tools", undefined]]);
  assert.strictEqual(seen, 1);
  assert.strictEqual(env.U.tools.installing, "realesrgan");
  assert.deepStrictEqual(env.timers, [2000]);
  env.answer = { detail: "x" };                                         // 形の違う答え: 道具は無し、理由を出す
  env.timers.length = 0;
  await env.U.load();
  assert.strictEqual(env.U.tools, null);
  assert.match(env.U.error, /拡大の方式を調べられませんでした/);
  assert.deepStrictEqual(env.timers, []);                               // 導入中でなければ読み直さない
  off();
  await env.U.load();
  assert.strictEqual(seen, 2);                                          // 外したものには知らせない

  // 導入: RTX Video Super Resolution は NVIDIA の利用条件への同意を先に聞き(断れば送らない)、accept_license: true で送る
  env = load("ja", null);
  env.U.tools = tools;
  env.confirmed = false;
  await env.U.install("rtxvsr");
  assert.deepStrictEqual([env.calls, env.confirms], [[], 1]);
  env.confirmed = true;
  env.answer = { ...tools, installing: "rtxvsr" };
  await env.U.install(tools.methods[0]);
  assert.deepStrictEqual(env.calls.pop(), ["POST", "/api/upscale/tools/install", { method: "rtxvsr", accept_license: true }]);
  assert.strictEqual(env.U.tools.installing, "rtxvsr");
  assert.deepStrictEqual(env.timers.slice(-1), [2000]);
  await env.U.install("realesrgan");                                    // Real-ESRGAN は同意を聞かない
  assert.deepStrictEqual(env.calls.pop(), ["POST", "/api/upscale/tools/install", { method: "realesrgan", accept_license: false }]);
  assert.strictEqual(env.confirms, 2);
  env.answer = new Error("別の導入が走っています");                       // 断られた理由は installErr に残す(次に入れるまで)
  await env.U.install("realesrgan");
  assert.strictEqual(env.U.installErr, "別の導入が走っています");
  console.log("ok");
})().catch((e) => { console.error(e); process.exit(1); });
"""

VIEWER_IDS = ("vup", "vup-panel", "vup-scale", "vup-method", "vup-note", "vup-tools", "vup-go", "vup-stop", "vup-prog")


class ViewerUpscaleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (WEB / "index.html").read_text(encoding="utf-8")
        cls.ids = set(re.findall(r'id="([^"]+)"', cls.html))
        cls.gallery = (WEB / "gallery.js").read_text(encoding="utf-8")
        cls.shared = (WEB / "upscale.js").read_text(encoding="utf-8")
        cls.edit = (WEB / "edit.js").read_text(encoding="utf-8")

    def test_the_button_and_panel_are_in_the_viewer(self):
        for i in VIEWER_IDS:
            self.assertIn(i, self.ids)
        meta = self.html[self.html.index('id="vmeta"'):self.html.index('id="mv-project"')]
        for i in VIEWER_IDS:                        # 生成物ビューアの行(#vmeta)の中: MV・ショート動画・編集タブでは、#vmeta ごと隠れる
            self.assertIn(f'id="{i}"', meta)
        self.assertLess(meta.index('id="vdel"'), meta.index('id="vup"'))       # 「削除」の隣
        self.assertLess(meta.index('id="vup"'), meta.index('id="vacts"'))
        self.assertIn('title="この動画・画像を AI で拡大します(2〜4 倍)"', meta)
        scale_sel = meta[meta.index('id="vup-scale"'):meta.index('id="vup-method"')]
        self.assertEqual(re.findall(r'<option value="(\d)"', scale_sel), ["2", "3", "4"])      # 元のまま(1)は無い
        self.assertIn('<option value="auto"', meta[meta.index('id="vup-method"'):])

    def test_upscale_js_is_loaded_before_gallery_and_edit(self):
        order = re.findall(r'<script src="([\w.]+)"></script>', self.html)
        self.assertLess(max(order.index("i18n.js"), order.index("app.js")), order.index("upscale.js"))
        self.assertLess(order.index("upscale.js"), order.index("gallery.js"))
        self.assertLess(order.index("upscale.js"), order.index("edit.js"))

    def test_every_vup_id_gallery_js_uses_is_in_the_page_and_the_other_way_round(self):
        used = set(re.findall(r'\$\("([\w-]+)"\)', self.gallery))
        self.assertEqual(sorted(u for u in used if u not in self.ids), [])
        self.assertEqual(sorted(i for i in self.ids if i.startswith("vup") and i not in used), [], "使われていない vup の要素")

    def test_it_posts_the_upscale_and_follows_the_job(self):
        """POST /api/gallery/{id}/upscale {method, scale, tab} → 202 {id}。GET /api/jobs/{id} を 1 秒おきに読み、できたら一覧を読み直して、
        始めたタブのビューアで新しい生成物(outputs[0])を開く。中止は POST /api/jobs/{id}/cancel。"""
        g = self.gallery
        self.assertIn("fetch(`/api/gallery/${encodeURIComponent(item.id)}/upscale`, { method: \"POST\"", g)
        self.assertIn("JSON.stringify({ method: c.method, scale: c.scale, tab })", g)
        self.assertIn("fetch(`/api/jobs/${cur.id}`, { headers: langHeaders() })", g)
        self.assertIn("setTimeout(pollUpscale, 1000)", g)
        self.assertIn("fetch(`/api/jobs/${j.id}/cancel`, { method: \"POST\"", g)
        self.assertIn('["completed", "failed", "cancelled"].includes(job.status)', g)
        done = g[g.index("async function finishUpscale"):]
        done = done[:done.index("\n}\n")]
        for step in ("await loadGallery();", "studio.viewing[cur.tab] = job.outputs[0];", "renderHistory();", "renderViewer();", "vup.open = false;"):
            self.assertIn(step, done)
        self.assertLess(done.index("await loadGallery();"), done.index("studio.viewing[cur.tab]"))
        self.assertIn('const UP_KINDS = ["video", "image"];', g)       # 曲は拡大しない
        self.assertIn("renderUpscalePanel(item);", g)                  # 見ている生成物が変わるたびに欄を描き直す

    def test_the_shared_module_owns_the_tools_api_and_the_preference(self):
        self.assertIn('api("/api/upscale/tools")', self.shared)
        self.assertIn('"edit-upscale"', self.shared)
        for src in (self.gallery, self.edit):           # 道具の API は、共通の upscale.js だけが呼ぶ
            self.assertEqual(re.findall(r'["`](/api/upscale[^"`]*)["`]', src), [])
        self.assertIn("const U = UPSCALE", self.gallery)
        self.assertIn("U.plan(sel.value, scale, item.width, item.height, item.kind)", self.gallery)      # 見ている生成物の寸法と種類で照らす
        self.assertIn('U.renderTools($("vup-tools"))', self.gallery)       # 道具の欄は編集タブと同じ描き方
        self.assertIn('"tag ext edtag"', self.gallery)                # 拡大したものの印は、編集タブの書き出しと同じ見た目

    @unittest.skipUnless(shutil.which("node"), "node が無い")
    def test_scripts_parse(self):
        for f in ("upscale.js", "gallery.js", "edit.js"):
            r = subprocess.run(["node", "--check", str(WEB / f)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, f + ": " + r.stderr)

    @unittest.skipUnless(shutil.which("node"), "node が無い")
    def test_shared_logic(self):
        r = subprocess.run(["node", "-e", NODE_HARNESS, str(WEB)], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
