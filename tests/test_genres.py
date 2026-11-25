"""音楽タブの自作のジャンル: LLM での定義の作成と検証、名前の検査、API(作る・直す・消す)、画面の選択肢との対応。
lifespan は使わず、台帳は一時フォルダ、LLM は偽物。"""
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from studio import genres, main, songplan
from studio.config import REPO_ROOT
from studio.ledger import Ledger
from studio.llm import LlmError

WEB = REPO_ROOT / "web"
SETTINGS = ["a neon-lit shrine gate at midnight", "a tatami room with a glowing synthesizer", "the last night train to Kyoto",
            "a lantern festival by the river", "a rooftop above Shibuya", "a bamboo forest with hidden speakers", "a koto recital in a club",
            "a rainy platform at dawn"]
EMOTIONS = ["quiet awe", "neon loneliness", "ancient wonder", "restless calm", "gentle defiance", "midnight reverie", "fleeting joy"]
VIBE = "traditional Japanese instruments meeting synthesizers, neon shrines, night trains, serene futurism"


def definition(**over) -> dict:
    return {"vibe": VIBE, "settings": SETTINGS, "emotions": EMOTIONS, "viz": "cyber", **over}


class FakeChat:
    """chat(system, user, max_tokens, temperature) の代わり。responses を順に返し(足りなければ最後のもの)、呼び出しを残す。"""

    def __init__(self, *responses, error=None, during=None):
        self.responses, self.error, self.during, self.calls = responses or (json.dumps(definition()),), error, during, []

    async def __call__(self, system, user, max_tokens=0, temperature=0):
        self.calls.append((system, user, max_tokens, temperature))
        if self.during:
            self.during()
        if self.error:
            raise self.error
        return self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]


class FakeLlm:
    def __init__(self, chat):
        self.chat = chat


