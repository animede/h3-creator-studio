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


class SingleLineTest(unittest.TestCase):
    def test_text_and_objects(self):
        self.assertEqual(M._single_line(" a  quiet\n street. "), "a quiet street")
        self.assertEqual((M._single_line(None), M._single_line(0), M._single_line(5)), ("", "", "5"))
        # 文字列のはずがオブジェクト・配列で返ってきたら、中の値をつなぐ(Python の表記をプロンプトに入れない)
        self.assertEqual(M._single_line({"color": "black.", "length": ["short", ""]}), "black; short")
        self.assertEqual((M._single_line({}), M._single_line([])), ("", ""))


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


class NonSingingShotTest(unittest.IsolatedAsyncioTestCase):
    """歌わないシーン(前奏・間奏・後奏)の構図を順に変える(2026-11-27: 前奏が参照画像の正面のポーズに寄り、話しているように見えた)。"""

    def test_shots_rotate_over_non_singing_scenes_and_the_last_one_walks_away(self):
        scenes = [scene(1, 0.0), scene(2, 0.1), scene(3, 0.9), scene(4, 0.2), scene(5, 0.0), scene(6, 0.0), scene(7, 0.8), scene(8, 0.0)]
        shots = M.non_singing_shots(scenes)
        self.assertEqual(sorted(shots), [1, 2, 4, 5, 6, 8])                       # 歌うシーン(3, 7)には構図を足さない
        self.assertEqual([shots[i] for i in (1, 2, 4, 5, 6)], [*M.NON_SINGING_SHOTS, M.NON_SINGING_SHOTS[0]])
        self.assertTrue(shots[1].startswith("Wide establishing shot"))           # 前奏の最初は引きの画
        self.assertEqual(shots[8], M.NON_SINGING_LAST_SHOT)                       # 曲の最後(後奏)は去っていく後ろ姿
        self.assertEqual(M.non_singing_shots([scene(1, 0.9), scene(2, 0.9)]), {})
        self.assertEqual(M.non_singing_shots([scene(1, 0.0)]), {1: M.NON_SINGING_SHOTS[0]})     # 1 シーンだけなら「最後」にしない

    def test_shot_leads_and_the_llm_shot_size_is_neutralised(self):
        out = M.enforce_reference_and_lipsync(scene(1, 0.0), "Medium shot of her walking; a close-up of the lanterns.",
                                              {"subject": "a woman"}, shot=M.NON_SINGING_SHOTS[0])
        self.assertTrue(out.startswith(M.NON_SINGING_SHOTS[0] + " Use the exact same person"))
        self.assertIn("keeps a naturally closed, relaxed mouth. Scene framing of her walking; a scene framing of the lanterns.", out)
        self.assertNotIn("Medium shot", out)
        # 引き・全身の語は構図と食い違わないので残す(LLM が framing に合わせて書いた語)
        wide = M.enforce_reference_and_lipsync(scene(1, 0.0), "Wide shot of the whole street, full-body.", {}, shot=M.NON_SINGING_SHOTS[0])
        self.assertTrue(wide.endswith("Wide shot of the whole street, full-body."))
        # 歌うシーンには shot を渡しても足さない(口の見える構図の規則のまま)
        sung = M.enforce_reference_and_lipsync(scene(1, 0.9), "She sings.", {}, shot=M.NON_SINGING_SHOTS[0])
        self.assertNotIn("Wide establishing", sung)
        self.assertTrue(sung.startswith("Medium close-up performance shot"))

    def test_portrait_prefix_of_a_non_singing_scene_is_not_head_and_shoulders(self):
        quiet = M.format_h3_official_ref2va_prompt(scene(1, 0.0), "dir", {}, frame="portrait")
        self.assertIn("[Shot 1] Vertical frame composition: <Subject 1> stays horizontally centered", quiet)
        self.assertNotIn("head and", quiet)
        sung = M.format_h3_official_ref2va_prompt(scene(1, 0.9), "dir", {}, frame="portrait")
        self.assertIn("head and shoulders in the upper-middle", sung)
        self.assertIn("[Shot 1] Square frame composition", M.format_h3_official_ref2va_prompt(scene(1, 0.0), "dir", {}, frame="square"))

    async def test_generate_scenario_puts_the_shots_in_the_prompts(self):
        scenes = [scene(1, 0.0), scene(2, 0.0), scene(3, 0.9), scene(4, 0.0)]
        chat = FakeChat([OVERVIEW, items([1, 2, 3, 4], prompt="She walks")])
        result = await M.generate_scenario(chat, scenes, "", {"subject": "a woman"})
        batch = chat.calls[1]["user"]
        self.assertIn("Such scenes come with a framing: write the prompt around it", batch)
        briefs = json.loads(re.search(r"SCENES: (\[.*\])\nReturn exactly", batch).group(1))
        self.assertTrue(briefs[0]["framing"].startswith("Wide establishing shot of the whole place: the character appears small"))
        self.assertNotIn("framing", briefs[2])                                    # 歌うシーンには構図を渡さない
        self.assertIn('"framing": "Wide establishing shot', chat.calls[0]["user"])  # 全体の構成の依頼にも見える
        prompts = [it["prompt"] for it in result["scenes"]]
        self.assertIn("[Shot 1] Wide establishing shot of the whole place:", prompts[0])
        self.assertIn("[Shot 1] Side tracking shot:", prompts[1])
        self.assertIn("[Shot 1] Medium close-up performance shot", prompts[2])
        self.assertIn("[Shot 1] Closing wide shot from behind:", prompts[3])
        # 歌わせない(イメージ MV)は、どんな構図も LLM に任せるので足さない
        chat = FakeChat([OVERVIEW, items([1, 2])])
        result = await M.generate_scenario(chat, scenes[:2], "", {}, lipsync=False)
        self.assertFalse(any(shot in it["prompt"] for it in result["scenes"] for shot in M.NON_SINGING_SHOTS))


