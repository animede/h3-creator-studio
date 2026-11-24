import json
import re
import unittest

from studio import mvscript as M
from studio.llm import LlmError


class FakeChat:
    """Llm.chat の代わり。replies を順に返す(Exception なら投げる)。呼ばれた中身を calls に残す。"""

    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    async def __call__(self, system, user, max_tokens=0, temperature=0):
        self.calls.append({"system": system, "user": user, "max_tokens": max_tokens, "temperature": temperature})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, str) else json.dumps(reply)


OVERVIEW = {"title": "夜明けの MV", "visual_theme": "city dawn", "color_script": "blue to gold", "story_arc": "arc", "continuity_rules": ["same coat"]}


def scene(i, ratio=0.8, **kw):
    return {"index": i, "start": (i - 1) * 8.0, "end": i * 8.0, "duration": 8.0, "vocal_ratio": ratio, "boundary_kind": "bar", **kw}


def items(indexes, prompt="Wide shot of the singer"):
    return [{"index": i, "emotion": f"emo{i}", "shot": "close", "camera": "static", "prompt": f"{prompt} {i}"} for i in indexes]


def expected_of(user: str) -> list:
    """batch の依頼文から、頼まれた番号の並びを読む。"""
    return json.loads(re.search(r"with indexes (\[[^\]]*\])", user).group(1))


