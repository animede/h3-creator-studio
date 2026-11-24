"""web/viz.js(音楽の再生中のスペクトラム)の検査: 音楽タブのムード・ジャンルの選択肢との対応と、読み込みの順。
viz.js の定義だけを DOM の無い node に読み込んで確かめる(画面は動かさない)。"""
import json
import re
import shutil
import subprocess
import unittest

from studio.config import REPO_ROOT

WEB = REPO_ROOT / "web"
needs_node = unittest.skipUnless(shutil.which("node"), "node が PATH に無い")


def viz(expr: str):
    """viz.js を node に読み込み、式 expr の値(JSON にできるもの)を返す。"""
    script = ("const vm = require('vm'), fs = require('fs');"
              "const ctx = vm.createContext({ console, window: {}, document: {}, localStorage: {} });"
              f"vm.runInContext(fs.readFileSync({json.dumps(str(WEB / 'viz.js'))}, 'utf8'), ctx);"
              f"process.stdout.write(JSON.stringify(vm.runInContext({json.dumps(expr)}, ctx)));")
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def music_list(name: str, pairs: bool = False) -> list:
    """music.js の const NAME = [...] の中身(pairs なら [値, 表示] の値だけ)。"""
    src = (WEB / "music.js").read_text(encoding="utf-8")
    block = re.search(rf"const {name} = (\[.*?\]);", src, re.S).group(1)
    return re.findall(r'\["([^"]+)", "', block) if pairs else re.findall(r'"([^"]+)"', block)