class OfficialGuideTest(unittest.IsolatedAsyncioTestCase):
    """公式の手引き(ref-en.txt: ショットごとに具体的に・350〜500 語)と SKILL.md の Tips(抽象語より具体的な描写)に合わせた部分。"""

    def test_overview_look(self):
        look = M.overview_look({"visual_theme": "Winter dusk street, warm\nlanterns. Second sentence is dropped.",
                                "color_script": "Indigo to yuzu yellow."})
        self.assertEqual(look, "The target video is a music video that fully preserves the reference character, with one consistent "
                               "look: Winter dusk street, warm lanterns. Color palette: Indigo to yuzu yellow.")
        self.assertEqual(M.overview_look({"visual_theme": "", "color_script": ""}), "")
        self.assertEqual(M.overview_look({"color_script": "deep indigo"}),
                         "The target video is a music video that fully preserves the reference character, with one consistent look: deep indigo.")
        self.assertEqual(M.overview_look(None), "")
        long = M.overview_look({"visual_theme": " ".join(["word"] * 28) + " textures, damp stones, and lights everywhere"})
        self.assertIn("word textures, damp", long)                     # 30 語で切り、終わりの「,」「and」は落とす
        self.assertTrue(long.endswith("damp."), long)

    def test_detail_rule_and_sound(self):
        self.assertIn("about 120-180 English words", M.detail_rule())
        self.assertIn("Do not describe sounds or music", M.detail_rule())
        self.assertIn("the sounds of the moment", M.detail_rule(sound=True))
        self.assertNotIn("Do not describe sounds", M.detail_rule(sound=True))
        self.assertIn("over abstract words such as cinematic", M.detail_rule(sound=True))

    async def test_scenario_uses_the_look_and_asks_for_detail(self):
        overview = {**OVERVIEW, "visual_theme": "winter dusk street", "color_script": "indigo to yuzu yellow"}
        for lipsync in (True, False):
            chat = FakeChat([overview, items([1])])
            result = await M.generate_scenario(chat, [scene(1)], "", {"subject": "a woman"}, lipsync=lipsync)
            self.assertIn(M.detail_rule(sound=False), chat.calls[1]["user"])
            text = result["scenes"][0]["prompt"]
            self.assertIn("detailed_description:\nThe target video is a music video that fully preserves the reference character, "
                          "with one consistent look: winter dusk street. Color palette: indigo to yuzu yellow. It contains", text)
            self.assertNotIn(M.DEFAULT_STYLE_SENTENCE, text)
        self.assertIn("ONE concrete sentence of at most 25 words", chat.calls[0]["user"])     # 全体の構成の依頼にも
        chat = FakeChat([overview, items([1])])                       # 画風を選んだら、画風の文が優先
        result = await M.generate_scenario(chat, [scene(1)], "", {}, style="anime")
        self.assertNotIn("one consistent look", result["scenes"][0]["prompt"])


