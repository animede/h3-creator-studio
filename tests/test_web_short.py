"""ショート動画タブの画面(web/short.js・web/project.js)と web/index.html の整合: スクリプトが使う要素がページにあり、読み込みの順が合っている。
MV タブ(web/mv.js)と共通の部品(makeProjectPanel)を使っていることも確かめる。"""
import re
import unittest

from studio.config import REPO_ROOT

WEB = REPO_ROOT / "web"


class ShortPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (WEB / "index.html").read_text(encoding="utf-8")
        cls.ids = set(re.findall(r'id="([^"]+)"', cls.html))
        cls.short = (WEB / "short.js").read_text(encoding="utf-8")
        cls.mv = (WEB / "mv.js").read_text(encoding="utf-8")
        cls.project = (WEB / "project.js").read_text(encoding="utf-8")

    def test_the_tab_and_its_panel_exist(self):
        self.assertIn('<button class="tab" data-tab="short" role="tab">ショート動画</button>', self.html)
        self.assertIn('id="tab-short"', self.html)
        self.assertIn('<div class="mvproj" id="short-project"></div>', self.html)

    def test_every_sh_id_used_by_short_js_is_in_the_page(self):
        used = {"sh-" + m for m in re.findall(r'\bsh\("([\w-]+)"\)', self.short)}
        used |= {f"sh-{w}-{part}" for w in ("char", "narr") for part in ("busy", "busy-text", "note")}      # startBusy / setNote が which から組み立てる
        used |= set(re.findall(r'\$\("([\w-]+)"\)', self.short))
        self.assertEqual(sorted(u for u in used if u not in self.ids), [])

    def test_the_shared_panel_ids_exist_for_both_tabs(self):
        for i in ("mv-project", "short-project", "hist-grid", "proj-del", "proj-del-text", "proj-del-out", "proj-del-out-fld", "proj-del-out-text", "proj-del-note"):
            self.assertIn(i, self.ids)
        for js in (self.mv, self.short):
            self.assertIn("makeProjectPanel({", js)
        self.assertIn('tab: "short", base: "/api/short"', self.short)
        self.assertIn('tab: "mv", base: "/api/mv"', self.mv)

    def test_scripts_load_the_shared_panel_before_both_tabs(self):
        order = re.findall(r'<script src="([\w.]+)"></script>', self.html)
        self.assertLess(order.index("project.js"), order.index("mv.js"))
        self.assertLess(order.index("project.js"), order.index("short.js"))
        self.assertLess(max(order.index(f) for f in ("app.js", "gallery.js", "dnd.js", "presets.js")), order.index("project.js"))

    def test_the_inputs_the_backend_needs_are_there(self):
        for i in ("sh-story", "sh-song", "sh-narr", "sh-narr-go", "sh-speaker", "sh-images", "sh-char", "sh-char-go", "sh-length", "sh-aspect", "sh-quality",
                  "sh-bgtext", "sh-min", "sh-max", "sh-seed", "sh-random", "sh-final", "sh-go", "sh-len-warn"):
            self.assertIn(i, self.ids)
        for url in ("/api/short", "/api/short/describe/narration", "/api/mv/describe/image", "/api/tts/speakers"):
            self.assertIn(f'"{url}"', self.short)
        # 最終の音声(結合し直しに渡す)と、5 分超の警告
        self.assertIn("final_audio: ui.audio.value", self.project)
        self.assertIn("A4000 の高速で 1 シーン約 3.5 分", self.short)


if __name__ == "__main__":
    unittest.main()
