import re
import unittest

from studio import songplan as sp

WAFU = {"vibe": "traditional Japanese instruments meeting synthesizers, neon shrines, night trains", "id": "w1",
        "settings": ["a neon-lit shrine gate at midnight", "the last night train to Kyoto", "a koto recital in a club"],
        "emotions": ["quiet awe", "neon loneliness"]}
SHOEGAZE = {"vibe": "walls of reverb guitars, hazy melancholy, 90s indie noise",
            "settings": ["a foggy field at dawn", "a bedroom full of pedals"], "emotions": ["hazy longing"]}

SAMPLE = """THEME: **深夜のコンビニ、最後の缶コーヒー (Shinya no konbini)**
CAPTION: "A nostalgic 2000s J-Pop song with a warm, breezy mood and a clear female vocal"
{"recommended_duration": 75, "parts": {"verse1": 20, "chorus1": 25}}

[Intro]
[Verse 1]
青い光の下で
(Aoi hikari no shita de)
ひとり缶を開ける

[Chorus]
ああ 夜はまだ終わらない
(Aa yoru wa mada owaranai)
"""


class ParseTest(unittest.TestCase):
    def test_parse_song_plan(self):
        plan = sp.parse_song_plan(SAMPLE, want_lyrics=True)
        self.assertEqual(plan["theme"], "深夜のコンビニ、最後の缶コーヒー")
        self.assertEqual(plan["caption"], "A nostalgic 2000s J-Pop song with a warm, breezy mood and a clear female vocal.")
        self.assertEqual(plan["recommended_duration"], 75)
        self.assertEqual(plan["parts"]["chorus1"], 25)
        lines = plan["lyrics"].splitlines()
        self.assertEqual(lines[0], "[Intro]")
        self.assertIn("青い光の下で", lines)
        self.assertNotIn("(Aoi hikari no shita de)", plan["lyrics"])      # ローマ字行は消える
        self.assertNotIn("THEME", plan["lyrics"])

    def test_without_lyrics(self):
        plan = sp.parse_song_plan(SAMPLE, want_lyrics=False)
        self.assertEqual(plan["lyrics"], "")
        self.assertTrue(plan["theme"] and plan["caption"])

    def test_multiline_json_meta_and_code_fence(self):
        resp = 'THEME: 夜行バス\nCAPTION: A track.\n```\n{\n "recommended_duration": 60,\n "parts": {}\n}\n[Verse]\n歌う\n```'
        plan = sp.parse_song_plan(resp, True)
        self.assertEqual(plan["recommended_duration"], 60)
        self.assertEqual(plan["lyrics"].splitlines()[0], "[Verse]")
        self.assertFalse(plan["lyrics"].rstrip().endswith("```"))

    def test_lyrics_under_instrumental_tag_become_verse(self):
        fixed = sp.fix_lyrics_structure("[Intro]\n歌詞がある\n[Chorus]\nサビ")
        self.assertEqual(fixed.splitlines(), ["[Intro]", "[verse]", "歌詞がある", "[Chorus]", "サビ"])

    def test_clean_theme(self):
        self.assertEqual(sp.clean_theme('  "雨の駅"  '), "雨の駅")
        self.assertEqual(sp.clean_theme("一行目\n二行目"), "一行目")
        self.assertEqual(sp.clean_theme("銀河鉄道の夜(Ginga tetsudo no yoru)"), "銀河鉄道の夜")
        long = "とても長いテーマの名前がここから始まります、ここから先は切り捨てる長い説明文が続く"
        self.assertEqual(sp.clean_theme(long), "とても長いテーマの名前がここから始まります")

    def test_clean_caption(self):
        self.assertEqual(sp.clean_caption("```\nA **dreamy** pop song,\nwith `synths`\n```"), "A dreamy pop song, with synths.")
        self.assertEqual(sp.clean_caption("A song.\nNote: this is great"), "A song.")
        self.assertEqual(sp.clean_caption("A song"), "A song.")
        cut = sp.clean_caption("This is a sentence. " * 40)
        self.assertLessEqual(len(cut), 480)
        self.assertTrue(cut.endswith("."))

    def test_inspiration_matches_partial_genre(self):
        self.assertIn(sp.theme_inspiration("Hip Hop")["setting"], sp.GENRES["Hip Hop"]["settings"])
        self.assertEqual(sp.theme_inspiration("Rock")["genre_key"], "Rock")
        self.assertIn(sp.theme_inspiration("Unknown Style")["setting"], sp.DEFAULT_GENRE["settings"])

    def test_inspiration_uses_custom_genres_first(self):
        custom = {"和風エレクトロ": WAFU, "Shoegaze": SHOEGAZE}
        for genre in ("和風エレクトロ", "Shoegaze", "shoegaze", "SHOEGAZE"):                 # 名前そのもの(大文字小文字は区別しない)
            insp = sp.theme_inspiration(genre, custom)
            self.assertEqual(insp["genre_key"], genre)
            self.assertIs(insp["ctx"], custom["Shoegaze" if "oe" in genre.lower() else "和風エレクトロ"])
            self.assertEqual(insp["essence"], insp["ctx"]["vibe"])
            self.assertIn(insp["setting"], insp["ctx"]["settings"])
            self.assertIn(insp["emotion"], insp["ctx"]["emotions"])
        self.assertIs(sp.theme_inspiration("Rock", {"Rock": SHOEGAZE})["ctx"], SHOEGAZE)       # 同じ名前なら、自作が先(アプリは重ならないようにする)

    def test_inspiration_builtin_exact_then_partial_match_over_both(self):
        custom = {"和風エレクトロ": WAFU}
        insp = sp.theme_inspiration("Rock", custom)                                           # 自作に無ければ、同梱
        self.assertIs(insp["ctx"], sp.GENRES["Rock"])
        self.assertEqual(insp["essence"], "")                                                 # 同梱の世界観は、プロンプトに足さない
        insp = sp.theme_inspiration("和風エレクトロ風の夜", custom)                            # 名前の一部(自作の名前が入っている)
        self.assertIs(insp["ctx"], WAFU)
        insp = sp.theme_inspiration("和風", custom)                                           # 入力が自作の名前の一部
        self.assertIs(insp["ctx"], WAFU)
        self.assertIs(sp.theme_inspiration("Lo-fi Hip Hop", custom)["ctx"], sp.GENRES["Hip Hop"])      # 同梱の名前の一部(これまでどおり)
        self.assertIs(sp.theme_inspiration("pop", custom)["ctx"], sp.GENRES["J-POP"])
        both = {"hip": SHOEGAZE}                                                              # 名前の一部が同梱にも自作にも当たれば、自作が先
        self.assertIs(sp.theme_inspiration("Lo-fi Hip Hop", both)["ctx"], SHOEGAZE)
        self.assertIs(sp.theme_inspiration("Hip Hop", both)["ctx"], sp.GENRES["Hip Hop"])         # 名前そのものは、部分一致より先に当たる

    def test_inspiration_falls_back_to_the_default_genre(self):
        insp = sp.theme_inspiration("Unknown Style", {"和風エレクトロ": WAFU})
        self.assertIs(insp["ctx"], sp.DEFAULT_GENRE)
        self.assertEqual(insp["essence"], "")
        self.assertIs(sp.theme_inspiration("", {"和風エレクトロ": WAFU})["ctx"], sp.GENRES["J-POP"])      # 空は J-POP
        self.assertEqual(sp.theme_inspiration("", None)["genre_key"], "J-POP")
        empty = {"X": {"vibe": "v", "settings": [], "emotions": []}}                            # 種が空でも落ちない
        insp = sp.theme_inspiration("X", empty)
        self.assertIn(insp["setting"], sp.DEFAULT_GENRE["settings"])
        self.assertIn(insp["emotion"], sp.DEFAULT_GENRE["emotions"])

    def test_a_long_free_text_is_not_matched_by_a_word_inside_it(self):
        text = "a gentle lullaby for rock climbers waiting at dawn, with a soft ballad feel and kayokyoku strings"       # 40 文字を超える
        self.assertGreater(len(text), sp.GENRE_NAME_MAX)
        self.assertIs(sp.theme_inspiration(text)["ctx"], sp.DEFAULT_GENRE)                    # 「rock」「ballad」が入っていても、別のジャンルの種にしない
        self.assertIs(sp.theme_inspiration("a rock song")["ctx"], sp.GENRES["Rock"])           # 短い(名前の長さ以内)ものは、これまでどおり
        free = "90 年代のシティポップに三味線を混ぜた、夏の終わりの切ない曲"
        self.assertIs(sp.theme_inspiration(free, {"和風エレクトロ": WAFU})["ctx"], sp.DEFAULT_GENRE)
        self.assertEqual(sp.theme_inspiration(free)["genre_key"], free)                       # プロンプトには、書いた文がそのまま入る
        self.assertTrue(sp.theme_inspiration(free)["free"])                                   # 定義が無い: 既定の場面・感情は添えない
        self.assertTrue(sp.theme_inspiration("Unknown Style")["free"])
        for known in ("Rock", "和風エレクトロ"):
            self.assertFalse(sp.theme_inspiration(known, {"和風エレクトロ": WAFU})["free"], known)
        exact = "x" * 60                                                                      # 長くても、名前そのものなら当たる(上限は名前の検査が守る)
        self.assertIs(sp.theme_inspiration(exact, {exact: WAFU})["ctx"], WAFU)