class RebuildTest(unittest.TestCase):
    """やり直しで演出だけを書き換えたときの組み立て直し(台本と同じ規則)と、全文を書き換えたときの検査。"""
    PARAMS = {"character_facts": {"subject": "a woman"}, "script": {"visual_theme": "dusk"}, "width": 1024, "height": 576,
              "lipsync": True, "style": "none"}
    SCENES = [scene(1, 0.0), scene(2, 0.0), scene(3, 0.9)]

    def test_non_singing_scene_gets_the_rotation_shot_unless_the_direction_names_one(self):
        plain = M.rebuild_scene_prompt(self.PARAMS, self.SCENES, 2, "She pauses by a window")
        self.assertIn("[Shot 1] " + M.NON_SINGING_SHOTS[1], plain)              # 台本と同じ順の構図(2 つ目の歌わないシーン)
        named = M.rebuild_scene_prompt(self.PARAMS, self.SCENES, 2, "Close-up of her gloved hand on the window glass")
        self.assertNotIn("Side tracking shot", named)
        self.assertIn("Close-up of her gloved hand on the window glass", named)   # 人が決めた構図は中立にしない
        self.assertIn("keeps a naturally closed, relaxed mouth", named)
        sung = M.rebuild_scene_prompt(self.PARAMS, self.SCENES, 3, "Wide shot from behind as she sings")
        self.assertIn("Medium close-up performance shot", sung)                   # 歌うシーンは口が見える構図のまま
        self.assertNotIn("from behind", sung)
        self.assertIn("one consistent look: dusk.", sung)
        image_mv = M.rebuild_scene_prompt({**self.PARAMS, "lipsync": False}, self.SCENES, 1, "She walks")
        self.assertNotIn("<Audio 1>", image_mv)
        self.assertNotIn("Wide establishing", image_mv)
        self.assertTrue(M.names_framing("a low-angle view"))
        self.assertFalse(M.names_framing("she walks under the lanterns"))

    def test_prompt_problems(self):
        original = M.scene_prompt(scene(1, 0.9), "She sings", {"subject": "a woman"})
        self.assertEqual(M.prompt_problems(original, original.replace("She sings", "She sings softly")), [])
        no_audio = original.replace("<Audio 1>", "the audio")
        self.assertIn("reference labels must stay", " ".join(M.prompt_problems(original, no_audio)))
        no_summary = original.replace("summary:\n", "")
        self.assertIn("sections must stay", " ".join(M.prompt_problems(original, no_summary)))
        two_shots = original.replace("[Shot 1] ", "[Shot 1] At 00:02.000, ") + "\n[Shot 1] more"
        problems = " ".join(M.prompt_problems(original, two_shots))
        self.assertIn("exactly one [Shot 1]", problems)
        self.assertIn("must not have a timestamp", problems)


