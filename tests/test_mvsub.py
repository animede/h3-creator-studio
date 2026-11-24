import tempfile
import unittest
from pathlib import Path

from studio import mvsub as S


class SplitTest(unittest.TestCase):
    def test_short_text_is_one_line(self):
        self.assertEqual(S.split_caption("今日は晴れです。", 12), ["今日は晴れです。"])

    def test_breaks_after_punctuation_first(self):
        self.assertEqual(S.split_caption("今日は、少しだけ早く学校を出た。夕焼けの道は、いつもより長く見える。", 18),
                         ["今日は、少しだけ早く学校を出た。", "夕焼けの道は、いつもより長く見える。"])

    def test_breaks_after_hiragana_when_no_punctuation(self):
        lines = S.split_caption("まとめて磨きあげる技術をもって進む", 8)
        self.assertEqual("".join(lines), "まとめて磨きあげる技術をもって進む")
        self.assertTrue(all(len(ln) <= 8 + 1 for ln in lines), lines)

    def test_never_splits_an_english_word_or_a_number_from_its_counter(self):
        lines = S.split_caption("Phase C の作業を 2 本まとめて進める計画です", 10)
        joined = " ".join(lines)
        self.assertNotIn("Phase\nC", "\n".join(lines))
        self.assertIn("2 本", joined)


class CuesTest(unittest.TestCase):
    def test_long_sentence_is_split_into_two_line_cues_by_character_ratio(self):
        timings = [{"text": "あ" * 40, "start": 0.0, "end": 8.0}]
        cues = S.build_cues(timings, 10)
        self.assertEqual(len(cues), 2)                                   # 4 行 → 2 行ずつ 2 枚
        self.assertEqual((cues[0][0], cues[0][1], cues[1][1]), (0.0, 4.0, 8.0))
        self.assertEqual(cues[0][2], "あ" * 10 + "\n" + "あ" * 10)

    def test_empty_or_zero_length_sentences_are_skipped(self):
        self.assertEqual(S.build_cues([{"text": "", "start": 0, "end": 1}, {"text": "a", "start": 2, "end": 2}], 10), [])


class WriteTest(unittest.TestCase):
    def test_ass_and_srt(self):
        with tempfile.TemporaryDirectory() as d:
            ass, srt = Path(d) / "s.ass", Path(d) / "s.srt"
            n = S.write_subtitles([{"text": "こんにちは{世界}", "start": 0.0, "end": 1.5}, {"text": "さようなら", "start": 1.5, "end": 3.25}],
                                  768, 448, ass, srt)
            self.assertEqual(n, 2)
            text = ass.read_text("utf-8")
            self.assertIn("PlayResX: 768\nPlayResY: 448", text)
            self.assertIn("Style: Caption,Noto Sans CJK JP,", text)
            self.assertIn("Dialogue: 0,0:00:00.00,0:00:01.50,Caption,,0,0,0,,こんにちは｛世界｝", text)      # 制御文字は全角に
            self.assertIn("Dialogue: 0,0:00:01.50,0:00:03.25,Caption,,0,0,0,,さようなら", text)
            self.assertEqual(srt.read_text("utf-8"), "1\n00:00:00,000 --> 00:00:01,500\nこんにちは{世界}\n\n2\n00:00:01,500 --> 00:00:03,250\nさようなら\n\n")
            with_font = Path(d) / "f.ass"
            S.write_subtitles([{"text": "a", "start": 0, "end": 1}], 448, 768, with_font, Path(d) / "f.srt", font="Meiryo")
            self.assertIn("Style: Caption,Meiryo,", with_font.read_text("utf-8"))

    def test_portrait_lifts_the_caption(self):
        self.assertGreater(S._layout(448, 768)["margin"], S._layout(768, 448)["margin"])

    def test_filter_path_escapes_colons_and_backslashes(self):
        self.assertEqual(S.filter_path("C:\\work\\sub titles.ass"), "C\\:/work/sub titles.ass")
        self.assertEqual(S.filter_path("/tmp/a'b.ass"), "/tmp/a\\'b.ass")


if __name__ == "__main__":
    unittest.main()