class MakePlanTest(unittest.IsolatedAsyncioTestCase):
    async def test_one_call_makes_theme_caption_lyrics(self):
        calls = []

        async def chat(system, user, max_tokens=0, temperature=0):
            calls.append((system, user, max_tokens))
            return SAMPLE
        plan = await sp.make_plan(chat, "J-POP", "Japanese", mood="bright and happy", vocal="female vocal")
        self.assertEqual(len(calls), 1)
        self.assertIn("ONE response", calls[0][1])
        self.assertIn("Language of theme and lyrics: Japanese", calls[0][1])
        self.assertIn("lyricist", calls[0][0])
        self.assertTrue(plan["lyrics"].startswith("[Intro]"))
        self.assertIn("female vocal", plan["caption"])        # 指定したボーカルが入っていなければ足される

    async def test_retry_once_then_error(self):
        n = []

        async def chat(system, user, max_tokens=0, temperature=0):
            n.append(1)
            return "THEME: だけ"                                # 歌詞が無い
        with self.assertRaises(sp.SongPlanError):
            await sp.make_plan(chat)
        self.assertEqual(len(n), 2)

    async def test_given_theme_is_kept_and_missing_caption_is_filled(self):
        seen = []

        async def chat(system, user, max_tokens=0, temperature=0):
            seen.append(system[:20])
            if system.startswith("You are a music producer"):     # キャプションだけの別呼び出し
                return "A calm piano piece."
            return "THEME: 勝手なテーマ\n[Verse]\n歌う\n"
        plan = await sp.make_plan(chat, theme="海辺の町")
        self.assertEqual(plan["theme"], "海辺の町")
        self.assertEqual(plan["caption"], "A calm piano piece.")
        self.assertEqual(len(seen), 2)

    async def test_instrumental_plan_has_no_lyrics(self):
        async def chat(system, user, max_tokens=0, temperature=0):
            self.assertEqual(max_tokens, 400)
            return "THEME: 星の海\nCAPTION: A calm instrumental track with no vocals."
        plan = await sp.make_plan(chat, "Jazz", want_lyrics=False)
        self.assertEqual((plan["theme"], plan["lyrics"]), ("星の海", ""))

    async def test_theme_only_uses_a_short_prompt(self):
        calls = []

        async def chat(system, user, max_tokens=0, temperature=0):
            calls.append((system, user, max_tokens))
            return '"夜行バスの窓の朝焼け"'
        plan = await sp.make_plan(chat, "Rock", "Japanese", want_lyrics=False, want_caption=False)
        self.assertEqual((plan["theme"], plan["caption"], plan["lyrics"]), ("夜行バスの窓の朝焼け", "", ""))
        self.assertEqual(len(calls), 1)
        self.assertNotIn("lyricist", calls[0][0])           # 約 1000 トークンの作詞用プロンプトは使わない
        self.assertIn("Genre: Rock", calls[0][1])
        self.assertIn("Write the theme in Japanese", calls[0][1])
        plan = await sp.make_plan(chat, theme="海", want_lyrics=False, want_caption=False)    # テーマを渡したら、呼ばずにそのまま
        self.assertEqual((plan["theme"], len(calls)), ("海", 1))

        async def empty(*a, **k):
            return "  "
        with self.assertRaises(sp.SongPlanError):
            await sp.make_plan(empty, want_lyrics=False, want_caption=False)

    async def test_lyrics_without_caption_does_not_ask_for_one(self):
        calls = []

        async def chat(system, user, max_tokens=0, temperature=0):
            calls.append(user)
            return SAMPLE                                   # 勝手にキャプションを付けてきても捨てる
        plan = await sp.make_plan(chat, "J-POP", theme="夜", want_caption=False)
        self.assertEqual((len(calls), plan["caption"], plan["theme"]), (1, "", "夜"))
        self.assertNotIn("CAPTION:", calls[0])
        self.assertTrue(plan["lyrics"].startswith("[Intro]"))

    async def test_make_caption_one_sentence_or_detailed(self):
        calls = []

        async def chat(system, user, max_tokens=0, temperature=0):
            calls.append((system, user, max_tokens))
            return "```\nA warm ballad with a gentle mood\n```"
        cap = await sp.make_caption(chat, "Ballad", "雨の窓辺", "[Verse]\n歌詞", "calm and gentle", "male vocal", "Japanese", thinking=True)
        self.assertEqual(cap, "A warm ballad with a gentle mood, with a male vocal.")      # LLM が省いたボーカルは足される
        self.assertIn("ONE complete English prose sentence", calls[0][1])
        self.assertIn("Theme/Concept: 雨の窓辺", calls[0][1])
        self.assertIn("歌詞", calls[0][1])
        self.assertEqual(calls[0][2], 120)
        await sp.make_caption(chat, "Ballad", thinking=False, instrumental=True, vocal="male vocal", lyrics="[Verse]\nx")
        self.assertIn("3-5 complete English prose sentences", calls[1][1])
        self.assertIn("none (instrumental", calls[1][1])
        self.assertNotIn("male vocal", calls[1][1])         # インストではボーカルを渡さない
        self.assertEqual((calls[1][0] == sp.CAPTION_GENERATE_DETAILED_PROMPT, calls[1][2]), (True, 350))

        async def empty(*a, **k):
            return ""
        with self.assertRaises(sp.SongPlanError):
            await sp.make_caption(empty, "Jazz")

    def test_template_caption(self):
        self.assertEqual(sp.template_caption("Jazz", "calm", "male vocal"), "A calm Jazz song with a fitting atmosphere and male vocal.")
        self.assertNotIn("vocal", sp.template_caption("Jazz", instrumental=True))
        self.assertEqual(sp.template_caption("", "calm"), "A calm pop song with a fitting atmosphere and a fitting vocal.")
        self.assertEqual(sp.template_caption("Lo-fi Hip Hop", "", "", True), "A Lo-fi Hip Hop instrumental track with a fitting atmosphere.")      # 英語の名前は、そのまま入れる

    def test_template_caption_for_a_free_text_or_a_japanese_name_is_plain_english(self):
        """自由に書いた長い文や日本語の名前を、英語のキャプションに埋め込まない(ACE-Step に渡るのは英語の文)。"""
        free = "90 年代のシティポップに三味線を混ぜた、夏の終わりの切ない曲"
        self.assertEqual(sp.template_caption(free, "bittersweet and sad", "female vocal"),
                         "A bittersweet and sad song in the described style with a fitting atmosphere and female vocal.")
        self.assertEqual(sp.template_caption("和風エレクトロ"), "A song in the described style with a fitting atmosphere and a fitting vocal.")
        self.assertEqual(sp.template_caption(free, "intense and powerful"), "An intense and powerful song in the described style with a fitting atmosphere and a fitting vocal.")
        self.assertEqual(sp.template_caption(free, instrumental=True), "An instrumental track in the described style with a fitting atmosphere.")
        long_ascii = "a dreamy 90s shoegaze track with a koto and a long outro"                 # 英語でも、名前の長さを超える文は、そのままは入れない
        self.assertGreater(len(long_ascii), sp.GENRE_NAME_MAX)
        self.assertEqual(sp.template_caption(long_ascii, "calm"), "A calm song in the described style with a fitting atmosphere and a fitting vocal.")
        for genre in (free, "和風エレクトロ", long_ascii):
            self.assertTrue(sp.template_caption(genre, "calm", "male vocal").isascii())             # 英語だけ
            self.assertNotIn(genre, sp.template_caption(genre, "calm", "male vocal"))

    async def test_make_plan_uses_a_custom_genre(self):
        calls = []

        async def chat(system, user, max_tokens=0, temperature=0):
            calls.append(user)
            return SAMPLE
        custom = {"和風エレクトロ": WAFU}
        plan = await sp.make_plan(chat, "和風エレクトロ", "Japanese", custom=custom)
        self.assertEqual(len(calls), 1)
        user = calls[0]
        self.assertIn("Genre: 和風エレクトロ", user)
        self.assertIn(f"Genre essence: {WAFU['vibe']}", user)                                  # 世界観が、プロンプトに入る
        scene = re.search(r"Scene inspiration: (.*)", user).group(1)
        emotion = re.search(r"Emotional tone: (.*)", user).group(1)
        self.assertIn(scene, WAFU["settings"])                                                  # 場面・感情の種も、自作の定義から
        self.assertIn(emotion, WAFU["emotions"])
        self.assertTrue(plan["lyrics"].startswith("[Intro]"))
        await sp.make_plan(chat, "Rock", "Japanese", custom=custom)                             # 同梱のジャンルは、これまでどおり(世界観の行を足さない)
        self.assertNotIn("Genre essence", calls[1])
        self.assertIn(re.search(r"Scene inspiration: (.*)", calls[1]).group(1), sp.GENRES["Rock"]["settings"])
        await sp.make_plan(chat, "Rock", "Japanese")                                            # custom を渡さなくても動く
        self.assertNotIn("Genre essence", calls[2])
        await sp.make_plan(chat, "和風エレクトロ", "Japanese", theme="夜の神社", custom=custom)       # テーマがあるときも、世界観は渡す
        self.assertIn(f"Genre essence: {WAFU['vibe']}", calls[3])
        self.assertIn("Theme (write this exact theme", calls[3])

    async def test_make_plan_with_a_free_text_genre(self):
        calls = []

        async def chat(system, user, max_tokens=0, temperature=0):
            calls.append(user)
            return SAMPLE
        free = "90 年代のシティポップに三味線を混ぜた、夏の終わりの切ない曲"
        await sp.make_plan(chat, free, "Japanese", custom={"和風エレクトロ": WAFU})
        self.assertIn(f"Genre: {free}", calls[0])                                               # 書いた文が、そのままジャンルの欄に入る
        self.assertIn(f"Style: {free}", calls[0])
        self.assertNotIn("Genre essence", calls[0])
        self.assertNotIn("Scene inspiration", calls[0])                                         # どのジャンルにも当てはまる既定の場面・感情は添えない
        self.assertNotIn("Emotional tone", calls[0])
        self.assertIn("Creative approach:", calls[0])                                           # 切り口(毎回変わる)は残す
        await sp.make_plan(chat, free, "Japanese", theme="夏の終わりの海", custom={})           # テーマを渡したときは、これまでどおり
        self.assertIn("Theme (write this exact theme", calls[1])
        await sp.make_plan(chat, "J-POP", "Japanese")                                           # 同梱のジャンルは、これまでどおり場面・感情を添える
        for label in ("Scene inspiration", "Emotional tone", "Creative approach"):
            self.assertIn(label, calls[2])

    async def test_theme_only_uses_the_custom_vibe(self):
        calls = []

        async def chat(system, user, max_tokens=0, temperature=0):
            calls.append((system, user))
            return "夜の神社の踏切"
        plan = await sp.make_plan(chat, "和風エレクトロ", "Japanese", want_lyrics=False, want_caption=False, custom={"和風エレクトロ": WAFU})
        self.assertEqual(plan["theme"], "夜の神社の踏切")
        user = calls[0][1]
        self.assertIn("Genre: 和風エレクトロ", user)
        self.assertIn(f"Genre essence: {WAFU['vibe']}", user)
        self.assertIn(re.search(r"Scene inspiration: (.*)", user).group(1), WAFU["settings"])
        self.assertIn("Generate one original 和風エレクトロ song theme", user)

    async def test_theme_only_for_a_free_text_has_no_default_seeds(self):
        """定義の無いジャンル(自由に書いた文)のテーマは、既定の場面・感情に引きずられないよう、書いた文と切り口だけで頼む。"""
        calls = []

        async def chat(system, user, max_tokens=0, temperature=0):
            calls.append(user)
            return "真夏のカセットテープ"
        free = "90 年代のシティポップに三味線を混ぜた、夏の終わりの切ない曲"
        plan = await sp.make_plan(chat, free, "Japanese", want_lyrics=False, want_caption=False, custom={"和風エレクトロ": WAFU})
        self.assertEqual(plan["theme"], "真夏のカセットテープ")
        user = calls[0]
        self.assertTrue(user.startswith(f"Genre: {free}\nCreative approach: "))
        self.assertTrue(user.endswith("Write the theme in Japanese.\n\nGenerate one original song theme in the style described above:"))
        for label in ("Genre essence", "Scene inspiration", "Emotional tone"):
            self.assertNotIn(label, user)
        await sp.make_plan(chat, "Unknown Style", "Japanese", want_lyrics=False, want_caption=False)       # 知らない名前も同じ
        self.assertNotIn("Scene inspiration", calls[1])
        await sp.make_plan(chat, "Jazz", "Japanese", want_lyrics=False, want_caption=False)               # 同梱は、これまでどおり
        self.assertIn("Genre essence: improvisation", calls[2])
        self.assertTrue(calls[2].endswith("Generate one original Jazz song theme:"))
        self.assertIn(re.search(r"Scene inspiration: (.*)", calls[2]).group(1), sp.GENRES["Jazz"]["settings"])

    async def test_the_fallback_caption_and_make_caption_get_the_custom_vibe(self):
        calls = []

        async def chat(system, user, max_tokens=0, temperature=0):
            calls.append(user)
            if system.startswith("You are a music producer"):
                return "A calm electronic track."
            return "THEME: 夜の神社\n[Verse]\n歌う\n"                                          # キャプションの行が無い
        custom = {"和風エレクトロ": WAFU}
        plan = await sp.make_plan(chat, "和風エレクトロ", "Japanese", custom=custom)
        self.assertEqual(plan["caption"], "A calm electronic track.")
        self.assertIn(f"Genre essence: {WAFU['vibe']}", calls[1])                               # プランにキャプションが無くて別に頼むときも
        await sp.make_caption(chat, "和風エレクトロ", "夜の神社", custom=custom)
        self.assertIn("Genre/Style: 和風エレクトロ", calls[2])
        self.assertIn(f"Genre essence: {WAFU['vibe']}", calls[2])
        await sp.make_caption(chat, "Rock", "夜の神社", custom=custom)
        self.assertNotIn("Genre essence", calls[3])
        free = "90 年代のシティポップに三味線を混ぜた曲"
        await sp.make_caption(chat, free, custom=custom)
        self.assertIn(f"Genre/Style: {free}", calls[4])
        self.assertNotIn("Genre essence", calls[4])
        await sp.make_caption(chat, "和風エレクトロ")                                              # custom を渡さなければ、名前だけ(これまでどおり)
        self.assertNotIn("Genre essence", calls[5])


