"""画風(studio/styles.py)と、それを使う各所(画像・動画のハンドラ、MV・ショート動画の演出、API、画面)の検査。"""
import json
import re
import tempfile
import types
import unittest
from pathlib import Path

import io

import httpx
from PIL import Image
from fastapi.testclient import TestClient

from studio import main, mv, mvscript as M, shortscript as S, styles
from studio.handlers import h3, h3image
from studio.i18n import tr
from studio.jobs import Ctx, Job
from studio.ledger import Ledger

def png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), "red").save(buf, "PNG")
    return buf.getvalue()


WEB = Path(__file__).resolve().parent.parent / "web"
ANIME = styles.STYLES["anime"]["phrase"]
REF_ANIME = ("Keep the referenced subject exactly as in the reference image; render the scene, background, lighting and materials "
             f"in {ANIME}.")


class FakeChat:
    """Llm.chat の代わり。replies を順に返す。呼ばれた中身を calls に残す。"""

    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    async def __call__(self, system, user, max_tokens=0, temperature=0):
        self.calls.append(user)
        return json.dumps(self.replies.pop(0))


class StylesTest(unittest.TestCase):
    def test_ids_and_labels(self):
        self.assertEqual(list(styles.STYLES), ["none", "real", "cinema", "drama", "anime", "illust", "sketch", "watercolor", "oil", "cg3d"])
        self.assertEqual(styles.STYLES["none"]["phrase"], "")
        self.assertTrue(all(v["phrase"] for k, v in styles.STYLES.items() if k != "none"))
        self.assertEqual(styles.public_list()[4], {"id": "anime", "ja": "アニメ", "en": "Anime"})

    def test_phrase_with_and_without_reference(self):
        self.assertEqual(styles.style_phrase("anime", False), f"Visual style: {ANIME}.")
        self.assertEqual(styles.style_phrase("anime", True), REF_ANIME)
        self.assertEqual(styles.style_phrase("none", True), "")
        self.assertEqual(styles.style_phrase(None, False), "")

    def test_apply_style(self):
        self.assertEqual(styles.apply_style("a cat ", "oil", False), "a cat\n\nVisual style: oil painting, visible brush strokes, rich impasto texture.")
        self.assertEqual(styles.apply_style("a cat", "none", True), "a cat")

    def test_check_style(self):
        self.assertEqual(styles.check_style(None), "none")
        self.assertEqual(styles.check_style(""), "none")
        self.assertEqual(styles.check_style("cg3d"), "cg3d")
        with self.assertRaises(ValueError) as cm:
            styles.check_style("pixel")
        self.assertIn("画風は none / real", str(cm.exception))
        self.assertTrue(tr(str(cm.exception), "en").startswith("Visual style must be one of none / real"))


class HandlerPromptTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.ledger = Ledger(self.root)
        self.addCleanup(self.ledger.close)
        p = self.root / "uploads" / "r.png"
        p.parent.mkdir()
        p.write_bytes(png_bytes())
        self.img = self.ledger.add_output("image", p, "upload", filename="r.png")["id"]

    def test_normalize_keeps_style_and_rejects_unknown(self):
        self.assertEqual(h3image.normalize("h3image.t2i", {"prompt": "猫"})["style"], "none")
        self.assertEqual(h3image.normalize("h3image.t2i", {"prompt": "猫", "style": "anime"})["style"], "anime")
        self.assertEqual(h3.normalize("h3.t2va", {"prompt": "猫", "style": "sketch"})["style"], "sketch")
        for fn, kind in ((h3image.normalize, "h3image.t2i"), (h3.normalize, "h3.t2va")):
            with self.assertRaises(ValueError):
                fn(kind, {"prompt": "猫", "style": "pixel"})

    def h3_prompt(self, kind, **params) -> str:
        base = {"prompt": "猫が歩く", "seed": None, "width": 768, "height": 768, "seconds": 5.0, "style": "anime"}
        job = Job(id="j1", tab="video", kind=kind, params={**base, **params})
        ctx = Ctx(None, self.ledger, self.root)
        return h3._build_request(job, ctx)[1]["prompt"]

    def test_h3_prompt_composition(self):
        self.assertEqual(self.h3_prompt("h3.t2va"), f"猫が歩く\n\nVisual style: {ANIME}.")
        self.assertEqual(self.h3_prompt("h3.t2i"), f"猫が歩く\n\nVisual style: {ANIME}.")
        self.assertEqual(self.h3_prompt("h3.ref2va", references=[self.img]), f"猫が歩く\n\n{REF_ANIME}")
        self.assertEqual(self.h3_prompt("h3.ref2i", references=[self.img]), f"猫が歩く\n\n{REF_ANIME}")
        self.assertEqual(self.h3_prompt("h3.fl2va", first_image=self.img, last_image=None), f"猫が歩く\n\n{REF_ANIME}")
        self.assertEqual(self.h3_prompt("h3.t2va", style="none"), "猫が歩く")
        self.assertEqual(self.h3_prompt("h3.t2va", style=None), "猫が歩く")         # 画風の無い古いジョブ

    async def image_prompt(self, kind, refs, **extra) -> tuple:
        sent = []

        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path == "/api/assets":
                return httpx.Response(201, json={"id": "a" * 32})
            if req.method == "POST" and req.url.path == "/api/jobs":
                sent.append(json.loads(req.content))
                return httpx.Response(202, json={"id": "remote1"})
            if req.url.path == "/api/jobs/remote1":
                return httpx.Response(200, json={"status": "completed", "image_url": "/outputs/o.png"})
            if req.url.path == "/outputs/o.png":
                return httpx.Response(200, content=png_bytes())
            return httpx.Response(204)
        sched = types.SimpleNamespace(comps={"h3image": types.SimpleNamespace(spec=types.SimpleNamespace(base_url="http://h3image.test"))})
        job = Job(id="job1", tab="image", kind=kind, params={"prompt": "猫", "width": 512, "height": 512, "seed": 1, "steps": None,
                                                              "references": refs, "style": "anime", **extra})
        await h3image.run(job, Ctx(sched, self.ledger, self.root, transport=httpx.MockTransport(handler)))
        rec = self.ledger.get_output(job.outputs[0])
        return sent[0]["prompt"], rec["params"]

    async def test_h3image_prompt_composition_and_ledger(self):
        prompt, params = await self.image_prompt("h3image.t2i", [])
        self.assertEqual(prompt, f"猫\n\nVisual style: {ANIME}.")
        self.assertEqual((params["prompt"], params["style"]), ("猫", "anime"))      # 台帳には入力欄の文と画風を別に残す
        prompt, _ = await self.image_prompt("h3image.edit", [self.img])
        self.assertEqual(prompt, f"猫\n\n{REF_ANIME}")


def mv_scene(i):
    return {"index": i, "start": (i - 1) * 8.0, "end": i * 8.0, "duration": 8.0, "vocal_ratio": 0.8, "boundary_kind": "bar"}


