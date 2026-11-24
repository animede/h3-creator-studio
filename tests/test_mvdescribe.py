import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from studio import mv_audio
from studio import mvdescribe as D
from studio import mvscript
from studio.llm import LlmError

FACTS = {"subject": "a young anime-style girl", "face": "brown eyes", "hair": "long silver hair", "outfit": "blue sailor uniform",
         "style": "anime cel shading", "identity_anchor": "A young anime-style girl with long silver hair"}
JA = "長い銀髪、茶色の瞳、青いセーラー服の少女。アニメ調"


def vision_reply(ja=JA, **over) -> str:
    return json.dumps({**FACTS, "description_ja": ja, **over}, ensure_ascii=False)


class FakeLlm:
    """Llm の代わり(chat と chat_image と chat_audio)。replies は順に返す(Exception なら投げる)。呼ばれた中身は seen / chats / heard に残す。"""

    def __init__(self, vision=(), texts=(), audio=()):
        self.vision, self.texts, self.audio, self.seen, self.chats, self.heard = list(vision), list(texts), list(audio), [], [], []

    async def chat_audio(self, system, user, clips, max_tokens=0, temperature=0):
        self.heard.append({"system": system, "user": user, "clips": list(clips), "max_tokens": max_tokens, "temperature": temperature})
        reply = self.audio.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, str) else json.dumps(reply)

    async def chat_image(self, system, user, image_bytes, mime="image/png", max_tokens=0, temperature=0):
        self.seen.append({"system": system, "user": user, "image": image_bytes, "mime": mime, "max_tokens": max_tokens, "temperature": temperature})
        reply = self.vision.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def chat(self, system, user, max_tokens=0, temperature=0):
        self.chats.append({"system": system, "user": user, "max_tokens": max_tokens, "temperature": temperature})
        reply = self.texts.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class DescribeCharacterTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "girl.png"
        buf = io.BytesIO()
        Image.new("RGB", (64, 48), (10, 120, 200)).save(buf, "PNG")
        self.path.write_bytes(buf.getvalue())

    async def test_vision_answer_becomes_a_japanese_line_and_english_facts(self):
        llm = FakeLlm(vision=[vision_reply()])
        r = await D.describe_character(llm, self.path)
        self.assertEqual(r, {"text": JA, "facts": FACTS, "source": "vision", "note": ""})
        (call,) = llm.seen
        self.assertEqual(call["image"], self.path.read_bytes())
        self.assertEqual((call["mime"], call["temperature"]), ("image/png", 0.0))
        self.assertEqual(call["system"], D.VISION_SYSTEM)
        self.assertEqual(llm.chats, [])                                          # 日本語の 1 行が答えに入っていれば、LLM は 1 回だけ

    def test_the_prompt_asks_for_the_appearance_only(self):
        self.assertIn('{"subject":"","face":"","hair":"","outfit":"","style":"","identity_anchor":"","description_ja":""}', D.VISION_PROMPT)
        for word in ("expression", "location", "background", "lighting", "pose", "action", "held props"):
            self.assertIn(word, D.VISION_PROMPT)                                 # 見た目だけ: これらは入れない
        self.assertIn("Never invent a different hairstyle", D.VISION_SYSTEM)     # lipsync-mv の analyze_character の system プロンプト

    async def test_fenced_json_and_missing_fields_are_fine(self):
        reply = "```json\n" + json.dumps({"subject": "a robot", "outfit": "red helmet", "description_ja": "赤いヘルメットのロボット。カートゥーン調", "hair": 5}) + "\n```"
        r = await D.describe_character(FakeLlm(vision=[reply]), self.path)
        self.assertEqual(r["facts"], {"subject": "a robot", "outfit": "red helmet", "hair": "5"})     # 空の項目は無い
        self.assertEqual((r["text"], r["source"]), ("赤いヘルメットのロボット。カートゥーン調", "vision"))

    async def test_without_a_japanese_line_the_facts_are_turned_into_one_by_a_text_call(self):
        for ja in ("", "a girl with silver hair"):                               # 空、日本語でない
            llm = FakeLlm(vision=[vision_reply(ja=ja)], texts=["「長い銀髪、青い瞳の少女。アニメ調」"])
            r = await D.describe_character(llm, self.path)
            self.assertEqual((r["text"], r["source"]), ("長い銀髪、青い瞳の少女。アニメ調", "vision"), ja)      # 鉤括弧は外す
            (call,) = llm.chats
            self.assertIn("ONE compact Japanese line", call["user"])
            self.assertIn("long silver hair", call["user"])
            self.assertNotIn("identity_anchor", call["user"])                    # 日本語にする材料は見た目だけ
        llm = FakeLlm(vision=[vision_reply(ja="")], texts=[LlmError("止まっています")])
        r = await D.describe_character(llm, self.path)                           # 日本語にできなければ、英語の特徴をつなげて使う
        self.assertEqual(r["text"], "a young anime-style girl, brown eyes, long silver hair, blue sailor uniform, anime cel shading")

    async def test_unreadable_json_is_asked_again_once(self):
        llm = FakeLlm(vision=["I think the picture shows a girl.", vision_reply()])
        r = await D.describe_character(llm, self.path)
        self.assertEqual((r["source"], r["text"]), ("vision", JA))
        self.assertEqual(len(llm.seen), 2)
        self.assertNotIn("not valid JSON", llm.seen[0]["user"])
        self.assertTrue(llm.seen[1]["user"].startswith(D.VISION_PROMPT))
        self.assertIn("not valid JSON", llm.seen[1]["user"])                     # 同じ依頼に「JSON だけで答え直す」指示を足す
        llm = FakeLlm(vision=["no json", "still no json"])
        r = await D.describe_character(llm, self.path)                           # 2 回続けて読めなければ、あきらめる(例外にはしない)
        self.assertEqual((r["source"], r["text"], len(llm.seen)), ("default", "", 2))
        self.assertIn("画像の解析の答えを読めませんでした", r["note"])

    async def test_no_person_is_not_asked_again_and_falls_back(self):
        empty = json.dumps({k: "" for k in (*D.FACT_KEYS, "description_ja")})
        llm = FakeLlm(vision=[empty])
        r = await D.describe_character(llm, self.path)
        self.assertEqual((r["source"], r["text"], r["facts"], len(llm.seen)), ("default", "", {"subject": mvscript.DEFAULT_SUBJECT}, 1))
        self.assertIn("人物が写っていないかもしれません", r["note"])
        self.assertIn("画像のプロンプトも無い", r["note"])

    async def test_vision_unavailable_falls_back_to_the_ledger_prompt(self):
        facts = json.dumps({"subject": "a girl", "face": "", "hair": "silver hair", "outfit": "", "style": "anime"})
        llm = FakeLlm(vision=[LlmError("画像を読む投影ファイル(mmproj)を取得できませんでした")], texts=[facts, "銀髪の少女。アニメ調"])
        r = await D.describe_character(llm, self.path, "a girl with silver hair in a sunny room, anime")
        self.assertEqual(r["source"], "prompt")
        self.assertEqual(r["facts"], {"subject": "a girl", "hair": "silver hair", "style": "anime"})      # mvscript.character_facts の抜き出し
        self.assertEqual(r["text"], "銀髪の少女。アニメ調")
        self.assertIn("投影ファイル", r["note"])
        self.assertIn("画像のプロンプトから見た目を抜き出しました", r["note"])
        self.assertIn("DESCRIPTION: a girl with silver hair in a sunny room, anime", llm.chats[0]["user"])

    async def test_vision_unavailable_and_no_prompt_gives_the_neutral_default(self):
        llm = FakeLlm(vision=[LlmError("この LLM は画像を読めません")])
        r = await D.describe_character(llm, self.path, "  ")
        self.assertEqual((r["source"], r["text"], r["facts"]), ("default", "", {"subject": mvscript.DEFAULT_SUBJECT}))
        self.assertIn("この LLM は画像を読めません", r["note"])
        self.assertEqual(llm.chats, [])                                          # LLM を呼ばない
        r = await D.describe_character(FakeLlm(), self.path.with_name("gone.png"), "")      # ファイルが無い
        self.assertEqual((r["source"], r["text"]), ("default", ""))

    async def test_a_dead_llm_is_an_error_only_when_the_fallback_needs_it(self):
        llm = FakeLlm(vision=[LlmError("llama-server を起動できません")], texts=[LlmError("llama-server を起動できません")])
        with self.assertRaises(LlmError):                                        # 台帳のプロンプトから抜き出すにも LLM が要る
            await D.describe_character(llm, self.path, "a girl")

    async def test_long_text_is_clipped(self):
        r = await D.describe_character(FakeLlm(vision=[vision_reply(ja="あ" * 2000)]), self.path)
        self.assertLessEqual(len(r["text"]), 600)