CAPTION = "A wistful, bittersweet piano ballad with a lonely late-night mood."


class UserCaptionTest(unittest.IsolatedAsyncioTestCase):
    """利用者が書いたキャプションは、AI が書くテーマ・歌詞の参考として LLM に渡る(雰囲気に沿わせ、文は写さず、英語の語を歌詞に混ぜない)。無ければ、これまでどおり。"""

    def setUp(self):
        self.calls = []

        async def chat(system, user, max_tokens=0, temperature=0):
            self.calls.append((system, user))
            return SAMPLE if max_tokens > 100 else "夜行バスの窓の朝焼け"
        self.chat = chat

    def user(self, n=-1):
        return self.calls[n][1]

    def test_caption_context_lines(self):
        lines = sp.caption_context(CAPTION, "Japanese")
        self.assertEqual(lines, [f'User\'s caption (their own description of the sound): "{CAPTION}"',
                                 "Follow the caption's mood, style and atmosphere in the theme and lyrics: word choice, imagery, emotional tone. "
                                 "Do NOT copy or quote it, and never carry its English words into the Japanese theme and lyrics."])
        with_chip = sp.caption_context(CAPTION, "Japanese", mood="calm and gentle")                                           # ムードのチップがあれば、チップが明示(食い違えば、チップが勝つ)
        self.assertIn("; the Mood above stays explicit)", with_chip[0])
        self.assertIn("emotional tone. If it conflicts with the Mood above, the Mood wins. Do NOT copy or quote it", with_chip[1])
        self.assertNotIn("Mood wins", " ".join(lines))                                                                        # チップが無ければ、言わない
        self.assertNotIn("English words", sp.caption_context(CAPTION, "English")[1])                                          # 英語の歌詞には、英語の語を混ぜない注意は要らない
        self.assertIn("in the lyrics:", sp.caption_context(CAPTION, "Japanese", what="lyrics")[1])
        for blank in ("", "   \n ", None):
            self.assertEqual(sp.caption_context(blank, "Japanese"), [], repr(blank))
        self.assertEqual(sp.caption_context(CAPTION, "Japanese", what=""), [])                                                # 書くものが無ければ、渡さない

    async def test_plan_prompt_carries_the_caption_only_when_given(self):
        await sp.make_plan(self.chat, "J-POP", "Japanese", user_caption=CAPTION)
        user = self.user()
        for line in sp.caption_context(CAPTION, "Japanese"):
            self.assertIn(line, user)
        self.assertIn("Mood: as described by the user's caption below", user)                  # チップが無ければ、ムードはキャプションから(作詞の「ムード別の語」の規則が効く)
        self.assertNotIn("match the theme", user)
        self.assertLess(user.index("Mood:"), user.index("User's caption"))
        self.assertLess(user.index("User's caption"), user.index("Vocal style:"))
        self.assertNotIn("Emotional tone", user)                                                # ジャンルの感情の種は、キャプションと食い違わないよう添えない
        self.assertIn("Scene inspiration", user)                                                # 場面と切り口は、これまでどおり
        self.assertIn("Creative approach", user)
        self.assertIn("MOOD-SPECIFIC VOCABULARY", self.calls[0][0])
        await sp.make_plan(self.chat, "J-POP", "Japanese")                                      # 書いていなければ、これまでどおり
        user = self.user()
        for absent in ("User's caption", "Follow the caption", "as described by the user's caption"):
            self.assertNotIn(absent, user)
        self.assertIn("Mood: match the theme", user)
        self.assertIn("Emotional tone", user)
        for blank in ("", "  \n  ", None):
            await sp.make_plan(self.chat, "J-POP", "Japanese", user_caption=blank)
            self.assertNotIn("User's caption", self.user(), repr(blank))
            self.assertIn("Mood: match the theme", self.user(), repr(blank))

    async def test_a_mood_chip_stays_explicit_and_the_caption_supports_it(self):
        await sp.make_plan(self.chat, "J-POP", "Japanese", mood="bright and happy", user_caption=CAPTION)
        user = self.user()
        self.assertIn("Mood: bright and happy", user)
        self.assertNotIn("as described by the user's caption", user)
        self.assertIn(f'User\'s caption (their own description of the sound; the Mood above stays explicit): "{CAPTION}"', user)
        self.assertIn("Follow the caption's mood, style and atmosphere in the theme and lyrics", user)
        self.assertIn("If it conflicts with the Mood above, the Mood wins.", user)
        await sp.make_plan(self.chat, "J-POP", "Japanese", user_caption=CAPTION)                  # チップが無ければ、キャプションがムードの元
        self.assertNotIn("Mood wins", self.user())
        await sp.make_plan(self.chat, "J-POP", "Japanese", mood="bright and happy", want_caption=True, user_caption=CAPTION)       # キャプションも AI が書くとき、チップの指定は残る
        self.assertIn('Mood must be "bright and happy"', self.user())

    async def test_the_caption_is_one_line_and_cut_short(self):
        await sp.make_plan(self.chat, "J-POP", "Japanese", user_caption="  A calm\n\nsea   song  ")
        self.assertIn('(their own description of the sound): "A calm sea song"', self.user())
        long = "x" * 1000
        await sp.make_plan(self.chat, "J-POP", "Japanese", user_caption=long)
        shown = re.search(r'User\'s caption \([^)]*\): "(.*)"', self.user()).group(1)
        self.assertEqual((shown, len(shown)), ("x" * sp.CAPTION_HINT_MAX, 400))
        await sp.make_plan(self.chat, "J-POP", "Japanese", user_caption="あ" * 399 + " い")           # 切れ目の空白は残さない
        self.assertIn('"' + "あ" * 399 + '"', self.user())

    async def test_english_lyrics_need_no_warning_about_english_words(self):
        await sp.make_plan(self.chat, "J-POP", "English", user_caption=CAPTION)
        user = self.user()
        self.assertIn("Do NOT copy or quote it.", user)
        self.assertNotIn("English words", user)
        await sp.make_plan(self.chat, "J-POP", "Korean", user_caption=CAPTION)
        self.assertIn("never carry its English words into the Korean theme and lyrics.", self.user())

    async def test_a_given_theme_leaves_only_the_lyrics_to_follow_the_caption(self):
        await sp.make_plan(self.chat, "J-POP", "Japanese", theme="海辺の町", user_caption=CAPTION)
        user = self.user()
        self.assertIn("Theme (write this exact theme", user)
        self.assertIn("Follow the caption's mood, style and atmosphere in the lyrics:", user)
        self.assertIn("never carry its English words into the Japanese lyrics.", user)
        self.assertNotIn("in the theme and lyrics", user)
        n = len(self.calls)                                                                     # テーマも歌詞も AI が書かない(キャプションだけ)なら、渡さない
        await sp.make_plan(self.chat, "Jazz", "Japanese", theme="海辺の町", want_lyrics=False, user_caption=CAPTION)
        self.assertEqual(len(self.calls), n + 1)
        self.assertNotIn("User's caption", self.user())
        self.assertIn("Mood: match the theme", self.user())

    async def test_an_instrumental_plan_follows_the_caption_in_the_theme(self):
        await sp.make_plan(self.chat, "Jazz", "Japanese", want_lyrics=False, user_caption=CAPTION)
        user = self.user()
        self.assertIn("Follow the caption's mood, style and atmosphere in the theme:", user)
        self.assertIn("never carry its English words into the Japanese theme.", user)           # 歌詞は書かない
        self.assertNotIn("lyrics", user.split("OUTPUT FORMAT")[0].split("Follow the caption")[1])
        self.assertNotIn("<the complete lyrics", user)

    async def test_theme_only_prompt_follows_the_caption(self):
        plan = await sp.make_plan(self.chat, "Rock", "Japanese", want_lyrics=False, want_caption=False, user_caption=CAPTION)
        self.assertEqual(plan["theme"], "夜行バスの窓の朝焼け")
        system, user = self.calls[0]
        self.assertNotIn("lyricist", system)                                                    # 短いテーマ用のプロンプトのまま
        for line in sp.caption_context(CAPTION, "Japanese", what="theme"):
            self.assertIn(line, user)
        self.assertIn("Follow the caption's mood, style and atmosphere in the theme: word choice, imagery, emotional tone.", user)
        self.assertIn("never carry its English words into the Japanese theme.", user)
        self.assertNotIn("Emotional tone", user)                                                # ランダムな感情の種は添えない
        self.assertIn("Scene inspiration", user)
        self.assertIn("Genre essence: rebellion", user)
        self.assertTrue(user.endswith("Write the theme in Japanese.\n\nGenerate one original Rock song theme:"))
        self.assertLess(user.index("Creative approach"), user.index("User's caption"))
        await sp.make_plan(self.chat, "Rock", "Japanese", want_lyrics=False, want_caption=False)       # 書いていなければ、これまでどおり
        user = self.user()
        self.assertNotIn("User's caption", user)
        self.assertIn("Emotional tone", user)
        n = len(self.calls)                                                                     # テーマを渡したら、呼ばずにそのまま
        plan = await sp.make_plan(self.chat, theme="海", want_lyrics=False, want_caption=False, user_caption=CAPTION)
        self.assertEqual((plan["theme"], len(self.calls)), ("海", n))

    async def test_theme_only_prompt_for_a_free_text_genre_follows_the_caption(self):
        free = "90 年代のシティポップに三味線を混ぜた、夏の終わりの切ない曲"
        await sp.make_plan(self.chat, free, "Japanese", want_lyrics=False, want_caption=False, user_caption=CAPTION)
        user = self.user()
        self.assertTrue(user.startswith(f"Genre: {free}\nCreative approach: "))
        self.assertIn(f'"{CAPTION}"', user)
        self.assertTrue(user.endswith("Write the theme in Japanese.\n\nGenerate one original song theme in the style described above:"))
        self.assertLess(user.index("User's caption"), user.index("Write the theme in Japanese."))

    async def test_a_custom_genre_keeps_its_essence_next_to_the_caption(self):
        await sp.make_plan(self.chat, "和風エレクトロ", "Japanese", custom={"和風エレクトロ": WAFU}, user_caption=CAPTION)
        user = self.user()
        self.assertIn(f"Genre essence: {WAFU['vibe']}", user)
        self.assertIn("Scene inspiration", user)
        self.assertNotIn("Emotional tone", user)
        self.assertIn(f'"{CAPTION}"', user)


if __name__ == "__main__":
    unittest.main()