class GenerateDefinitionTest(unittest.IsolatedAsyncioTestCase):
    async def test_good_json(self):
        chat = FakeChat()
        got = await genres.generate_definition(chat, "和風エレクトロ", "和楽器とシンセを混ぜた夜の電子音楽")
        self.assertEqual(got, definition())
        self.assertEqual(len(chat.calls), 1)
        system, user, max_tokens, temperature = chat.calls[0]
        self.assertEqual(user, "Genre name: 和風エレクトロ\nDescription: 和楽器とシンセを混ぜた夜の電子音楽\n\nWrite the JSON definition:")
        for name in genres.EXAMPLE_GENRES:                                  # 同梱の 2 つの定義を、そのまま例に見せる
            self.assertIn(f"Genre name: {name}", system)
            self.assertIn(songplan.GENRES[name]["vibe"], system)
            self.assertIn(songplan.GENRES[name]["settings"][0], system)
        self.assertEqual(len(genres.EXAMPLE_GENRES), 2)
        self.assertIn("EVERY value in English", system)                    # 名前・説明は日本語でも、種は英語
        for viz in genres.VIZ_IDS:
            self.assertIn(viz, system)
        self.assertEqual((max_tokens, temperature), (700, 0.7))
        await genres.generate_definition(chat, "Shoegaze")                  # 説明が無ければ、その行は出さない
        self.assertEqual(chat.calls[1][1], "Genre name: Shoegaze\n\nWrite the JSON definition:")

    async def test_examples_have_8_settings_and_7_emotions(self):
        for name in genres.EXAMPLE_GENRES:
            self.assertEqual((len(songplan.GENRES[name]["settings"]), len(songplan.GENRES[name]["emotions"])), (genres.SETTINGS_COUNT, genres.EMOTIONS_COUNT))

    async def test_fenced_json_and_prose_around_it(self):
        for text in (f"```json\n{json.dumps(definition(), indent=2)}\n```", f"Sure! Here is the definition {{as asked}}:\n{json.dumps(definition())}\nHope it helps.",
                     f"```\n{json.dumps(definition())}\n```\nNote: viz is cyber"):
            chat = FakeChat(text)
            self.assertEqual(await genres.generate_definition(chat, "X"), definition(), text[:30])
            self.assertEqual(len(chat.calls), 1)

    async def test_trailing_commas_and_a_list_vibe_are_tolerated(self):
        text = json.dumps(definition(vibe=["neon shrines", "night trains"])).replace('"]', '",]').replace('"}', '",}')
        got = await genres.generate_definition(FakeChat(text), "X")
        self.assertEqual((got["vibe"], got["settings"], got["viz"]), ("neon shrines, night trains", SETTINGS, "cyber"))

    async def test_an_object_wrapped_in_a_list_is_found(self):
        chat = FakeChat(json.dumps([definition()]))
        self.assertEqual(await genres.generate_definition(chat, "X"), definition())
        self.assertEqual(len(chat.calls), 1)

    async def test_prose_then_json_only_retry(self):
        chat = FakeChat("すみません、ジャンルの説明を書きます: とても良いジャンルです。", json.dumps(definition()))
        self.assertEqual(await genres.generate_definition(chat, "X"), definition())
        self.assertEqual(len(chat.calls), 2)
        self.assertNotIn("ONLY the JSON object", chat.calls[0][1])
        self.assertIn("ONLY the JSON object", chat.calls[1][1])             # 2 回目は、JSON だけと念を押す
        self.assertTrue(chat.calls[1][1].startswith(chat.calls[0][1]))
        self.assertLess(chat.calls[1][3], chat.calls[0][3])                  # 温度も下げる

    async def test_unusable_answers_are_retried_once_then_a_japanese_error(self):
        too_few = json.dumps(definition(settings=SETTINGS[:2]))
        japanese = json.dumps(definition(vibe="和風の電子音楽、夜の神社"))
        no_vibe = json.dumps(definition(vibe=""))
        for bad in ("", "{", "[1, 2]", too_few, japanese, no_vibe, json.dumps({"vibe": "x"}), json.dumps(definition(settings="not a list"))):
            chat = FakeChat(bad)
            with self.assertRaises(genres.GenreError, msg=bad[:40]) as cm:
                await genres.generate_definition(chat, "X")
            self.assertEqual((cm.exception.status, len(chat.calls)), (503, 2), bad[:40])
            self.assertIn("ジャンルの定義をうまく作れませんでした", cm.exception.message)
        chat = FakeChat(too_few, json.dumps(definition()))                   # 1 回目が駄目でも、2 回目で取れれば良い
        self.assertEqual(await genres.generate_definition(chat, "X"), definition())

    async def test_llm_error_becomes_a_japanese_message_without_retry(self):
        chat = FakeChat(error=LlmError("llama-server を起動できません"))
        with self.assertRaises(genres.GenreError) as cm:
            await genres.generate_definition(chat, "X")
        self.assertEqual((cm.exception.status, len(chat.calls)), (503, 1))
        self.assertIn("ジャンルの定義を作れませんでした", cm.exception.message)
        self.assertIn("llama-server を起動できません", cm.exception.message)

    async def test_counts_are_padded_and_trimmed(self):
        got = await genres.generate_definition(FakeChat(json.dumps(definition(settings=SETTINGS[:5], emotions=EMOTIONS + ["extra one", "extra two"]))), "X")
        self.assertEqual(got["settings"][:5], SETTINGS[:5])                                       # 足りなければ、既定のジャンルの種で足す
        self.assertEqual(got["settings"][5:], songplan.DEFAULT_GENRE["settings"][:3])
        self.assertEqual(got["emotions"], EMOTIONS)                                               # 多ければ、先頭から 7 つ
        got = await genres.generate_definition(FakeChat(json.dumps(definition(settings=SETTINGS[:4], emotions=EMOTIONS[:4]))), "X")        # 下限ちょうど
        self.assertEqual((len(got["settings"]), len(got["emotions"])), (genres.SETTINGS_COUNT, genres.EMOTIONS_COUNT))
        self.assertEqual(len(set(x.lower() for x in got["settings"] + got["emotions"])), 15)       # 足したものが重ならない
        got = await genres.generate_definition(FakeChat(json.dumps(definition(settings=SETTINGS + [f"extra place {i}" for i in range(30)]))), "X")
        self.assertEqual(got["settings"], SETTINGS)

    async def test_items_are_cleaned(self):
        messy = ["  a quiet room ", "A Quiet Room", "1. a night bus", "- a lantern bridge", "夜の駅", '"a snowy harbor"', "", None, 7, "x" * 100 + " tail", *SETTINGS]
        got = await genres.generate_definition(FakeChat(json.dumps(definition(settings=messy))), "X")
        s = got["settings"]
        self.assertEqual(s[:4], ["a quiet room", "a night bus", "a lantern bridge", "a snowy harbor"])    # 空白・番号・箇条書き・引用符・重なり(大文字小文字)・日本語・空・数
        self.assertEqual(len(s), 8)
        self.assertTrue(all(len(x) <= genres.MAX_SEED for x in s))
        got = await genres.generate_definition(FakeChat(json.dumps(definition(settings="a, b\n- one place;two place;three place;four place\nfive"))), "X")
        self.assertEqual(got["settings"][:5], ["a, b", "one place", "two place", "three place", "four place"])    # 文字列でも、改行と ; で分ける

    async def test_unknown_viz_is_derived_from_the_name_then_the_vibe(self):
        for viz, name, vibe, want in (("sparkly", "Lo-fi Chill", VIBE, "healing"), ("", "Shoegaze", VIBE, "energetic"), (None, "Dark Techno", VIBE, "cyber"),
                                      ("Mystical", "X", VIBE, "mystical"), (["x"], "X", VIBE, "energetic"), ("nope", "Plain", "ambient drones, wide space", "healing"),
                                      (" EPIC ", "X", VIBE, "epic")):
            got = await genres.generate_definition(FakeChat(json.dumps(definition(viz=viz, vibe=vibe))), name)
            self.assertEqual(got["viz"], want, (viz, name))
            self.assertIn(got["viz"], genres.VIZ_IDS)