def heard_reply(**over) -> dict:
    return {"language": "Japanese", "genre": "J-Pop", "mood": "Melancholy, nostalgic", "instruments": "Piano, strings", "vocal": "female", "tempo": "medium", **over}


class ExcerptTest(unittest.TestCase):
    def test_which_parts_of_a_song_are_heard(self):
        for duration, starts in ((None, [0.0]), ("x", [0.0]), (0, [0.0]), (12, [0.0]), (30, [0.0]), (40, [0.0]),       # 40 秒以下(長さ不明も)は先頭の 1 か所
                                 (41, [0.0]), (50, [0.0]),                                                            # 2 か所が近すぎるときも 1 か所
                                 (55, [13.75, 25.0]), (60, [15.0, 30.0]), (155, [38.75, 93.0]), (600, [150.0, 360.0])):    # 長さの 25% と 60%(終わりに収まるよう寄せる)
            self.assertEqual(D.excerpt_starts(duration), starts, duration)
        for duration in (55, 60, 100, 155, 400):
            for start in D.excerpt_starts(duration):
                self.assertLessEqual(start + D.EXCERPT_S, duration)                                                   # 曲の終わりをはみ出さない


class ListenSongTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.song = Path(tmp.name) / "song.mp3"
        self.song.write_bytes(b"ID3fake")
        self.cuts = []

        async def fake_cut(src, start, end, dst, *, rate=0, channels=0, pad_to=0.0):
            self.cuts.append({"src": Path(src), "start": start, "end": end, "rate": rate, "channels": channels, "name": Path(dst).name})
            Path(dst).write_bytes(f"RIFF{len(self.cuts)}".encode())
            return Path(dst)
        p = mock.patch.object(mv_audio, "cut", fake_cut)
        p.start()
        self.addCleanup(p.stop)

    async def test_a_short_song_is_heard_once_from_the_start_as_16k_mono(self):
        llm = FakeLlm(audio=[heard_reply()])
        heard = await D.listen_song(llm, self.song, 30.0)
        self.assertEqual(heard, {"language": "Japanese", "genre": "J-Pop", "mood": "Melancholy, nostalgic", "instruments": "Piano, strings",
                                 "vocal": "female", "tempo": "medium"})
        (cut,) = self.cuts
        self.assertEqual((cut["src"], cut["start"], cut["end"], cut["rate"], cut["channels"]), (self.song, 0.0, 30.0, 16000, 1))
        (call,) = llm.heard
        self.assertEqual(call["clips"], [b"RIFF1"])                                      # 1 か所ずつ聞かせる(2 か所を 1 回にすると、先頭の方だけを答えがち)
        self.assertEqual((call["system"], call["temperature"]), (D.LISTEN_SYSTEM, 0.0))
        self.assertIn('Return JSON only: {"language": "<language of the vocals, or none>", "genre": "<genre>"', call["user"])
        self.assertIn("City Pop, Bossa Nova", call["user"])                     # ジャンルの例(「Pop, Rock」に寄らないように)
        self.assertIn("Do not write lyrics", call["user"])                               # 歌詞は聞き取らない
        self.assertNotIn("lyrics\"", call["user"])                                       # 答えの項目にも、歌詞は無い

    async def test_a_long_song_is_heard_at_two_places_and_the_answers_are_merged(self):
        llm = FakeLlm(audio=[heard_reply(genre="Pop Rock", mood="Energetic, Melancholy", instruments="Drums, Guitar"),
                             heard_reply(genre=["Pop", "ballad", "pop rock"], mood="melancholy, Reflective", instruments="Piano, Drums", vocal="Female", tempo="Slow")])
        heard = await D.listen_song(llm, self.song, 155.0)
        self.assertEqual([(c["start"], c["end"]) for c in self.cuts], [(38.75, 68.75), (93.0, 123.0)])
        self.assertEqual([c["clips"] for c in llm.heard], [[b"RIFF1"], [b"RIFF2"]])
        self.assertEqual(heard["genre"], "Pop Rock, Pop, ballad")                          # 同じ語は 1 つに(大文字小文字は区別しない)。配列の答えも読める
        self.assertEqual(heard["mood"], "Energetic, Melancholy, Reflective")
        self.assertEqual(heard["instruments"], "Drums, Guitar, Piano")
        self.assertEqual((heard["language"], heard["vocal"], heard["tempo"]), ("Japanese", "female", "medium, Slow"))

    async def test_a_part_without_singing_does_not_cancel_the_part_with_it(self):
        llm = FakeLlm(audio=[heard_reply(language="instrumental", vocal="none"), heard_reply(language="Japanese", vocal="female")])
        heard = await D.listen_song(llm, self.song, 155.0)
        self.assertEqual((heard["language"], heard["vocal"]), ("Japanese", "female"))              # 間奏だけの 1 か所の「歌なし」は捨てる
        heard = await D.listen_song(FakeLlm(audio=[heard_reply(language="instrumental", vocal="none"), heard_reply(language="instrumental", vocal="none")]), self.song, 155.0)
        self.assertEqual((heard["language"], heard["vocal"]), ("instrumental", "none"))            # どこも歌なしなら、そのまま

    async def test_tolerant_json_and_one_retry(self):
        llm = FakeLlm(audio=["Sure! Here is the analysis: it sounds like a ballad.", "```json\n" + json.dumps(heard_reply(genre="Ballad")) + "\n```"])
        heard = await D.listen_song(llm, self.song, 20)
        self.assertEqual(heard["genre"], "Ballad")
        self.assertEqual(len(llm.heard), 2)
        self.assertNotIn("not valid JSON", llm.heard[0]["user"])
        self.assertTrue(llm.heard[1]["user"].startswith(D.LISTEN_PROMPT))
        self.assertIn("not valid JSON", llm.heard[1]["user"])                            # 同じ依頼に「JSON だけで答え直す」指示を足す
        messy = {"Language": "Japanese", "GENRE": None, "mood": ["Sad", "Calm"], "instruments": "unknown", "vocal": "N/A", "tempo": "  ", "extra": "ignored", "lyrics": "x"}
        heard = await D.listen_song(FakeLlm(audio=[messy]), self.song, 20)
        self.assertEqual(heard, {"language": "Japanese", "mood": "Sad, Calm"})            # 項目名は大文字小文字を問わず、空・unknown・知らない項目は捨てる

    async def test_an_unreadable_part_is_skipped_but_nothing_heard_is_an_error(self):
        llm = FakeLlm(audio=["no json", "still no json", heard_reply(genre="Rock")])        # 1 か所目は、頼み直しても読めない
        heard = await D.listen_song(llm, self.song, 155.0)
        self.assertEqual((heard["genre"], len(llm.heard)), ("Rock", 3))
        llm = FakeLlm(audio=["no json", "still no json", "nope", "nope again"])
        with self.assertRaises(LlmError) as cm:
            await D.listen_song(llm, self.song, 155.0)
        self.assertIn("曲を聞き取れませんでした", cm.exception.message)
        empty = {k: "" for k in D.LISTEN_FIELDS}
        llm = FakeLlm(audio=[empty])                                                     # JSON は読めて、何も聞き取れていない: 頼み直さない
        with self.assertRaises(LlmError) as cm:
            await D.listen_song(llm, self.song, 20)
        self.assertEqual(len(llm.heard), 1)
        self.assertIn("何も聞き取れませんでした", cm.exception.message)

    async def test_llm_and_ffmpeg_failures_are_raised_as_they_are(self):
        llm = FakeLlm(audio=[LlmError("この LLM は音声を読めません")])
        with self.assertRaises(LlmError) as cm:                                          # 音声を読めない LLM には、2 か所目を試さない
            await D.listen_song(llm, self.song, 155.0)
        self.assertEqual((cm.exception.message, len(llm.heard)), ("この LLM は音声を読めません", 1))
        err = mv_audio.MvAudioError("音声の切り出しに失敗しました(ffmpeg): x", "tail")
        with mock.patch.object(mv_audio, "cut", side_effect=err):
            with self.assertRaises(mv_audio.MvAudioError):
                await D.listen_song(FakeLlm(), self.song, 20)

    async def test_the_excerpt_files_are_removed_and_the_song_is_not_touched(self):
        seen_dirs = []
        original = mv_audio.cut

        async def spying_cut(src, start, end, dst, **kw):
            seen_dirs.append(Path(dst).parent)
            return await original(src, start, end, dst, **kw)
        with mock.patch.object(mv_audio, "cut", spying_cut):
            await D.listen_song(FakeLlm(audio=[heard_reply()]), self.song, 20)
        self.assertTrue(seen_dirs and not seen_dirs[0].exists())                         # 切り出した WAV の置き場は、終わったら消える
        self.assertEqual(self.song.read_bytes(), b"ID3fake")