class ImageMvTest(unittest.IsolatedAsyncioTestCase):
    """歌わせない(lipsync=False): 歌の割合を見ず、音声の参照が無い形にする。"""

    def test_enforce_ignores_vocal_ratio_and_keeps_wide_shots(self):
        out = M.enforce_reference_and_lipsync(scene(1, 0.9), "Wide shot from behind on the beach", {"subject": "a girl"}, lipsync=False)
        self.assertTrue(out.startswith("Use the exact same person from the reference image;"))
        self.assertIn("does not sing or mouth words", out)
        self.assertTrue(out.endswith("Wide shot from behind on the beach"))          # 口を隠す構図の書き換えはしない
        self.assertNotIn("articulate every phoneme", out)

    def test_format_has_no_audio_reference(self):
        text = M.format_h3_official_ref2va_prompt(scene(1, 0.9), "She walks along the shore.", {"subject": "a girl"}, frame="portrait", lipsync=False)
        self.assertNotIn("<Audio 1>", text)
        self.assertNotIn("Vocal", text)
        for section in ("subject_definitions:", "summary:", "retention_analysis:", "detailed_description:", "overall_soundscape:", "non_diegetic_music:"):
            self.assertIn(section, text)
        self.assertIn("[reference generation] ", text)
        self.assertIn("without singing or speaking", text)
        self.assertIn("Vertical frame composition", text)
        self.assertIn("She walks along the shore <Subject 1> (S1) does not sing", text)      # 末尾の . は _single_line が落とす(歌わせる形と同じ)
        self.assertIn("No dialogue, singing, or vocals", text)

    async def test_generate_scenario_tells_the_llm_not_to_sing(self):
        chat = FakeChat([OVERVIEW, items([1, 2])])
        result = await M.generate_scenario(chat, [scene(1, 0.9), scene(2, 0.1)], "", {}, lipsync=False)
        self.assertIn("image music video in which the character never sings on camera", chat.calls[0]["user"])
        batch = chat.calls[1]["user"]
        self.assertIn("NEVER sings, speaks or mouths words", batch)
        self.assertNotIn("phoneme-level lip articulation", batch)
        self.assertIn("image MV, no audio reference", result["prompt_format"])
        for it in result["scenes"]:
            self.assertNotIn("<Audio 1>", it["prompt"])
            self.assertIn("does not sing or speak", it["prompt"])


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


class NoOverlayTextTest(unittest.IsolatedAsyncioTestCase):
    """MV のシーンに字幕・テロップ・歌詞の文字を出さない(H3 が MV らしい場面に字幕を描くことがあった)。"""

    def test_every_scene_prompt_asks_for_a_clean_picture(self):
        for ratio in (0.9, 0.0):
            out = M.format_h3_official_ref2va_prompt(scene(1, ratio), "Medium shot under neon lights.", {"subject": "a girl"})
            self.assertIn(M.NO_OVERLAY_TEXT, out)
            self.assertIn("No text overlay.", out)
            self.assertNotIn("No lyrics are written", out)          # 「歌詞を書く」を連想させる言い回しは使わない

    def test_overlay_sentences_from_the_llm_are_removed(self):
        text = ("Close-up of the singer. Subtitles appear at the bottom with the lyrics. Warm light. "
                "The lyrics text scrolls across. Karaoke text fades in! A title card reads LOVE. Neon city behind her.")
        self.assertEqual(M.scrub_overlay_text(text), "Close-up of the singer. Warm light. Neon city behind her.")
        out = M.format_h3_official_ref2va_prompt(scene(1, 0.9), "Close-up. Subtitles appear at the bottom. Warm light.", {"subject": "a girl"})
        self.assertNotIn("Subtitles appear", out)
        self.assertIn("[Shot 1] Close-up. Warm light ", out)                  # 文末の「.」は移植元どおり落ちる

    async def test_the_scene_llm_is_told_not_to_write_on_screen_text(self):
        chat = FakeChat([OVERVIEW, items([1, 2])])
        await M.generate_scenario(chat, [scene(1, 0.9), scene(2, 0.0)], "", {"subject": "a girl"})
        self.assertIn(M.NO_OVERLAY_RULE.strip(), chat.calls[1]["user"])
