import unittest

from studio import songplan as sp

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

    def test_template_caption(self):
        self.assertEqual(sp.template_caption("Jazz", "calm", "male vocal"), "A calm Jazz song with a fitting atmosphere and male vocal.")
        self.assertNotIn("vocal", sp.template_caption("Jazz", instrumental=True))


if __name__ == "__main__":
    unittest.main()