class ProposeConceptTest(unittest.IsolatedAsyncioTestCase):
    INFO = {**heard_reply(), "bpm": 72, "lyrics": "ひみつの歌詞 SECRET-LYRICS-XYZ"}

    async def test_prompt_has_the_music_facts_but_never_the_lyrics(self):
        llm = FakeLlm(texts=["切ないピアノバラード。雨の窓辺で過去を想う。セピアの色調で、孤独から安らぎへ。"])
        text = await D.propose_concept(llm, self.INFO, {"uploaded": True, "lyrics": "STUDIO-LYRICS"})
        self.assertEqual(text, "切ないピアノバラード。雨の窓辺で過去を想う。セピアの色調で、孤独から安らぎへ。")
        (call,) = llm.chats
        prompt = call["system"] + call["user"]
        for word in ("Genre: J-Pop", "Mood: Melancholy, nostalgic", "Instruments: Piano, strings", "Tempo and key: medium / 72 BPM", "Vocal language: Japanese",
                     "Vocals: female", "JAPANESE", "2 to 3 short sentences", "guessed by listening to short excerpts"):
            self.assertIn(word, prompt)
        for secret in ("SECRET-LYRICS-XYZ", "STUDIO-LYRICS", "ひみつの歌詞"):
            self.assertNotIn(secret, prompt)                                     # 歌詞は、聞き取りにも台帳にもあっても、案の材料にしない
        self.assertIn("Do not quote or invent lyrics", call["user"])
        self.assertIn("Do not mention the singer's gender or looks", call["user"])      # 人物は画像で決まる

    async def test_bpm_comes_from_the_analysis_only_when_there_is_one(self):
        llm = FakeLlm(texts=["切ない曲。", "切ない曲。"])
        await D.propose_concept(llm, heard_reply(), {"uploaded": True})
        self.assertIn("- Tempo and key: medium\n", llm.chats[0]["user"])                 # BPM の分析がまだなら、BPM は書かない
        self.assertNotIn("medium /", llm.chats[0]["user"])
        await D.propose_concept(llm, {**heard_reply(), "bpm": 118.5}, {})
        self.assertIn("medium / 118.5 BPM", llm.chats[1]["user"])

    async def test_studio_song_params_fill_what_listening_did_not_say(self):
        params = {"theme": "夜行バスの朝焼け", "caption": "J-pop with bright guitars", "genre": "J-POP", "language": "Japanese", "bpm": 120,
                  "key_scale": "A major", "mood": "bright and happy", "vocal": "female vocal", "instrumental": False, "lyrics": "歌詞はここ"}
        llm = FakeLlm(texts=["明るい J-POP。"])
        await D.propose_concept(llm, {}, params)
        user = llm.chats[0]["user"]
        for word in ("Theme given by the creator: 夜行バスの朝焼け", "J-pop with bright guitars", "Genre: J-POP", "Mood: bright and happy",
                     "120 BPM / key A major", "Vocal language: Japanese", "Vocals: female vocal"):
            self.assertIn(word, user)
        self.assertNotIn("歌詞はここ", user)
        self.assertNotIn("Instrumental", user)
        self.assertNotIn("guessed by listening", user)                           # 台帳の情報だけなら、「聞いて当てた」とは書かない
        llm = FakeLlm(texts=["インストの曲。"])
        await D.propose_concept(llm, {"genre": "heard genre", "mood": "heard mood", "bpm": 90}, {"genre": "params genre", "mood": "params mood", "bpm": 120, "instrumental": True})
        user = llm.chats[0]["user"]
        self.assertIn("Genre: heard genre", user)                                # 聞いた結果が先
        self.assertIn("Mood: heard mood", user)
        self.assertIn("90 BPM", user)
        self.assertIn("Instrumental: yes (no vocals)", user)

    async def test_answer_is_cleaned_and_kept_to_japanese(self):
        llm = FakeLlm(texts=["NOTE: 「明るい曲。\n夜明けの街を走る。」"])
        self.assertEqual(await D.propose_concept(llm, self.INFO), "明るい曲。夜明けの街を走る。")
        llm = FakeLlm(texts=["A bright song about dawn.", "明るい曲。"])         # 英語で答えたら、日本語で答えるよう足して 1 回だけ頼み直す
        self.assertEqual(await D.propose_concept(llm, self.INFO), "明るい曲。")
        self.assertNotIn(D.CONCEPT_RETRY, llm.chats[0]["user"])
        self.assertIn(D.CONCEPT_RETRY, llm.chats[1]["user"])
        with self.assertRaises(LlmError) as cm:
            await D.propose_concept(FakeLlm(texts=["English only.", "Still English."]), self.INFO)
        self.assertIn("日本語で答えませんでした", cm.exception.message)
        long = await D.propose_concept(FakeLlm(texts=["あ。" * 2000]), self.INFO)
        self.assertLessEqual(len(long), 1000)

    async def test_nothing_to_go_on_is_an_error_without_calling_the_llm(self):
        llm = FakeLlm()
        for info, params in (({}, None), ({"language": ""}, {"uploaded": True}), ({"bpm": None, "genre": "  "}, {})):
            with self.assertRaises(LlmError) as cm:
                await D.propose_concept(llm, info, params)
            self.assertIn("曲の情報が無い", cm.exception.message)
        self.assertEqual(llm.chats, [])
        with self.assertRaises(LlmError):                                        # LLM が止まっていれば、その LlmError
            await D.propose_concept(FakeLlm(texts=[LlmError("止まっています")]), self.INFO)


if __name__ == "__main__":
    unittest.main()



class ListenWordsTest(unittest.TestCase):
    """聞き取りの答えの語の切り分け: 「…, and wistful」の and を落とし、R&B や Rock and Roll は切らない。"""

    def test_words(self):
        self.assertEqual(D._words("Melancholy, nostalgic, and wistful"), ["Melancholy", "nostalgic", "wistful"])
        self.assertEqual(D._words("Synthesizers, drums, bass, and electric guitar"), ["Synthesizers", "drums", "bass", "electric guitar"])
        self.assertEqual(D._words("R&B"), ["R&B"])
        self.assertEqual(D._words("Rock and Roll"), ["Rock and Roll"])
        self.assertEqual(D._words(["Pop", "Rock"]), ["Pop", "Rock"])