class ValidationTest(unittest.TestCase):
    def test_name(self):
        self.assertEqual(genres.check_name("  和風  エレクトロ　"), "和風 エレクトロ")                # 前後を削り、途中の空白(全角も)は 1 つに
        self.assertEqual(genres.check_name("あ" * 40), "あ" * 40)
        for bad in ("", "   ", "あ" * 41, None, 5, ["x"], "a\x00b", "a\u200bb", "\u200b"):                     # 制御文字・見えない文字も
            with self.assertRaises(genres.GenreError, msg=repr(bad)) as cm:
                genres.check_name(bad)
            self.assertEqual(cm.exception.status, 400)
        with self.assertRaises(genres.GenreError) as cm:
            genres.check_name("")
        self.assertEqual(cm.exception.message, "名前は 1〜40 文字で入力してください")

    def test_clean_genre(self):
        self.assertEqual(genres.clean_genre("  Rock \n"), "Rock")
        self.assertEqual(genres.clean_genre("90 年代の\nシティポップ  に　三味線"), "90 年代の シティポップ に 三味線")
        self.assertEqual((genres.clean_genre(None), genres.clean_genre("  ")), ("", ""))
        self.assertEqual(len(genres.clean_genre("あ" * 200)), 200)
        with self.assertRaises(ValueError) as cm:
            genres.clean_genre("あ" * 201)
        self.assertEqual(str(cm.exception), "genre は 200 文字以内にしてください")

    def test_seeds_vibe_viz(self):
        self.assertEqual(genres.check_seeds([" a  b ", "", "A B", "c"], "場面"), ["a b", "c"])
        for bad in ("a\nb", [1], [], ["", " "], [f"p{i}" for i in range(genres.MAX_SEEDS + 1)], ["x" * (genres.MAX_SEED_EDIT + 1)], None):
            with self.assertRaises(genres.GenreError, msg=repr(bad)[:40]) as cm:
                genres.check_seeds(bad, "場面")
            self.assertEqual(cm.exception.status, 400)
        self.assertEqual(genres.check_vibe("  a,   b "), "a, b")
        for bad in ("", "  ", None, 3, "x" * (genres.MAX_VIBE + 1)):
            with self.assertRaises(genres.GenreError, msg=repr(bad)):
                genres.check_vibe(bad)
        self.assertEqual(genres.check_viz(" Dark "), "dark")
        for bad in ("", None, "neon", "ENERGETICS"):
            with self.assertRaises(genres.GenreError, msg=repr(bad)):
                genres.check_viz(bad)


