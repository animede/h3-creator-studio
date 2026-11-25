"""編集タブの画面(web/edit.js)と web/index.html の整合: タブと入れ物がある、スクリプトが使う要素がページにある、使う API が契約どおり。
node があれば、state を組み立てる純粋な関数(分ける・区間の削除・挿入・時間軸の変換)を、偽の DOM で edit.js を読み込んで確かめる。"""
import re
import shutil
import subprocess
import unittest

from studio.config import REPO_ROOT

WEB = REPO_ROOT / "web"

# 偽の DOM(何を呼んでも落ちない要素)で edit.js を読み込み、EDIT_LOGIC を取り出して検査する(assert が外れたら exit 1)
NODE_HARNESS = r"""
const fs = require("fs"), vm = require("vm"), assert = require("assert");
const src = fs.readFileSync(process.argv[process.argv.length - 1], "utf8");
const fake = () => {
  const base = { value: "mono", dataset: {}, style: {}, hidden: false, textContent: "", classList: { toggle() {}, add() {}, remove() {} } };
  const p = new Proxy(base, { get: (t, k) => (k in t ? t[k] : k === "then" ? undefined : () => p), set: (t, k, v) => { t[k] = v; return true; } });
  return p;
};
const ctx = {
  $: () => fake(), el: () => fake(), wireDrop() {}, uploadFiles() {}, byId() {}, fmtSec: String, fmtDur: String, makeDraggable() {}, apiJson() {},
  pickable() {}, renderSelectBar() {}, loadGallery() {}, document: fake(), prompt() {}, confirm() {}, setTimeout, clearTimeout, console,
  localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
  studio: { tab: "image", items: [], own: {}, on: { tab: [], items: [] }, select: { on: false, ids: new Set() } },
};
vm.createContext(ctx);
const L0 = vm.runInContext(src + "\n;EDIT_LOGIC", ctx);
// 別の realm のオブジェクトは deepStrictEqual で比べられないので、答えを JSON で素の形にする
const L = Object.fromEntries(Object.entries(L0).map(([k, fn]) => [k, (...a) => { const r = fn(...a); return r === undefined ? r : JSON.parse(JSON.stringify(r)); }]));
assert(ctx.studio.own.edit && typeof ctx.studio.own.edit.history === "function", "studio.own.edit が登録される");
assert.strictEqual(ctx.studio.on.tab.length, 1);

const V = (id, i, o) => ({ id, kind: "video", in: i, out: o });
const base = () => ({
  clips: [V("a", 0, 10), V("b", 0, 10)],
  transitions: [{ after: 0, type: "crossfade", duration: 1 }],
  fade_in: 0, fade_out: 0, effects: [], audio: { mode: "original", song_id: null, song_db: -14 },
});
const ranges = (s) => s.clips.map((c) => [c.id, c.in, c.out]);

// 分ける: 同じ生成物の in/out だけ変えた 2 つ。後ろの境目の効果は 1 つずれ、分けたところは cut
let sp = L.splitAt(base(), 4);
assert.deepStrictEqual(ranges(sp.state), [["a", 0, 4], ["a", 4, 10], ["b", 0, 10]]);
assert.deepStrictEqual(sp.state.transitions, [{ after: 1, type: "crossfade", duration: 1 }]);
assert.strictEqual(sp.at, 1);       // t より前のクリップが 1 つ
sp = L.splitAt(base(), 10);        // クリップの境目では分けない
assert.strictEqual(sp.state.clips.length, 2);
assert.strictEqual(sp.at, 1);
const imgState = { ...base(), clips: [{ id: "i", kind: "image", in: 0, out: 5, duration: 5 }], transitions: [] };
const isp = L.splitAt(imgState, 2).state.clips;
assert.deepStrictEqual(isp.map((c) => [c.in, c.out, c.duration]), [[0, 2, 2], [0, 3, 3]]);

// 区間の削除: 1 つのクリップの中(2 つに分かれる)、境目をまたぐ、全体
let d = L.deleteRange(base(), 12, 15);
assert.deepStrictEqual(ranges(d), [["a", 0, 10], ["b", 0, 2], ["b", 5, 10]]);
assert.deepStrictEqual(d.transitions, [{ after: 0, type: "crossfade", duration: 1 }]);
assert.strictEqual(L.total(d.clips), 17);
d = L.deleteRange(base(), 8, 12);
assert.deepStrictEqual(ranges(d), [["a", 0, 8], ["b", 2, 10]]);
assert.deepStrictEqual(d.transitions, []);
d = L.deleteRange(base(), 0, 10);       // 先頭のクリップを丸ごと
assert.deepStrictEqual(ranges(d), [["b", 0, 10]]);
assert.deepStrictEqual(d.transitions, []);
d = L.deleteRange(base(), 0, 25);       // 終了が全体を超えても、全体まで
assert.strictEqual(d.clips.length, 0);
assert.throws(() => L.deleteRange(base(), 5, 5), /終了/);
// 3 つ並び、後ろの境目の効果は、前のクリップが丸ごと消えても残って付け替わる
const three = { ...base(), clips: [V("a", 0, 10), V("b", 0, 10), V("c", 0, 10)], transitions: [{ after: 0, type: "crossfade", duration: 1 }, { after: 1, type: "fadeblack", duration: 2 }] };
d = L.deleteRange(three, 0, 10);
assert.deepStrictEqual(d.transitions, [{ after: 0, type: "fadeblack", duration: 2 }]);
// 効果の区間は削った分だけ詰める。区間の中は消え、またぐものは縮む
const fx = (s, e) => ({ type: "mono", value: 1, start: s, end: e });
d = L.deleteRange({ ...base(), effects: [fx(18, 20), fx(13, 14), fx(10, 14), fx(null, null), fx(2, 3)] }, 12, 15);
assert.deepStrictEqual(d.effects.map((e) => [e.start, e.end]), [[15, 17], [10, 12], [null, null], [2, 3]]);

// 挿入: 分けた間に入れる。境目の効果は後ろへずれ、効果の区間は挿入した分だけ後ろへ
const img = L.newClip({ id: "i", kind: "image" }, 3);
assert.deepStrictEqual(img, { id: "i", kind: "image", in: 0, out: 3, duration: 3 });
let ins = L.insertAt({ ...base(), effects: [fx(8, 9), fx(4, 6), fx(1, 2)] }, 5, img);
assert.deepStrictEqual(ins.clips.map((c) => [c.id, c.in, c.out]), [["a", 0, 5], ["i", 0, 3], ["a", 5, 10], ["b", 0, 10]]);
assert.deepStrictEqual(ins.transitions, [{ after: 2, type: "crossfade", duration: 1 }]);
assert.deepStrictEqual(ins.effects.map((e) => [e.start, e.end]), [[11, 12], [4, 9], [1, 2]]);
ins = L.insertAt(base(), 10, img);        // 境目のところ: そこの効果は消える(cut)
assert.deepStrictEqual(ins.clips.map((c) => c.id), ["a", "i", "b"]);
assert.deepStrictEqual(ins.transitions, []);
ins = L.insertAt(base(), 0, img);
assert.deepStrictEqual(ins.clips.map((c) => c.id), ["i", "a", "b"]);
assert.deepStrictEqual(ins.transitions, [{ after: 1, type: "crossfade", duration: 1 }]);
ins = L.insertAt(base(), 999, img);       // 全体より後ろは、末尾
assert.deepStrictEqual(ins.clips.map((c) => c.id), ["a", "b", "i"]);
assert.strictEqual(L.newClip({ id: "v", kind: "video", length_s: 5.0006 }, 3).out, 5);

// クリップの in / out、並べ替え、境目の効果、整え
assert.throws(() => L.setClip(base(), 0, { in: 5, out: 5 }, 10), /out/);
assert.throws(() => L.setClip(base(), 0, { out: 11 }, 10), /元の長さ/);
assert.deepStrictEqual(ranges(L.setClip(base(), 0, { in: 2, out: 6 }, 10)), [["a", 2, 6], ["b", 0, 10]]);
assert.deepStrictEqual(L.moveClip(base(), 0, 1).clips.map((c) => c.id), ["b", "a"]);
assert.strictEqual(L.moveClip(base(), 0, -1).clips[0].id, "a");
assert.deepStrictEqual(L.setTransition(base(), 0, "fadeblack", 9).transitions, [{ after: 0, type: "fadeblack", duration: 5 }]);
assert.deepStrictEqual(L.setTransition(base(), 0, "cut", 1).transitions, []);
assert.throws(() => L.setTransition({ ...base(), clips: [V("a", 0, 0.8), V("b", 0, 10)] }, 0, "crossfade", 1), /短くて/);
let fixed = L.fixup({ ...base(), clips: [V("a", 0, 10), V("b", 0, 1.4)], effects: [fx(18, 30), fx(30, 40)], fade_in: 99 });
assert.deepStrictEqual(fixed.transitions, [{ after: 0, type: "crossfade", duration: 0.7 }]);      // 短いほうの半分に縮む
assert.deepStrictEqual(fixed.effects, []);          // 全体(11.4 秒)を超えた区間は、収まらない・空になるので外れる
assert.strictEqual(fixed.fade_in, 11.4);
fixed = L.fixup({ ...base(), clips: [V("a", 0, 10), V("b", 0, 0.8)] });
assert.deepStrictEqual(fixed.transitions, []);         // 半分(0.4 秒)が 0.5 秒に満たないので外れる

// 時間軸の変換: クロスフェード 1 秒で、後ろのクリップは 1 秒前に食い込む
const o = L.outStarts(base());
assert.deepStrictEqual(o.starts, [0, 9]);
assert.strictEqual(L.tlToOut(base(), 12), 11);
assert.strictEqual(L.outToTl(base(), 11), 12);
assert.strictEqual(L.tlToOut(base(), 10, true), 10);    // 区間の終わりが境目なら、前のクリップの終わり
assert.strictEqual(L.tlToOut(base(), 10), 9);           // 区間の始まりが境目なら、後ろのクリップの頭
assert.strictEqual(L.outToTl(base(), 4), 4);
assert.strictEqual(L.tlToOut({ ...base(), transitions: [] }, 12), 12);
for (const t of [0, 3.3, 10.5, 19.9]) assert.strictEqual(L.outToTl(base(), L.tlToOut(base(), t)), t);
"""

