"""アングル(studio/angles.py)と、それを使う画像タブの edit(ハンドラ、API、画面)の検査。"""
import re
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from studio import angles, main
from studio.handlers import h3image
from studio.i18n import tr
from tests.test_styles import REF_ANIME, HandlerPromptTest

WEB = Path(__file__).resolve().parent.parent / "web"
LEFT = angles.ANGLES["left"]["instruction"]
LEFT_FULL = f"{LEFT} {angles.KEEP_CLAUSE}"


class AnglesTest(unittest.TestCase):
    def test_ids_and_labels(self):
        self.assertEqual(list(angles.ANGLES), ["none", "front", "left", "right", "side", "back", "high", "low"])
        self.assertEqual(angles.ANGLES["none"]["instruction"], "")
        self.assertTrue(all(v["instruction"] for k, v in angles.ANGLES.items() if k != "none"))
        self.assertEqual(angles.public_list()[2], {"id": "left", "ja": "斜め・左向き", "en": "Three-quarter, facing left"})
        self.assertIn("toward the LEFT edge of the picture", LEFT)
        self.assertEqual(angles.ANGLES["low"]["ja"], "見上げ(全身になります)")

    def test_instruction_has_the_keep_clause(self):
        self.assertEqual(angles.angle_instruction("left"), LEFT_FULL)
        self.assertTrue(angles.KEEP_CLAUSE.startswith("Keep the same subject: identical face, hair, outfit, and art style."))
        self.assertTrue(angles.KEEP_CLAUSE.endswith("Do not add other people."))
        for k in angles.ANGLES:
            if k != "none":
                self.assertTrue(angles.angle_instruction(k).endswith(angles.KEEP_CLAUSE), k)
        self.assertEqual(angles.angle_instruction("none"), "")
        self.assertEqual(angles.angle_instruction(None), "")

    def test_apply_angle(self):
        self.assertEqual(angles.apply_angle("猫 ", "left"), f"猫\n\n{LEFT_FULL}")
        self.assertEqual(angles.apply_angle("", "left"), LEFT_FULL)
        self.assertEqual(angles.apply_angle("猫", "none"), "猫")

    def test_check_angle(self):
        self.assertEqual(angles.check_angle(None), "none")
        self.assertEqual(angles.check_angle(""), "none")
        self.assertEqual(angles.check_angle("low"), "low")
        with self.assertRaises(ValueError) as cm:
            angles.check_angle("spin")
        self.assertIn("アングルは none / front", str(cm.exception))
        self.assertTrue(tr(str(cm.exception), "en").startswith("Camera angle must be one of none / front"))


class HandlerAngleTest(unittest.IsolatedAsyncioTestCase):
    setUp = HandlerPromptTest.setUp                    # 台帳と参照画像 1 枚(画風の検査と同じ用意)
    image_prompt = HandlerPromptTest.image_prompt      # H3-Image に送った文と、台帳の params を返す

    def test_normalize_keeps_angle_and_rejects_unknown(self):
        n = h3image.normalize
        self.assertEqual(n("h3image.edit", {"prompt": "猫", "references": [self.img]}, self.ledger)["angle"], "none")
        self.assertEqual(n("h3image.edit", {"prompt": "猫", "references": [self.img], "angle": "back"}, self.ledger)["angle"], "back")
        self.assertEqual(n("h3image.t2i", {"prompt": "猫", "angle": "back"})["angle"], "none")          # t2i にアングルは無い
        with self.assertRaises(ValueError):
            n("h3image.edit", {"prompt": "猫", "references": [self.img], "angle": "spin"}, self.ledger)

    def test_empty_prompt_is_allowed_only_with_an_angle(self):
        n = h3image.normalize
        self.assertEqual(n("h3image.edit", {"prompt": "", "references": [self.img], "angle": "left"}, self.ledger)["prompt"], "")
        for kind, extra in (("h3image.edit", {"references": [self.img]}), ("h3image.t2i", {"angle": "left"})):
            with self.assertRaises(ValueError):
                n(kind, {"prompt": " ", **extra}, self.ledger)

    async def angle_prompt(self, prompt, angle, style):
        return await self.image_prompt("h3image.edit", [self.img], prompt=prompt, angle=angle, style=style)

    async def test_composition_order_and_ledger(self):
        prompt, params = await self.angle_prompt("猫", "left", "anime")
        self.assertEqual(prompt, f"猫\n\n{LEFT_FULL}\n\n{REF_ANIME}")          # 入力欄の文 → アングル → 画風
        self.assertEqual((params["prompt"], params["angle"], params["style"]), ("猫", "left", "anime"))
        prompt, _ = await self.angle_prompt("猫", "left", "none")
        self.assertEqual(prompt, f"猫\n\n{LEFT_FULL}")
        prompt, _ = await self.angle_prompt("", "left", "none")
        self.assertEqual(prompt, LEFT_FULL)
        prompt, _ = await self.angle_prompt("", "left", "anime")
        self.assertEqual(prompt, f"{LEFT_FULL}\n\n{REF_ANIME}")
        prompt, _ = await self.angle_prompt("猫", "none", "anime")
        self.assertEqual(prompt, f"猫\n\n{REF_ANIME}")


class ApiTest(unittest.TestCase):
    def test_angles_route(self):
        js = TestClient(main.app).get("/api/angles").json()
        self.assertEqual(js["default"], "none")
        self.assertEqual([a["id"] for a in js["angles"]], list(angles.ANGLES))
        self.assertNotIn("instruction", js["angles"][1])


class WebTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (WEB / "index.html").read_text(encoding="utf-8")
        cls.i18n = (WEB / "i18n.js").read_text(encoding="utf-8")

    def test_image_tab_has_the_select_and_the_note(self):
        block = re.search(r'<select id="im-angle"[^>]*>(.*?)</select>', self.html, re.S).group(1)
        opts = re.findall(r'<option value="(\w+)" data-i18n>([^<]+)</option>', block)
        self.assertEqual(opts, [(k, v["ja"]) for k, v in angles.ANGLES.items()])      # 画面の選択肢 = studio/angles.py
        self.assertIn('id="im-angle-note"', self.html)
        self.assertIn('id="im-angle-fld" hidden', self.html)                           # 最初は t2i なので隠す
        for px in ("vd", "mv", "sh", "mu"):
            self.assertNotIn(f'id="{px}-angle"', self.html)

    def test_labels_have_english(self):
        for v in angles.ANGLES.values():
            self.assertIn(f'"{v["ja"]}": "{v["en"]}"', self.i18n)
        self.assertIn('"アングル": "Camera angle"', self.i18n)
        self.assertIn('"アングル: {name}(生成時に指示を足します)"', self.i18n)

    def test_image_script_sends_the_angle_only_in_edit(self):
        src = (WEB / "image.js").read_text(encoding="utf-8")
        self.assertIn('const angle = mode === "edit" ? im("angle").value : "none";', src)
        self.assertRegex(src, r"style: im\(\"style\"\)\.value, angle,")
        self.assertIn('im("angle-fld").hidden = m !== "edit";', src)
        self.assertIn('wireAngleNote(im("angle"), im("angle-note"), () => mode === "edit")', src)
        self.assertIn("function wireAngleNote(", (WEB / "presets.js").read_text(encoding="utf-8"))