class VizRulesTest(unittest.TestCase):
    def test_every_builtin_genre_gets_a_theme_in_the_list(self):
        for name in songplan.GENRES:
            self.assertIn(genres.derive_viz(name), genres.VIZ_IDS, name)
        self.assertEqual([genres.derive_viz(g) for g in ("Jazz", "EDM", "Ballad", "City Pop", "Anime", "Hip Hop", "Rock", "J-POP")],
                         ["healing", "cyber", "nostalgic", "romantic", "epic", "festive", "energetic", "energetic"])
        self.assertEqual((genres.derive_viz(""), genres.derive_viz(None, "xyz"), genres.derive_viz("和風エレクトロ")), ("energetic",) * 3)
        self.assertEqual(genres.derive_viz("xyz", "slow bossa nova"), "healing")                 # 名前に規則が無ければ、次の文字列

    @unittest.skipUnless(shutil.which("node"), "node が PATH に無い")
    def test_python_rules_match_viz_js(self):
        """画面(viz.js の GENRE_TO_VIZ)と同じ規則か: 同梱の 12 と、規則に当たるもの・当たらないものを両方で引く。"""
        probes = [*songplan.GENRES, "Lo-fi Hip Hop", "k pop idol", "Folk", "New Music", "Vocaloid", "Death Metal", "Bossa Nova", "Soul", "Orchestral", "Latin Pop",
                  "Shoegaze", "和風エレクトロ", "", "House", "Trance", "PHONK", "lofi", "lo-fi", "game music"]
        script = ("const vm = require('vm'), fs = require('fs');"
                  "const ctx = vm.createContext({ console, window: {}, document: {}, localStorage: {} });"
                  f"vm.runInContext(fs.readFileSync({json.dumps(str(WEB / 'i18n.js'))}, 'utf8') + fs.readFileSync({json.dumps(str(WEB / 'viz.js'))}, 'utf8'), ctx);"
                  f"process.stdout.write(JSON.stringify({json.dumps(probes)}.map((g) => vm.runInContext('vizThemeId(\"\", ' + JSON.stringify(g) + ')', ctx))));")
        r = subprocess.run(["node", "-e", script], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([genres.derive_viz(g) for g in probes], json.loads(r.stdout))

    def test_viz_ids_match_viz_js_and_the_music_script(self):
        viz_src = (WEB / "viz.js").read_text(encoding="utf-8")
        block = re.search(r"const VIZ_MOOD_THEMES = \{(.*?)\n\};", viz_src, re.S).group(1)
        self.assertEqual(re.findall(r"^  (\w+):", block, re.M), list(genres.VIZ_IDS))
        music = (WEB / "music.js").read_text(encoding="utf-8")
        themes = re.search(r"const VIZ_THEMES = (\[.*?\]);", music, re.S).group(1)
        self.assertEqual(re.findall(r'\["(\w+)", (?:t\()?"', themes), list(genres.VIZ_IDS))


class FakeLlmApiBase(unittest.TestCase):
    """台帳(一時フォルダ)と LLM(偽物)を app.state に入れた API の試験の土台。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ledger = Ledger(Path(tmp.name))
        self.addCleanup(self.ledger.close)
        self.chat = FakeChat()
        self.saved = {k: getattr(main.app.state, k, None) for k in ("ledger", "llm")}
        self.addCleanup(lambda: [setattr(main.app.state, k, v) for k, v in self.saved.items()])
        main.app.state.ledger, main.app.state.llm = self.ledger, FakeLlm(self.chat)
        self.client = TestClient(main.app)


class GenresApiTest(FakeLlmApiBase):
    def create(self, name="和風エレクトロ", description=None, expect=200):
        body = {"name": name} if description is None else {"name": name, "description": description}
        r = self.client.post("/api/music/genres", json=body)
        self.assertEqual(r.status_code, expect, r.text)
        return r.json()

    def names(self) -> list:
        return [g["name"] for g in self.client.get("/api/music/genres").json()]

    def test_list_has_builtins_first_then_mine(self):
        items = self.client.get("/api/music/genres").json()
        self.assertEqual([g["name"] for g in items], list(songplan.GENRES))
        self.assertTrue(all(g["builtin"] for g in items))
        self.assertEqual(set(items[0]), {"id", "name", "vibe", "settings", "emotions", "viz", "description", "builtin"})
        rock = next(g for g in items if g["name"] == "Rock")
        self.assertEqual((rock["id"], rock["vibe"], rock["settings"], rock["viz"], rock["description"]),
                         ("builtin:Rock", songplan.GENRES["Rock"]["vibe"], songplan.GENRES["Rock"]["settings"], "energetic", ""))
        self.assertTrue(all(g["viz"] in genres.VIZ_IDS for g in items))
        mine = self.create("和風エレクトロ", "和楽器とシンセを混ぜた夜の電子音楽")
        self.create("シューゲイザー")
        items = self.client.get("/api/music/genres").json()
        self.assertEqual([g["name"] for g in items], [*songplan.GENRES, "和風エレクトロ", "シューゲイザー"])            # 同梱が先、自作は作った順
        self.assertEqual([g["builtin"] for g in items], [True] * 12 + [False] * 2)
        self.assertEqual(items[12], mine)

    def test_create_asks_the_llm_and_saves_the_definition(self):
        js = self.create("和風エレクトロ", "和楽器とシンセを混ぜた夜の電子音楽")
        self.assertEqual((js["name"], js["vibe"], js["settings"], js["emotions"], js["viz"], js["description"], js["builtin"]),
                         ("和風エレクトロ", VIBE, SETTINGS, EMOTIONS, "cyber", "和楽器とシンセを混ぜた夜の電子音楽", False))
        self.assertTrue(js["id"] and not js["id"].startswith("builtin:"))
        self.assertEqual(len(self.chat.calls), 1)
        self.assertIn("Genre name: 和風エレクトロ", self.chat.calls[0][1])
        self.assertIn("Description: 和楽器とシンセを混ぜた夜の電子音楽", self.chat.calls[0][1])
        row = self.ledger.get_genre(js["id"])                                        # 台帳に入っている
        self.assertEqual((row["name"], row["vibe"], row["settings"], row["emotions"], row["viz"]), ("和風エレクトロ", VIBE, SETTINGS, EMOTIONS, "cyber"))
        self.assertEqual(self.names()[-1], "和風エレクトロ")

    def test_create_trims_the_name_and_description(self):
        js = self.create("  和風   エレクトロ ", "  説明の\n文  ")
        self.assertEqual((js["name"], js["description"]), ("和風 エレクトロ", "説明の 文"))
        self.assertEqual(self.create("Plain")["description"], "")

    def test_name_collisions_are_409_and_do_not_call_the_llm(self):
        self.create("和風エレクトロ")
        calls = len(self.chat.calls)
        for name in ("和風エレクトロ", "  和風エレクトロ ", "Rock", "rock", "ROCK", "ＲＯＣＫ", " hip  hop ", "j-pop", "City POP", "r&b"):      # 同梱とも、大文字小文字・全角半角を区別しない
            r = self.client.post("/api/music/genres", json={"name": name})
            self.assertEqual(r.status_code, 409, name)
            self.assertIn("という名前のジャンルがすでにあります", r.json()["detail"])
        r = self.client.post("/api/music/genres", json={"name": "rock"})
        self.assertEqual(r.json()["detail"], "「rock」という名前のジャンルがすでにあります")
        self.assertEqual(len(self.chat.calls), calls)                                 # LLM は呼ばない
        self.assertEqual(self.names()[12:], ["和風エレクトロ"])
        self.assertEqual(self.create("Shoegaze2")["name"], "Shoegaze2")               # 重ならなければ作れる

    def test_a_same_name_made_while_the_llm_works_is_refused(self):
        self.chat.during = lambda: self.ledger.create_genre("Racer", "v", ["a"], ["b"], "dark")
        r = self.client.post("/api/music/genres", json={"name": "racer"})
        self.assertEqual((r.status_code, [g["name"] for g in self.ledger.list_genres()]), (409, ["Racer"]))

    def test_create_validation(self):
        for body, word in (({}, "名前は 1〜40 文字"), ({"name": ""}, "名前は 1〜40 文字"), ({"name": "   "}, "名前は 1〜40 文字"), ({"name": "あ" * 41}, "名前は 1〜40 文字"),
                           ({"name": "a\x00b"}, "使えない文字"), ({"name": "ok", "description": "あ" * 201}, "説明は 200 文字以内")):
            r = self.client.post("/api/music/genres", json=body)
            self.assertEqual(r.status_code, 400, body)
            self.assertIn(word, r.json()["detail"])
        self.assertEqual((self.create("あ" * 40, "あ" * 200)["name"], len(self.chat.calls)), ("あ" * 40, 1))          # 上限ちょうどは作れる
        self.assertEqual(self.ledger.list_genres()[0]["name"], "あ" * 40)
        self.assertEqual(len(self.ledger.list_genres()), 1)                           # 断ったら何も作らない

    def test_llm_failures_are_503_and_nothing_is_saved(self):
        self.chat.error = LlmError("llama-server を起動できません")
        r = self.client.post("/api/music/genres", json={"name": "X"})
        self.assertEqual(r.status_code, 503)
        self.assertIn("ジャンルの定義を作れませんでした", r.json()["detail"])
        self.assertIn("llama-server", r.json()["detail"])
        self.chat.error, self.chat.responses = None, ("JSON ではありません",)
        r = self.client.post("/api/music/genres", json={"name": "X"})
        self.assertEqual((r.status_code, "うまく作れませんでした" in r.json()["detail"]), (503, True))
        self.assertEqual(self.ledger.list_genres(), [])

    def test_update_changes_only_the_given_fields(self):
        js = self.create()
        url = f"/api/music/genres/{js['id']}"
        r = self.client.put(url, json={"vibe": "  new   vibe "})
        self.assertEqual((r.status_code, r.json()["vibe"], r.json()["settings"], r.json()["name"]), (200, "new vibe", SETTINGS, "和風エレクトロ"))
        r = self.client.put(url, json={"settings": [" a  place ", "", "A Place", "b"], "emotions": ["calm"], "viz": "Dark"})
        self.assertEqual((r.json()["settings"], r.json()["emotions"], r.json()["viz"], r.json()["vibe"]), (["a place", "b"], ["calm"], "dark", "new vibe"))
        r = self.client.put(url, json={"name": " 新しい  名前 ", "vibe": "v2"})
        self.assertEqual((r.json()["name"], r.json()["vibe"]), ("新しい 名前", "v2"))
        row = self.ledger.get_genre(js["id"])
        self.assertEqual((row["name"], row["vibe"], row["settings"], row["emotions"], row["viz"], row["description"]),
                         ("新しい 名前", "v2", ["a place", "b"], ["calm"], "dark", ""))
        self.assertGreaterEqual(row["updated_at"], row["created_at"])
        self.assertEqual(len(self.chat.calls), 1)                                     # 手での編集では LLM を呼ばない
        self.assertEqual(self.names()[12:], ["新しい 名前"])

    def test_update_validation(self):
        js = self.create()
        self.create("シューゲイザー")
        url = f"/api/music/genres/{js['id']}"
        for body, word in (({}, "変更する項目"), ({"name": ""}, "名前は 1〜40 文字"), ({"name": "あ" * 41}, "名前は 1〜40 文字"), ({"vibe": " "}, "世界観"),
                           ({"vibe": "x" * 201}, "世界観"), ({"settings": []}, "場面は 1〜20 個"), ({"settings": ["", "  "]}, "場面は 1〜20 個"),
                           ({"settings": "a\nb"}, "場面は文字の配列"), ({"settings": [1, 2]}, "場面は文字の配列"), ({"emotions": [f"e{i}" for i in range(21)]}, "感情は 1〜20 個"),
                           ({"emotions": ["x" * 101]}, "感情は 1 つ 100 文字以内"), ({"viz": "neon"}, "viz は energetic"), ({"viz": ""}, "viz は")):
            r = self.client.put(url, json=body)
            self.assertEqual(r.status_code, 400, body)
            self.assertIn(word, r.json()["detail"])
        r = self.client.put(url, json={"name": "シューゲイザー"})                        # 名前を変えるときは、ほかの自作とも重ならない
        self.assertEqual((r.status_code, r.json()["detail"]), (409, "「シューゲイザー」という名前のジャンルがすでにあります"))
        for name in ("Rock", "rock", "ＪＡＺＺ", "jazz"):                                 # 同梱の名前とも(大文字小文字・全角半角を区別しない)
            self.assertEqual(self.client.put(url, json={"name": name}).status_code, 409, name)
        self.assertEqual(self.ledger.get_genre(js["id"])["name"], "和風エレクトロ")             # 断ったら変えない
        self.assertEqual(self.client.put(url, json={"name": "和風エレクトロ", "vibe": "same name"}).status_code, 200)     # 自分の名前のままは良い

    def test_renaming_only_the_case_of_your_own_name_is_fine(self):
        js = self.create("Shoegaze")
        r = self.client.put(f"/api/music/genres/{js['id']}", json={"name": "SHOEGAZE"})
        self.assertEqual((r.status_code, r.json()["name"]), (200, "SHOEGAZE"))

    def test_422_for_a_wrong_type(self):
        js = self.create()
        self.assertEqual(self.client.put(f"/api/music/genres/{js['id']}", json={"vibe": 5}).status_code, 422)

    def test_delete(self):
        js = self.create()
        r = self.client.delete(f"/api/music/genres/{js['id']}")
        self.assertEqual((r.status_code, r.json()), (200, {"ok": True}))
        self.assertEqual(self.names(), list(songplan.GENRES))
        r = self.client.delete(f"/api/music/genres/{js['id']}")
        self.assertEqual((r.status_code, r.json()["detail"]), (404, "そのジャンルは見つかりません(削除されたかもしれません)"))
        self.assertEqual(self.client.put(f"/api/music/genres/{js['id']}", json={"vibe": "x"}).status_code, 404)
        self.assertEqual(self.create()["name"], "和風エレクトロ")                         # 消したあとは、同じ名前をまた作れる

    def test_builtins_are_read_only(self):
        want = "同梱のジャンルは変更できません"
        for gid in ("builtin:Rock", "builtin:Hip Hop", "builtin:R&B", "builtin:J-POP"):
            for method, kw in (("put", {"json": {"vibe": "x"}}), ("put", {"json": {}}), ("delete", {})):
                r = getattr(self.client, method)(f"/api/music/genres/{gid}", **kw)
                self.assertEqual((r.status_code, r.json()["detail"]), (400, want), (gid, method))
        for method, kw in (("put", {"json": {"vibe": "x"}}), ("delete", {})):
            self.assertEqual(getattr(self.client, method)("/api/music/genres/builtin:Nope", **kw).status_code, 404)
            self.assertEqual(getattr(self.client, method)("/api/music/genres/nope", **kw).status_code, 404)
        rock = next(g for g in self.client.get("/api/music/genres").json() if g["name"] == "Rock")
        self.assertEqual(rock["vibe"], songplan.GENRES["Rock"]["vibe"])                  # 何も変わっていない


class CustomMapTest(unittest.TestCase):
    def test_custom_map_is_keyed_by_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp))
            try:
                self.assertEqual(genres.custom_map(ledger), {})
                ledger.create_genre("A", "va", ["s1"], ["e1"], "dark", "da")
                ledger.create_genre("B", "vb", ["s2"], ["e2"], "epic")
                got = genres.custom_map(ledger)
                self.assertEqual(list(got), ["A", "B"])
                self.assertEqual((got["A"]["vibe"], got["A"]["settings"], got["A"]["emotions"], got["A"]["description"]), ("va", ["s1"], ["e1"], "da"))
            finally:
                ledger.close()


class GenreUiTest(unittest.TestCase):
    """画面(music.js と index.html)の整合: スクリプトが使う要素が、ページにある。"""

    @classmethod
    def setUpClass(cls):
        cls.html = (WEB / "index.html").read_text(encoding="utf-8")
        cls.music = (WEB / "music.js").read_text(encoding="utf-8")
        cls.ids = set(re.findall(r'id="([^"]+)"', cls.html))

    def test_every_element_music_js_asks_for_is_in_the_page(self):
        used = set(re.findall(r'\bmu\("([\w-]+)"\)', self.music)) | set(re.findall(r'\bonEnter\("([\w-]+)"', self.music))
        used |= set(re.findall(r'"([\w-]+)"', re.search(r"const GENRE_BTNS = (\[.*?\]);", self.music, re.S).group(1)))
        used |= {f"{k}-go" for k in re.findall(r'"(\w+)"', re.search(r"const MAKERS = (\[.*?\]);", self.music, re.S).group(1))}
        used |= set(re.findall(r'"([\w-]+)"', re.search(r"for \(const id of \[([^\]]*)\]\) onEnter", self.music).group(1)))
        missing = sorted(f"mu-{i}" for i in used if f"mu-{i}" not in self.ids)
        self.assertEqual(missing, [])
        for i in ("genre-add", "genre-free", "genre-edit", "genre-busy", "genre-note", "free", "gedit-viz", "genres"):
            self.assertIn(f"mu-{i}", self.ids)

    def test_the_twelve_builtin_chips_are_still_a_plain_list(self):
        """プリセットの試験(test_presets / test_viz)は music.js の const GENRES = [...] を読む。同梱のチップは、その形のまま。"""
        block = re.search(r"const GENRES = (\[.*?\]);", self.music, re.S).group(1)
        self.assertEqual(re.findall(r'"([^"]+)"', block), list(songplan.GENRES))

    def test_free_text_and_name_fields_match_the_server_limits(self):
        attrs = {m.group(1): m.group(0) for m in re.finditer(r'<(?:input|textarea)[^>]*\bid="mu-([\w-]+)"[^>]*>', self.html)}
        for field, limit in (("free", genres.GENRE_MAX), ("genre-desc", genres.GENRE_MAX), ("genre-name", genres.MAX_NAME), ("free-name", genres.MAX_NAME),
                             ("gedit-name", genres.MAX_NAME), ("gedit-vibe", genres.MAX_VIBE)):
            self.assertIn(f'maxlength="{limit}"', attrs[field], field)


if __name__ == "__main__":
    unittest.main()