class ScenarioStyleTest(unittest.IsolatedAsyncioTestCase):
    def test_resolve_format_validates_style(self):
        self.assertEqual(mv.resolve_format({})["style"], "none")
        self.assertEqual(mv.resolve_format({"style": "watercolor"})["style"], "watercolor")
        with self.assertRaises(ValueError):
            mv.resolve_format({"style": "pixel"})

    async def test_mv_scenario_has_the_rule_and_the_style_sentence(self):
        items = [{"index": 1, "emotion": "e", "shot": "s", "camera": "c", "prompt": "She walks"}]
        for lipsync in (True, False):
            chat = FakeChat([{}, items])
            result = await M.generate_scenario(chat, [mv_scene(1)], "", {}, lipsync=lipsync, style="anime")
            self.assertIn(f"STYLE: All scenes share this visual style: {ANIME}; never contradict it.", chat.calls[1])
            text = result["scenes"][0]["prompt"]
            self.assertIn(REF_ANIME, text)
            self.assertNotIn("coherent cinematic music-video style", text)
        chat = FakeChat([{}, items])
        result = await M.generate_scenario(chat, [mv_scene(1)], "", {})
        self.assertNotIn("STYLE:", chat.calls[1])
        self.assertIn(M.DEFAULT_STYLE_SENTENCE, result["scenes"][0]["prompt"])

    async def test_short_scenario_uses_the_reference_form_when_any_image_exists(self):
        item = {"index": 1, "emotion": "e", "place": "p", "refs": [], "shot": "s", "camera": "c", "prompt": "A quiet street"}
        # 画像なし(t2va): 「Visual style:」の文
        chat = FakeChat([[item]])
        result = await S.generate_scenario(chat, [mv_scene(1)], {}, {}, [], timeline="free", frame="landscape", background_text="auto",
                                           roles=[], style="sketch")
        self.assertIn("STYLE: All scenes share this visual style", chat.calls[0])
        text = result["scenes"][0]["prompt"]
        self.assertIn(f"Visual style: {styles.STYLES['sketch']['phrase']}.", text)
        self.assertNotIn("Cinematic, coherent short-film style", text)
        # 景色の画像あり: このシーンが参照を使わなくても、参照の形
        chat = FakeChat([[item]])
        result = await S.generate_scenario(chat, [mv_scene(1)], {}, {}, ["a station"], timeline="free", frame="landscape",
                                           background_text="auto", roles=["scenery"], style="anime")
        self.assertIn(REF_ANIME, result["scenes"][0]["prompt"])
        # 人物の画像あり(ref2va の 6 セクション)
        chat = FakeChat([[{**item, "no_person": False}]])
        result = await S.generate_scenario(chat, [mv_scene(1)], {}, {"subject": "a girl"}, ["a girl"], timeline="free", frame="landscape",
                                           background_text="auto", roles=["person"], style="anime")
        text = result["scenes"][0]["prompt"]
        self.assertIn(f"{REF_ANIME} It contains one continuous shot", text)
        self.assertNotIn("coherent cinematic short-film style", text)


class ApiTest(unittest.TestCase):
    def test_styles_route(self):
        js = TestClient(main.app).get("/api/styles").json()
        self.assertEqual(js["default"], "none")
        self.assertEqual([s["id"] for s in js["styles"]], list(styles.STYLES))
        self.assertNotIn("phrase", js["styles"][1])

    def test_unknown_style_is_400_in_both_languages(self):
        from tests.test_mv_api import MvApiTest
        case = MvApiTest("test_create_returns_the_view_and_submits_the_first_job")
        case.setUp()
        try:
            r = case.client.post("/api/mv", json=case.body(style="pixel"))
            self.assertEqual(r.status_code, 400)
            self.assertIn("画風は", r.json()["detail"])
            r = case.client.post("/api/mv", json=case.body(style="pixel"), headers={"Accept-Language": "en"})
            self.assertEqual(r.status_code, 400)
            self.assertIn("Visual style must be one of", r.json()["detail"])
            view = case.create(style="anime")
            self.assertEqual(view["params"]["style"], "anime")
        finally:
            case.doCleanups()


class WebTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (WEB / "index.html").read_text(encoding="utf-8")
        cls.i18n = (WEB / "i18n.js").read_text(encoding="utf-8")

    def test_each_tab_has_the_select_and_the_note(self):
        for px in ("im", "vd", "mv", "sh"):
            block = re.search(rf'<select id="{px}-style"[^>]*>(.*?)</select>', self.html, re.S).group(1)
            opts = re.findall(r'<option value="(\w+)" data-i18n>([^<]+)</option>', block)
            self.assertEqual(opts, [(k, v["ja"]) for k, v in styles.STYLES.items()], px)      # 画面の選択肢 = studio/styles.py
            self.assertIn(f'id="{px}-style-note"', self.html)
        self.assertNotIn('id="mu-style"', self.html)

    def test_labels_have_english(self):
        for v in styles.STYLES.values():
            self.assertIn(f'"{v["ja"]}": "{v["en"]}"', self.i18n)
        self.assertIn('"画風": "Visual style"', self.i18n)
        self.assertIn('"画風: {name}(生成時に指示を足します)"', self.i18n)

    def test_scripts_send_the_style(self):
        for f, px in (("image.js", "im"), ("video.js", "vd"), ("mv.js", "mvp"), ("short.js", "sh")):
            src = (WEB / f).read_text(encoding="utf-8")
            self.assertIn(f'style: {px}("style").value', src, f)
            self.assertIn(f'wireStyleNote({px}("style"), {px}("style-note"))', src, f)