@needs_node
class VizThemeTest(unittest.TestCase):
    def test_every_mood_chip_has_its_own_theme(self):
        moods = music_list("MOODS", pairs=True)
        self.assertEqual(len(moods), 10)
        keys, themes = viz("Object.keys(MOOD_TO_VIZ)"), viz("Object.keys(VIZ_MOOD_THEMES)")
        self.assertEqual(sorted(keys), sorted(moods), "ムードのチップの値と MOOD_TO_VIZ のキーがずれています(ずれると、黙って既定の色になります)")
        for mood in moods:
            self.assertIn(viz(f"vizThemeId({json.dumps(mood)}, '')"), themes)

    def test_every_genre_chip_matches_a_rule(self):
        genres = music_list("GENRES")
        self.assertEqual(len(genres), 12)
        for genre in genres:
            self.assertTrue(viz(f"GENRE_TO_VIZ.some(([re]) => re.test({json.dumps(genre)}))"), f"{genre} に当たるルールがありません")
        self.assertEqual([viz(f"vizThemeId('', {json.dumps(g)})") for g in ("Jazz", "EDM", "Ballad", "City Pop")], ["healing", "cyber", "nostalgic", "romantic"])

    def test_mood_comes_before_genre_and_unknowns_fall_back(self):
        self.assertEqual(viz("vizThemeId('calm and gentle', 'Rock')"), "healing")
        self.assertEqual(viz("vizThemeId('', 'Rock')"), "energetic")
        for args in ("undefined, undefined", "'', ''", "'constructor', null", "'__proto__', 'unknown genre'", "42, {}"):
            self.assertEqual(viz(f"vizThemeId({args})"), "energetic", args)

    def test_a_theme_given_with_the_song_comes_first(self):
        """自作のジャンルが持つ viz(曲の params の viz)が、ムード・ジャンルより先。10 種に無い値は無いものとして、これまでどおり。"""
        self.assertEqual(viz("vizThemeId('calm and gentle', 'Rock', 'dark')"), "dark")
        self.assertEqual(viz("vizThemeId('', 'Rock', 'mystical')"), "mystical")
        for bad in ("'nope'", "''", "undefined", "null", "'constructor'", "'__proto__'", "'toString'", "7", "{}", "'Dark'"):
            self.assertEqual(viz(f"vizThemeId('calm and gentle', 'Rock', {bad})"), "healing", bad)
            self.assertEqual(viz(f"vizThemeId('', 'Jazz', {bad})"), "healing", bad)
        self.assertEqual(viz("vizThemeId('calm and gentle', 'Rock')"), "healing")                        # 2 つの引数は、これまでどおり
        for theme in viz("Object.keys(VIZ_MOOD_THEMES)"):
            self.assertEqual(viz(f"vizThemeId('', '', {json.dumps(theme)})"), theme)

    def test_every_theme_has_caption_keywords_in_english_and_japanese(self):
        """キャプションの言葉の表: 10 種の雰囲気が 1 つずつ(重ならない)。どれにも英語の語と日本語の語がある。既定の energetic は、同数のときに負けるよう最後。"""
        themes = viz("Object.keys(VIZ_MOOD_THEMES)")
        table = viz("CAPTION_TO_VIZ.map(([id, re]) => [id, re.source, re.flags])")
        self.assertEqual(sorted(t for t, _, _ in table), sorted(themes))
        self.assertEqual(len({t for t, _, _ in table}), 10)
        self.assertEqual(table[-1][0], "energetic")
        for theme, source, flags in table:
            self.assertTrue(re.search(r"[a-z]{3}", source), f"{theme}: 英語の語がありません")
            self.assertTrue(re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", source), f"{theme}: 日本語の語がありません")
            self.assertIn("g", flags)                       # 数えるため(String.match で全部の当たりを数える)
            self.assertIn("i", flags)

    def test_sample_captions_map_to_their_theme(self):
        samples = [
            ("A wistful, bittersweet piano ballad with a lonely late-night mood.", "melancholic"),
            ("A calm, gentle lo-fi hip-hop instrumental with a soothing, relaxed feel.", "healing"),
            ("An epic, cinematic orchestral anthem with a heroic atmosphere.", "epic"),
            ("A futuristic synthwave track with neon electronic textures.", "cyber"),
            ("A romantic and tender ballad about love.", "romantic"),
            ("A dark, heavy and gloomy industrial track with an ominous mood.", "dark"),
            ("A festive party dance track, playful and fun.", "festive"),
            ("A nostalgic 1980s city-pop track with a warm, breezy mood and a smooth female vocal.", "nostalgic"),
            ("A dreamy, ethereal fantasy soundtrack with a mysterious magic feel.", "mystical"),
            ("A bright, upbeat and happy pop song with an energetic driving beat.", "energetic"),
            ("切ない雨の夜のバラード", "melancholic"), ("穏やかで癒される曲", "healing"), ("壮大なオーケストラ", "epic"), ("電子音のサイバーな曲", "cyber"),
            ("恋の歌", "romantic"), ("暗くて重い曲", "dark"), ("祭りのように楽しい曲", "festive"), ("昭和の懐かしいレトロな曲", "nostalgic"),
            ("幻想的で神秘的な曲", "mystical"), ("明るく元気な曲", "energetic"),
            ("ダークなテクノ", "cyber"), ("ヒーリングミュージック", "healing"), ("ラブソング", "romantic"), ("ミステリアスなファンタジー", "mystical"),
            ("せつないバラード", "melancholic"), ("たのしいパーティの曲", "festive"), ("ハッピーなアップテンポ", "energetic"), ("ノスタルジックな曲", "nostalgic"),
            ("A dramatic anime opening song with an intense, emotionally charged atmosphere and a powerful male vocal.", "epic"),      # AI が作るキャプションの型
            ("A sad, slow piano piece", "melancholic"), ("Lo-Fi chillhop beats", "healing"), ("traditional Japanese enka", "nostalgic"),
            ("80's synth and 70s-90s vibes", "nostalgic"), ("90 年代のシティポップ", "nostalgic"), ("SAD SAD", "melancholic"),
        ]
        got = viz(f"{json.dumps([c for c, _ in samples], ensure_ascii=False)}.map((c) => vizThemeId('', '', '', c))")
        self.assertEqual(got, [t for _, t in samples])
        self.assertEqual({t for _, t in samples}, set(viz("Object.keys(VIZ_MOOD_THEMES)")))        # 10 種ぜんぶ、どれかの例で引ける

    def test_the_most_frequent_theme_wins_and_ties_go_to_the_table_order(self):
        self.assertEqual(viz("vizThemeId('', '', '', 'sad sad sad bright happy')"), "melancholic")           # 3 対 2
        self.assertEqual(viz("vizThemeId('', '', '', 'sad bright happy upbeat')"), "energetic")              # 1 対 3
        self.assertEqual(viz("vizThemeId('', '', '', 'bright and sad')"), "melancholic")                     # 同数: 並びが先(energetic は最後)
        self.assertEqual(viz("vizThemeId('', '', '', 'a dark synth track')"), "cyber")
        self.assertEqual(viz("vizThemeId('', '', '', 'a dreamy, nostalgic ballad')"), "nostalgic")
        self.assertEqual(viz("vizThemeId('', '', '', 'a dreamy, nostalgic, retro ballad')"), "nostalgic")     # 2 対 1
        self.assertEqual(viz("vizThemeId('', '', '', 'a dreamy, magic, nostalgic ballad')"), "mystical")      # 2 対 1(並びで後でも、多い方)
        self.assertEqual(viz("CAPTION_TO_VIZ.map(([id]) => id)"), ["melancholic", "healing", "epic", "cyber", "romantic", "dark", "festive", "nostalgic", "mystical", "energetic"])

    def test_words_that_only_look_like_keywords_do_not_count(self):
        for text in ("a funk groove", "a grand piano and a warm voice", "my glove and a clover", "a saddle", "enjoy the show", "technology", "a song about my grandmother",
                     "大切な人への歌", "a late-night mood", "a smooth female vocal", "electric guitar", "2000s J-Pop", "", "   "):
            self.assertEqual(viz(f"vizThemeFromCaption({json.dumps(text, ensure_ascii=False)})"), "", text)
        for text, theme in (("a grand, sweeping chorus", "epic"), ("grandiose strings", "epic"), ("a funky, fun groove", "festive"), ("切なさの残る曲", "melancholic")):
            self.assertEqual(viz(f"vizThemeFromCaption({json.dumps(text, ensure_ascii=False)})"), theme, text)

    def test_caption_comes_after_the_mood_and_before_the_genre(self):
        """順序: 曲に添えた雰囲気(viz)→ ムードのチップ → キャプションの言葉 → ジャンル → energetic。"""
        sad = json.dumps("A sad, slow piano piece")
        self.assertEqual(viz(f"vizThemeId('calm and gentle', 'Rock', 'dark', {sad})"), "dark")                # viz が先頭
        self.assertEqual(viz(f"vizThemeId('calm and gentle', 'Rock', '', {sad})"), "healing")                  # ムードがキャプションより先
        self.assertEqual(viz(f"vizThemeId('', 'Jazz', undefined, {sad})"), "melancholic")                      # キャプションがジャンル(Jazz は healing)より先
        self.assertEqual(viz(f"vizThemeId('', 'Rock', null, {sad})"), "melancholic")
        self.assertEqual(viz("vizThemeId('', 'Jazz', '', 'warm vocals and a steady beat')"), "healing")        # 当たる語が無ければ、ジャンル
        self.assertEqual(viz("vizThemeId('', 'Rock', '', 'warm vocals and a steady beat')"), "energetic")
        self.assertEqual(viz("vizThemeId('', '', '', 'warm vocals and a steady beat')"), "energetic")          # どれも無ければ energetic
        self.assertEqual(viz(f"vizThemeId('my own mood', 'Jazz', 'nope', {sad})"), "melancholic")              # チップに無いムード・10 種に無い viz は、無いものとして次へ
        self.assertEqual(viz("vizThemeId('', '', 'epic', 'A sad song')"), "epic")

    def test_a_caption_that_is_not_a_string_is_ignored_and_old_callers_still_work(self):
        for bad in ("undefined", "null", "42", "{}", "['sad']", "true", "{ toString() { return 'sad'; } }"):
            self.assertEqual(viz(f"vizThemeId('', 'Jazz', '', {bad})"), "healing", bad)
            self.assertEqual(viz(f"vizThemeFromCaption({bad})"), "", bad)
        self.assertEqual(viz("vizThemeId('calm and gentle', 'Rock')"), "healing")                              # 2 つ・3 つの引数は、これまでどおり
        self.assertEqual(viz("vizThemeId('', 'Rock', 'mystical')"), "mystical")
        self.assertEqual(viz("vizThemeId('', 'Rock')"), "energetic")
        self.assertEqual(viz("vizThemeId('', 'x'.repeat(5000), '', 'sad '.repeat(300))"), "melancholic")       # 長くても落ちない

    def test_themes_prefer_known_modes(self):
        modes = [m for m, _ in viz("VIZ_MODES")]
        draw = set(modes) - {"auto"}
        self.assertEqual((len(draw), modes[0], modes[-1]), (10, "spectrum", "auto"))
        self.assertEqual(viz("VIZ_DEFAULT_MODE"), "spectrum")
        for name, theme in viz("VIZ_MOOD_THEMES").items():
            self.assertTrue(theme["modes"] and set(theme["modes"]) <= draw, name)


class VizPageTest(unittest.TestCase):
    def test_the_music_card_passes_the_caption_to_the_visualizer(self):
        src = (WEB / "gallery.js").read_text(encoding="utf-8")
        call = re.search(r"vizBox\(a, (\{[^}]*\})\)", src).group(1)
        for key in ("viz", "mood", "genre", "caption"):
            self.assertIn(f"{key}: p.{key}", call)
        viz_src = (WEB / "viz.js").read_text(encoding="utf-8")
        self.assertIn("vizThemeId(opts.mood, opts.genre, opts.viz, opts.caption)", viz_src)        # attachVisualizer が、4 つとも渡す

    def test_page_loads_viz_before_gallery(self):
        order = re.findall(r'<script src="([\w.]+)"></script>', (WEB / "index.html").read_text(encoding="utf-8"))
        self.assertLess(order.index("app.js"), order.index("viz.js"))
        self.assertLess(order.index("viz.js"), order.index("gallery.js"))      # gallery.js の audioCard が vizBox を使う


if __name__ == "__main__":
    unittest.main()
