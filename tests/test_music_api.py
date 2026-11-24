import json
import re
import tempfile
import types
import unittest
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from studio import main
from studio.config import REPO_ROOT
from studio.handlers import ace
from studio.ledger import Ledger
from studio.llm import LlmError

PLAN = """THEME: 夜明けの駅
{"recommended_duration": 75, "parts": {}}

[Verse]
始発を待つホーム
[Chorus]
朝の光が来る
"""


class FakeLlm:
    def __init__(self, response=PLAN, error=None):
        self.response, self.error, self.calls = response, error, []

    async def chat(self, system, user, max_tokens=0, temperature=0):
        self.calls.append(user)
        if self.error:
            raise self.error
        return self.response


class MusicApiTest(unittest.TestCase):
    """音楽タブの「作る」ボタンの API。lifespan は使わず、必要な app.state だけを差し替える。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ledger = Ledger(Path(tmp.name))                  # 自作のジャンルを読む台帳
        self.addCleanup(self.ledger.close)
        self.llm, self.busy = FakeLlm(), False
        self.ace_requests = []

        def ace(req: httpx.Request) -> httpx.Response:
            self.ace_requests.append(req.url.path)
            return httpx.Response(200, json={"code": 200, "error": None, "data": {
                "models": [{"name": "acestep-v15-turbo", "is_loaded": True, "vram_ok": True}], "default_model": "acestep-v15-turbo"}})
        self.ace = ace
        self.saved = {k: getattr(main.app.state, k, None) for k in ("llm", "jobs", "sched", "ledger")}
        self.addCleanup(lambda: [setattr(main.app.state, k, v) for k, v in self.saved.items()])
        main.app.state.llm, main.app.state.ledger = self.llm, self.ledger
        main.app.state.sched = types.SimpleNamespace(comps={"ace": types.SimpleNamespace(spec=types.SimpleNamespace(base_url="http://ace.test"))})
        self.use_jobs(httpx.MockTransport(self.ace))
        self.client = TestClient(main.app)

    def use_jobs(self, transport):
        main.app.state.jobs = types.SimpleNamespace(gpu_busy=lambda: self.busy, ctx=types.SimpleNamespace(transport=transport))

    def test_theme(self):
        self.llm.response = "夜行バスの窓の朝焼け"
        r = self.client.post("/api/music/theme", json={"genre": "Rock", "language": "Japanese"})
        self.assertEqual((r.status_code, r.json()), (200, {"theme": "夜行バスの窓の朝焼け"}))
        self.assertIn("Genre: Rock", self.llm.calls[0])

    def test_lyrics_with_and_without_theme(self):
        r = self.client.post("/api/music/lyrics", json={"genre": "J-POP", "language": "Japanese", "mood": "calm and gentle"})
        js = r.json()
        self.assertEqual((r.status_code, js["theme"], js["recommended_duration"]), (200, "夜明けの駅", 75))
        self.assertIn("[Chorus]", js["lyrics"])
        self.assertNotIn("CAPTION:", self.llm.calls[0])
        r = self.client.post("/api/music/lyrics", json={"genre": "J-POP", "theme": "海辺の町"})      # テーマは固定
        self.assertEqual(r.json()["theme"], "海辺の町")
        self.assertIn("海辺の町", self.llm.calls[1])

    def test_caption(self):
        self.llm.response = "A warm ballad with a gentle mood and a clear female vocal"
        r = self.client.post("/api/music/caption", json={"genre": "Ballad", "theme": "窓辺", "lyrics": "[Verse]\nあ", "mood": "calm and gentle",
                                                         "vocal": "female vocal", "thinking": True})
        self.assertEqual((r.status_code, r.json()), (200, {"caption": "A warm ballad with a gentle mood and a clear female vocal."}))
        self.assertIn("Theme/Concept: 窓辺", self.llm.calls[0])
        self.client.post("/api/music/caption", json={"genre": "Ballad", "thinking": False})
        self.assertIn("3-5 complete English prose sentences", self.llm.calls[1])

    def test_gpu_work_does_not_refuse(self):
        """GPU で生成している間も、LLM は(CPU 専用で)動くので、「GPU で生成中」と断らない。"""
        self.busy = True
        for path in ("theme", "lyrics", "caption"):
            r = self.client.post(f"/api/music/{path}", json={"genre": "Jazz"})
            self.assertEqual(r.status_code, 200, path)
        self.assertEqual(len(self.llm.calls), 3)
        r = self.client.post("/api/songplan", json={"genre": "Jazz"})
        self.assertEqual(r.status_code, 200)

    def test_llm_errors_become_503(self):
        self.llm.error = LlmError("llama-server を起動できません")
        r = self.client.post("/api/music/theme", json={})
        self.assertEqual(r.status_code, 503)
        self.assertIn("llama-server", r.json()["detail"])

    def test_a_free_text_genre_up_to_200_chars_is_used_as_the_genre(self):
        """自由に書いた文(200 文字まで)も、ジャンルの名前と同じ場所に入る。長すぎれば日本語の 400。空は J-POP。"""
        free = "90 年代のシティポップに三味線を混ぜた、夏の終わりの切ない曲"
        r = self.client.post("/api/music/theme", json={"genre": free, "language": "Japanese"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn(f"Genre: {free}", self.llm.calls[0])
        self.assertIn("Generate one original song theme in the style described above", self.llm.calls[0])        # 文のまま「ジャンル」の欄に入り、長い文を名前の位置に埋め込まない
        self.assertNotIn("Scene inspiration", self.llm.calls[0])
        longest = "あ" * 200
        for path in ("music/theme", "music/lyrics", "music/caption", "songplan"):
            r = self.client.post(f"/api/{path}", json={"genre": longest})
            self.assertEqual(r.status_code, 200, (path, r.text))
            self.assertIn(longest, self.llm.calls[-1], path)
            r = self.client.post(f"/api/{path}", json={"genre": longest + "あ"})
            self.assertEqual((r.status_code, r.json()["detail"]), (400, "genre は 200 文字以内にしてください"), path)
        n = len(self.llm.calls)
        r = self.client.post("/api/music/theme", json={"genre": "  \n "})                      # 空は J-POP(これまでどおり)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Genre: J-POP", self.llm.calls[n])
        self.client.post("/api/music/theme", json={"genre": "90 年代の\n  シティポップ"})         # 1 行にそろえる
        self.assertIn("Genre: 90 年代の シティポップ", self.llm.calls[n + 1])

    def test_a_custom_genre_brings_its_vibe_and_seeds_to_every_button(self):
        self.ledger.create_genre("和風エレクトロ", "neon shrines, night trains, serene futurism", ["a neon-lit shrine gate at midnight"], ["quiet awe"], "cyber")
        self.llm.response = "夜の神社の踏切"
        r = self.client.post("/api/music/theme", json={"genre": "和風エレクトロ"})
        self.assertEqual((r.status_code, r.json()), (200, {"theme": "夜の神社の踏切"}))
        self.assertIn("Genre essence: neon shrines, night trains, serene futurism", self.llm.calls[0])
        self.assertIn("Scene inspiration: a neon-lit shrine gate at midnight", self.llm.calls[0])
        self.llm.response = PLAN
        for path in ("music/lyrics", "songplan"):
            n = len(self.llm.calls)
            r = self.client.post(f"/api/{path}", json={"genre": "和風エレクトロ", "mood": "calm and gentle"})
            self.assertEqual(r.status_code, 200, (path, r.text))
            self.assertIn("Genre essence: neon shrines, night trains, serene futurism", self.llm.calls[n], path)
            self.assertIn("Emotional tone: quiet awe", self.llm.calls[n], path)
        self.llm.response = "A warm electronic track"
        r = self.client.post("/api/music/caption", json={"genre": "和風エレクトロ", "theme": "夜の神社"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Genre essence: neon shrines, night trains, serene futurism", self.llm.calls[-1])
        r = self.client.post("/api/music/theme", json={"genre": "Rock"})                         # 同梱のジャンルは、これまでどおり
        self.assertIn("Genre essence: rebellion", self.llm.calls[-1])                            # (テーマだけの短い呼び出しは、同梱にも世界観を入れる)
        self.client.post("/api/music/lyrics", json={"genre": "Rock"})
        self.assertNotIn("Genre essence", self.llm.calls[-1])

    def test_a_typed_caption_guides_the_theme_and_lyrics_and_song_plan(self):
        """利用者が書いたキャプション(任意)は、AI が作るテーマ・歌詞の参考として LLM に渡る。書かなければ、これまでどおり。"""
        caption = "A wistful, bittersweet piano ballad with a lonely late-night mood."
        self.llm.response = "夜行バスの窓の朝焼け"
        r = self.client.post("/api/music/theme", json={"genre": "Rock", "language": "Japanese", "caption": caption})
        self.assertEqual((r.status_code, r.json()), (200, {"theme": "夜行バスの窓の朝焼け"}))
        self.assertIn(f'User\'s caption (their own description of the sound): "{caption}"', self.llm.calls[-1])
        self.assertIn("in the theme:", self.llm.calls[-1])
        self.assertNotIn("lyricist", self.llm.calls[-1])
        self.llm.response = PLAN
        prompts = {}
        for path, body in (("music/lyrics", {"mood": "calm and gentle"}), ("music/lyrics", {"theme": "海辺の町"}), ("songplan", {})):
            n = len(self.llm.calls)                                                             # (キャプションの行が無い応答だと、足りないキャプションを別に頼むので、最初の呼び出しを見る)
            r = self.client.post(f"/api/{path}", json={"genre": "J-POP", "language": "Japanese", "caption": caption, **body})
            self.assertEqual(r.status_code, 200, (path, body, r.text))
            user = prompts[path, tuple(body)] = self.llm.calls[n]
            self.assertIn(f'"{caption}"', user, (path, body))
            self.assertIn("Follow the caption's mood, style and atmosphere in the", user, (path, body))
            self.assertIn("never carry its English words into the Japanese", user, (path, body))
        self.assertIn("Mood: calm and gentle", prompts["music/lyrics", ("mood",)])               # チップが明示、キャプションは補足
        self.assertIn("the Mood above stays explicit", prompts["music/lyrics", ("mood",)])
        self.assertIn("Theme (write this exact theme on the THEME line): 海辺の町", prompts["music/lyrics", ("theme",)])
        self.assertIn("in the lyrics:", prompts["music/lyrics", ("theme",)])                    # テーマは固定: 歌詞だけがキャプションに沿う
        self.assertIn("in the theme and lyrics:", prompts["songplan", ()])
        n = len(self.llm.calls)
        self.client.post("/api/music/theme", json={"genre": "Rock"})                             # 書かなければ、これまでどおり
        self.client.post("/api/music/lyrics", json={"genre": "Rock"})
        self.client.post("/api/songplan", json={"genre": "Rock", "caption": "  \n "})           # 空白だけも、書いていないのと同じ
        self.assertGreaterEqual(len(self.llm.calls) - n, 3)
        for call in self.llm.calls[n:]:
            self.assertNotIn("User's caption", call)
        self.client.post("/api/music/caption", json={"genre": "Rock", "theme": "窓辺", "caption": caption})       # キャプションを作るボタンは、書いてあるものを使わない(作り直す)
        self.assertNotIn(caption, self.llm.calls[-1])

    def test_the_typed_caption_is_validated_and_cut_short_for_the_prompt(self):
        longest = "x" * ace.CAPTION_MAX
        for path in ("music/theme", "music/lyrics", "songplan"):
            n = len(self.llm.calls)
            r = self.client.post(f"/api/{path}", json={"genre": "Jazz", "caption": longest})
            self.assertEqual(r.status_code, 200, (path, r.text))
            self.assertIn("x" * 400, self.llm.calls[n], path)                                   # 参考に渡すのは、先頭の 400 文字
            self.assertNotIn("x" * 401, self.llm.calls[n], path)
            n = len(self.llm.calls)
            r = self.client.post(f"/api/{path}", json={"genre": "Jazz", "caption": longest + "x"})
            self.assertEqual((r.status_code, r.json()["detail"]), (400, "caption は 1000 文字以内にしてください"), path)
            self.assertEqual(len(self.llm.calls), n, path)                                      # LLM は呼ばない
            r = self.client.post(f"/api/{path}", json={"genre": "Jazz", "caption": "  " + longest + "  "})        # 前後の空白は数えない
            self.assertEqual(r.status_code, 200, path)
        r = self.client.post("/api/music/theme", json={"genre": "Jazz", "caption": None})
        self.assertEqual(r.status_code, 422)                                                   # 文字列でないものは、これまでの項目と同じく断る
        self.assertEqual(ace.CAPTION_MAX, 1000)

    def test_models_passthrough_and_ace_down(self):
        r = self.client.get("/api/music/models")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["default_model"], "acestep-v15-turbo")
        self.assertEqual(r.json()["models"][0], {"name": "acestep-v15-turbo", "is_loaded": True, "vram_ok": True, "min_vram_gb": None, "is_default": True})

        def down(req):
            raise httpx.ConnectError("refused")
        self.use_jobs(httpx.MockTransport(down))
        self.assertEqual(self.client.get("/api/music/models").json(), {"models": [], "default_model": None, "note": "ACE が起動していません"})


class MusicCaptionUiTest(unittest.TestCase):
    """画面(music.js と index.html)の整合: 書いたキャプションだけを、テーマ・歌詞・生成に渡す。"""

    @classmethod
    def setUpClass(cls):
        cls.music = (REPO_ROOT / "web" / "music.js").read_text(encoding="utf-8")
        cls.html = (REPO_ROOT / "web" / "index.html").read_text(encoding="utf-8")

    def test_only_a_typed_caption_is_sent(self):
        self.assertIn('const userCaption = () => (captionEdited ? val("caption") : "");', self.music)       # AI が入れた文は、書いたことにならない
        for body in ('llm("theme", { ...b, caption: userCaption() }', 'caption: userCaption(),'):
            self.assertIn(body, self.music)
        self.assertRegex(self.music, r'llm\("lyrics", \{[^}]*caption: userCaption\(\)')
        for m in re.finditer(r'val\("caption"\)', self.music):                                    # 欄の値を読むのは、書き換えたとき(captionEdited)だけ
            self.assertTrue(self.music[:m.start()].endswith("captionEdited ? "), self.music[max(0, m.start() - 40):m.end()])
        self.assertEqual(self.music.count("caption: userCaption()"), 3)                          # テーマ・歌詞・生成(「歌詞を新しくして生成」も、生成を通る)
        self.assertNotRegex(self.music, r'llm\("caption"[^;]*userCaption')                       # キャプションを作るボタンは、書いたものを使わない

    def test_the_caption_field_matches_the_server_limit(self):
        tag = re.search(r'<textarea[^>]*\bid="mu-caption"[^>]*>', self.html).group(0)
        self.assertIn(f'maxlength="{ace.CAPTION_MAX}"', tag)


if __name__ == "__main__":
    unittest.main()


class MusicModelsWhenAceDisabledTest(unittest.TestCase):
    """ACE を無効にした設定でも、モデル一覧は 404 にせず空で答える(画面のモデル欄が落ちないように)。lifespan は使わない。"""

    def test_disabled_ace_returns_empty_list(self):
        saved = getattr(main.app.state, "sched", None)
        self.addCleanup(setattr, main.app.state, "sched", saved)
        main.app.state.sched = types.SimpleNamespace(comps={})          # [components.ace] enabled = false のとき
        r = TestClient(main.app).get("/api/music/models")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["models"], [])
        self.assertIn("無効", r.json()["note"])
