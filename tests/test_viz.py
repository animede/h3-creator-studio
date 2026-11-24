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

    def test_themes_prefer_known_modes(self):
        modes = [m for m, _ in viz("VIZ_MODES")]
        draw = set(modes) - {"auto"}
        self.assertEqual((len(draw), modes[0], modes[-1]), (10, "spectrum", "auto"))
        self.assertEqual(viz("VIZ_DEFAULT_MODE"), "spectrum")
        for name, theme in viz("VIZ_MOOD_THEMES").items():
            self.assertTrue(theme["modes"] and set(theme["modes"]) <= draw, name)


class VizPageTest(unittest.TestCase):
    def test_page_loads_viz_before_gallery(self):
        order = re.findall(r'<script src="([\w.]+)"></script>', (WEB / "index.html").read_text(encoding="utf-8"))
        self.assertLess(order.index("app.js"), order.index("viz.js"))
        self.assertLess(order.index("viz.js"), order.index("gallery.js"))      # gallery.js の audioCard が vizBox を使う


if __name__ == "__main__":
    unittest.main()