class ExtractJsonTest(unittest.TestCase):
    def test_plain_fenced_and_with_prose(self):
        self.assertEqual(M.extract_json('{"a": 1}'), {"a": 1})
        self.assertEqual(M.extract_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(M.extract_json('```\n[{"index": 1}]\n```'), [{"index": 1}])
        self.assertEqual(M.extract_json('Sure, here you go:\n{"a": [1, 2]}\nHope it helps!'), {"a": [1, 2]})

    def test_skips_small_json_in_the_preface(self):
        """前置きの [1, 2, 3] や {} ではなく、後ろの本体(オブジェクトの配列)を取る。"""
        text = 'Here are the 3 scenes [1, 2, 3] and an empty {} object:\n```json\n[{"index": 1, "prompt": "x"}]\n```'
        self.assertEqual(M.extract_json(text), [{"index": 1, "prompt": "x"}])
        self.assertEqual(M.extract_json("only [1, 2, 3] here"), [1, 2, 3])      # 他に無ければ、最初に読めた値

    def test_errors(self):
        for text, word in (("", "JSON がありません"), ("no json at all", "JSON がありません"), ('{"a": 1', "読めません"), ('[{"index": 1,', "読めません")):
            with self.assertRaises(M.ScenarioError, msg=text) as cm:
                M.extract_json(text)
            self.assertIn(word, cm.exception.message)


class FrameTest(unittest.TestCase):
    def test_frame_kind_from_size(self):
        for size, kind in (((768, 448), "landscape"), ((1344, 768), "landscape"), ((448, 768), "portrait"), ((576, 1024), "portrait"),
                           ((512, 640), "portrait"), ((768, 960), "portrait"), ((512, 512), "square"), ((960, 960), "square"),
                           ((576, 640), "square")):      # 0.9 ちょうどは正方形扱い
            self.assertEqual(M.frame_kind(*size), kind, size)


class FormatTest(unittest.TestCase):
    SECTIONS = ["subject_definitions:", "summary:", "retention_analysis:", "detailed_description:", "overall_soundscape:", "non_diegetic_music:"]

    def test_six_sections_in_order(self):
        text = M.format_h3_official_ref2va_prompt(scene(1), "A neon street, slow dolly.", {"subject": "a girl with silver hair"})
        positions = [text.index(s) for s in self.SECTIONS]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(text.count("\n[Shot 1] "), 1)           # 描画のタイムラインは、タイムスタンプなしの [Shot 1] が 1 つだけ
        self.assertIn("<Subject 1> is the exact character shown in <Picture 1>: a girl with silver hair.", text)
        self.assertIn("<Audio 1>: fully_copy", text)
        self.assertIn("retained exactly: a girl with silver hair.", text)
        self.assertIn("[Shot 1] A neon street, slow dolly <Subject 1> (S1) physically performs", text)      # 演出の文末のピリオドは落とす
        self.assertTrue(text.endswith("non_diegetic_music:\nN/A"))

    def test_singing_vs_non_singing_by_vocal_ratio(self):
        def fmt(ratio):
            return M.format_h3_official_ref2va_prompt(scene(1, ratio), "dir", {})
        for ratio in (0.35, 0.36, 1.0):
            text = fmt(ratio)
            self.assertIn("physically performs the supplied vocal", text, ratio)
            self.assertIn("performs the supplied vocal with clearly visible synchronized lip movement", text)
            self.assertNotIn("closed, relaxed mouth", text)
        for ratio in (0.0, 0.2, 0.349):
            text = fmt(ratio)
            self.assertIn("keeps a naturally closed, relaxed mouth", text, ratio)
            self.assertIn("during the non-vocal interval", text)
            self.assertNotIn("physically performs", text)
        self.assertIn(M.DEFAULT_SUBJECT, fmt(0.9))          # 人物の説明が無いときは、中立の文
        self.assertIn("retained exactly: the exact same person and appearance shown in the reference image.", fmt(0.9))

    def test_portrait_and_square_prefixes(self):
        for frame, word in (("portrait", "Vertical frame composition"), ("square", "Square frame composition")):
            text = M.format_h3_official_ref2va_prompt(scene(1), "the direction", {}, frame=frame)
            self.assertIn(f"[Shot 1] {word}", text)
            self.assertLess(text.index(word), text.index("the direction"))      # 構図の文が、文頭(演出より前)
        landscape = M.format_h3_official_ref2va_prompt(scene(1), "the direction", {}, frame="landscape")
        self.assertIn("[Shot 1] the direction", landscape)
        self.assertNotIn("frame composition", landscape)

    def test_identity_anchor_wins_over_facts(self):
        ch = {"subject": "a girl", "hair": "silver", "identity_anchor": "Keep exactly this woman"}
        self.assertEqual(M.identity_instruction(ch), "Keep exactly this woman")
        self.assertEqual(M.identity_instruction({"subject": "a girl", "hair": "silver"}), "a girl, silver")
        self.assertEqual(M.identity_instruction({}), "the exact same person and appearance shown in the reference image")
        self.assertEqual(M.subject_definition({"subject": "a girl.", "hair": "silver\nhair"}), "a girl, silver hair")


class EnforceTest(unittest.TestCase):
    def test_singing_scene_loses_mouth_hiding_directions(self):
        raw = "Wide shot from behind, profile shot, over-the-shoulder shot, extreme close-up of the eyes, medium shot, she tilts her head back."
        out = M.enforce_reference_and_lipsync(scene(1, 0.9), raw, {"subject": "a girl"})
        head, tail = out.split("direct performance presence. ", 1)       # 頭書き(固定)と、LLM の演出の書き換え後
        self.assertTrue(head.startswith("Medium close-up performance shot, front-facing or three-quarter face view."))
        self.assertIn("preserve facial identity, hair, outfit, and visual style unchanged: a girl.", head)
        self.assertIn("lips and jaw articulate every phoneme", head)
        # 口を隠す構図は中立の語に置き換わり、ショットの大きさの語は頭書きに 1 つだけ残る
        self.assertEqual(tail, "performance framing from a front three-quarter angle, three-quarter face view, front three-quarter "
                               "performance framing, performance framing, performance framing, she keeps the face oriented toward the camera.")
        for gone in ("wide shot", "from behind", "profile", "over-the-shoulder", "the eyes", "medium shot", "medium close-up", "tilts"):
            self.assertNotIn(gone, tail.lower())

    def test_non_singing_scene_keeps_the_direction_and_asks_for_a_closed_mouth(self):
        out = M.enforce_reference_and_lipsync(scene(1, 0.1), "Wide shot of the empty stage", {"subject": "a girl"})
        self.assertTrue(out.startswith("Use the exact same person from the reference image;"))
        self.assertIn("keeps a naturally closed, relaxed mouth.", out)
        self.assertTrue(out.endswith("Wide shot of the empty stage"))


class ScenarioRequestTest(unittest.IsolatedAsyncioTestCase):
    async def test_valid_json_is_one_call(self):
        chat = FakeChat(['```json\n{"a": 1}\n```'])
        self.assertEqual(await M.scenario_request(chat, "PROMPT"), {"a": 1})
        self.assertEqual((len(chat.calls), chat.calls[0]["temperature"], chat.calls[0]["system"]), (1, 0.35, M.SYSTEM_PROMPT))

    async def test_one_repair_turn_then_success(self):
        chat = FakeChat(["I think the scenes are lovely but I forgot the JSON", '{"a": 2}'])
        with self.assertLogs("studio.mvscript", "WARNING"):
            self.assertEqual(await M.scenario_request(chat, "PROMPT"), {"a": 2})
        first, second = chat.calls
        self.assertTrue(second["user"].startswith("PROMPT") and second["user"].endswith(M.REPAIR_INSTRUCTION))     # 同じ依頼に、JSON だけで答え直す指示
        self.assertIn("Preserve the exact schema", second["user"])
        self.assertEqual(second["temperature"], 0.0)

    async def test_still_bad_after_the_repair_raises(self):
        chat = FakeChat(["nope", "still nope"])
        with self.assertRaises(M.ScenarioError), self.assertLogs("studio.mvscript", "WARNING"):
            await M.scenario_request(chat, "PROMPT")
        self.assertEqual(len(chat.calls), 2)         # 修復は 1 回だけ


class GenerateScenarioTest(unittest.IsolatedAsyncioTestCase):
    async def test_overview_then_batches_of_five(self):
        scenes = [scene(i, 0.9 if i % 2 else 0.0) for i in range(1, 8)]
        chat = FakeChat([OVERVIEW, items(range(1, 6)), items([6, 7])])
        progress = []
        result = await M.generate_scenario(chat, scenes, "a dawn story\nacross two lines", {"subject": "a girl"}, frame="portrait",
                                           on_progress=lambda done, total: progress.append((done, total)))
        self.assertEqual(len(chat.calls), 3)
        overview_call, first, second = (c["user"] for c in chat.calls)
        self.assertIn("USER_CONCEPT: a dawn story across two lines", overview_call)            # 改行はつぶす
        self.assertIn('CHARACTER_REFERENCE_FACTS: {"subject": "a girl"}', overview_call)
        self.assertEqual((expected_of(first), expected_of(second)), ([1, 2, 3, 4, 5], [6, 7]))
        self.assertIn("PREVIOUS: null", first)
        self.assertIn("VERTICAL frame", first)                    # 縦長の指示
        self.assertIn('TREATMENT: {"title": "夜明けの MV"', first)
        self.assertEqual(progress, [(5, 7), (7, 7)])
        self.assertEqual([it["index"] for it in result["scenes"]], list(range(1, 8)))
        self.assertEqual(result["overview"], OVERVIEW)
        # 次の batch に渡す PREVIOUS は、整形前の項目(6 セクションの定型文ではない)
        previous = json.loads(re.search(r"PREVIOUS: (\{.*\})\nSCENES", second).group(1))
        self.assertEqual((previous["index"], previous["prompt"]), (5, "Wide shot of the singer 5"))
        for sc, it in zip(scenes, result["scenes"]):
            self.assertIn("subject_definitions:", it["prompt"])
            self.assertEqual("physically performs the supplied vocal" in it["prompt"], sc["vocal_ratio"] >= 0.35, sc["index"])
            self.assertIn("Vertical frame composition", it["prompt"])

    async def test_landscape_has_no_frame_guidance_and_background_text_rules_apply(self):
        chat = FakeChat([OVERVIEW, items([1])])
        await M.generate_scenario(chat, [scene(1)], "", {}, frame="landscape", background_text="none")
        self.assertIn("USER_CONCEPT: (derive from the music and character)", chat.calls[0]["user"])
        batch = chat.calls[1]["user"]
        self.assertNotIn("VERTICAL frame", batch)
        self.assertNotIn("SQUARE", batch)
        self.assertIn("the scene contains no readable letters or words", batch)

    async def test_wrong_indexes_get_one_correction_then_succeed(self):
        chat = FakeChat([OVERVIEW, items([0, 1, 2]), items([1, 2, 3])])
        result = await M.generate_scenario(chat, [scene(1), scene(2), scene(3)], "", {})
        self.assertEqual(len(chat.calls), 3)
        self.assertNotIn("CORRECTION", chat.calls[1]["user"])
        self.assertTrue(chat.calls[2]["user"].startswith(chat.calls[1]["user"]))
        self.assertIn("CORRECTION: The previous answer had the wrong item count or indexes. Return exactly 3 items in this order: [1, 2, 3]", chat.calls[2]["user"])
        self.assertEqual([it["index"] for it in result["scenes"]], [1, 2, 3])

    async def test_wrong_count_twice_raises(self):
        chat = FakeChat([OVERVIEW, items([1]), items([1, 2])])
        with self.assertRaises(M.ScenarioError) as cm:
            await M.generate_scenario(chat, [scene(1), scene(2), scene(3)], "", {})
        self.assertIn("演出の数が合いません", cm.exception.message)
        self.assertIn("期待 3 個", cm.exception.message)

    async def test_right_count_wrong_indexes_twice_raises(self):
        chat = FakeChat([OVERVIEW, items([4, 5]), items([1, 3])])
        with self.assertRaises(M.ScenarioError) as cm:
            await M.generate_scenario(chat, [scene(1), scene(2)], "", {})
        self.assertIn("番号が合いません", cm.exception.message)
        self.assertIn("[1, 2]", cm.exception.message)
        self.assertIn("[1, 3]", cm.exception.message)

    async def test_json_repair_inside_a_batch(self):
        chat = FakeChat([OVERVIEW, "Here you go! (no json)", items([1, 2])])
        with self.assertLogs("studio.mvscript", "WARNING"):
            result = await M.generate_scenario(chat, [scene(1), scene(2)], "", {})
        self.assertEqual(len(chat.calls), 3)
        self.assertEqual(chat.calls[2]["temperature"], 0.0)
        self.assertEqual(len(result["scenes"]), 2)

    async def test_tolerates_wrapped_and_single_item_answers(self):
        chat = FakeChat([OVERVIEW, {"scenes": items([1, 2])}])               # 配列の代わりに {"scenes": [...]}
        result = await M.generate_scenario(chat, [scene(1), scene(2)], "", {})
        self.assertEqual((len(chat.calls), [it["index"] for it in result["scenes"]]), (2, [1, 2]))
        chat = FakeChat([OVERVIEW, items(range(1, 6)), items([6])[0]])        # 6 つめだけの batch が、配列に入らず項目そのものでも読む
        result = await M.generate_scenario(chat, [scene(i) for i in range(1, 7)], "", {})
        self.assertEqual((len(chat.calls), [it["index"] for it in result["scenes"]]), (3, [1, 2, 3, 4, 5, 6]))

    async def test_unreadable_overview_does_not_stop_the_run(self):
        chat = FakeChat(["prose only", "more prose", items([1])])
        with self.assertLogs("studio.mvscript", "WARNING"):
            result = await M.generate_scenario(chat, [scene(1)], "", {})
        self.assertEqual(result["overview"], {})
        self.assertIn("TREATMENT: {}", chat.calls[2]["user"])

    async def test_llm_errors_pass_through(self):
        chat = FakeChat([LlmError("llama-server を起動できません")])
        with self.assertRaises(LlmError):
            await M.generate_scenario(chat, [scene(1)], "", {})


if __name__ == "__main__":
    unittest.main()


class CharacterFactsTest(unittest.IsolatedAsyncioTestCase):
    """人物の説明から、見た目だけを抜き出す(場所・光・ポーズを人物の定義に入れない)。"""

    async def test_extracts_appearance_fields(self):
        calls = []

        async def chat(system, user, max_tokens=0, temperature=0):
            calls.append(user)
            return '```json\n{"subject": "a young anime-style girl", "face": "brown eyes", "hair": "short brown hair", ' \
                   '"outfit": "white blouse, dark vest", "style": "anime illustration", "location": "window"}\n```'
        facts = await M.character_facts(chat, "A vibrant anime-style illustration of a young girl standing by a sunlit window")
        self.assertEqual(facts, {"subject": "a young anime-style girl", "face": "brown eyes", "hair": "short brown hair",
                                 "outfit": "white blouse, dark vest", "style": "anime illustration"})      # 余計な鍵は捨てる
        self.assertIn("DESCRIPTION: A vibrant anime-style illustration", calls[0])
        self.assertIn("Leave out expression, emotion, location", calls[0])

    async def test_unreadable_answer_falls_back_to_the_description(self):
        async def chat(system, user, max_tokens=0, temperature=0):
            return "sorry, I cannot"
        self.assertEqual(await M.character_facts(chat, "a girl in a red coat"), {"subject": "a girl in a red coat"})

    async def test_empty_description_does_not_call_the_llm(self):
        async def chat(system, user, max_tokens=0, temperature=0):
            raise AssertionError("呼ばれないはず")
        self.assertEqual(await M.character_facts(chat, ""), {"subject": M.DEFAULT_SUBJECT})
        self.assertEqual(await M.character_facts(chat, M.DEFAULT_SUBJECT), {"subject": M.DEFAULT_SUBJECT})