TAB_BUTTON = '<button class="tab" data-tab="edit" role="tab">編集</button>'
# 契約(編集タブの API)にある入り口。edit.js はこれ以外の /api/edit を使わない
API_PATHS = {"/api/edit", "/api/edit/probe", "/api/edit/${id}", "/api/edit/${id}/state", "/api/edit/${id}/undo", "/api/edit/${id}/redo",
             "/api/edit/${id}/preview", "/api/edit/${id}/export", "/api/edit/${view.id}"}


class EditPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (WEB / "index.html").read_text(encoding="utf-8")
        cls.ids = set(re.findall(r'id="([^"]+)"', cls.html))
        cls.js = (WEB / "edit.js").read_text(encoding="utf-8")
        cls.css = (WEB / "style.css").read_text(encoding="utf-8")

    def test_the_tab_and_its_panels_exist(self):
        self.assertIn(TAB_BUTTON, self.html)
        self.assertEqual(len(re.findall(r'<button class="tab[ "][^>]*data-tab="', self.html)), 6)
        for i in ("tab-edit", "edit-project", "ed-body", "ed-video", "ed-ruler", "ed-clips-drop", "ed-song", "ed-ins-drop", "ed-export", "ed-undo", "ed-redo"):
            self.assertIn(i, self.ids)
        self.assertIn('data-tab="edit"', self.css)      # 右上の既定の生成物ビューアを、編集タブでは隠す

    def test_edit_js_is_loaded_after_the_shared_scripts(self):
        order = re.findall(r'<script src="([\w.]+)"></script>', self.html)
        self.assertIn("edit.js", order)
        self.assertLess(max(order.index(f) for f in ("app.js", "gallery.js", "dnd.js", "project.js")), order.index("edit.js"))

    def test_every_ed_id_edit_js_uses_is_in_the_page_and_the_other_way_round(self):
        used = {"ed-" + m for m in re.findall(r'\bed\("([\w-]+)"\)', self.js)}
        used |= set(re.findall(r'\$\("([\w-]+)"\)', self.js))
        self.assertEqual(sorted(u for u in used if u not in self.ids), [])
        page = {i for i in self.ids if i.startswith("ed-")}
        self.assertEqual(sorted(page - used), [], "使われていない ed- の要素")

    def test_api_paths_are_the_contracts(self):
        found = set(re.findall(r'["`](/api/edit[^"`]*)["`]', self.js))
        self.assertEqual(sorted(found - API_PATHS), [])
        for needed in ("/api/edit", "/api/edit/probe", "/api/edit/${id}/state", "/api/edit/${id}/preview", "/api/edit/${id}/undo",
                       "/api/edit/${id}/redo", "/api/edit/${id}/export"):
            self.assertIn(needed, found)
        for method in ('"PUT"', '"PATCH"', '"DELETE"'):
            self.assertIn(method, self.js)

    def test_it_registers_as_an_own_tab_with_polling_only_while_a_job_runs(self):
        self.assertIn("studio.own.edit = {", self.js)
        self.assertIn("if (!view || !view.job || studio.tab !== \"edit\") return;", self.js)

    @unittest.skipUnless(shutil.which("node"), "node が無い")
    def test_edit_js_parses(self):
        r = subprocess.run(["node", "--check", str(WEB / "edit.js")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    @unittest.skipUnless(shutil.which("node"), "node が無い")
    def test_state_building_logic(self):
        r = subprocess.run(["node", "-e", NODE_HARNESS, str(WEB / "edit.js")], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
