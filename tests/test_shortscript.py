import json
import re
import unittest

from studio import mvscript as M
from studio import shortscript as S
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


OUTLINE = {"title": "雨の駅", "setting": "a rainy small station", "characters": "a girl", "arc": "waiting, meeting, parting",
           "places": ["platform", "cafe"], "continuity_rules": ["same red umbrella", "night all through"]}
GIRL = {"subject": "a young girl", "hair": "short black hair", "outfit": "red coat"}
# params.images: 0 = person, 1 = station (scenery), 2 = cafe (scenery)
DESCS = ["a girl in a red coat", "a rainy station platform with a clock", "a small warm cafe interior"]
ROLES = ["person", "scenery", "scenery"]


def scene(i, **kw):
    return {"index": i, "start": (i - 1) * 8.0, "end": i * 8.0, "duration": 8.0, **kw}


def item(i, refs=(), prompt="A quiet shot", **kw):
    return {"index": i, "emotion": f"emo{i}", "place": f"place{i}", "refs": list(refs), "shot": "medium", "camera": "static",
            "prompt": f"{prompt} {i}", **kw}


def expected_of(user: str) -> list:
    return json.loads(re.search(r"with indexes (\[[^\]]*\])", user).group(1))


SECTIONS = ["subject_definitions:", "summary:", "retention_analysis:", "detailed_description:", "overall_soundscape:", "non_diegetic_music:"]


class StoryOutlineTest(unittest.IsolatedAsyncioTestCase):
    async def test_one_request_with_all_materials(self):
        chat = FakeChat([OUTLINE])
        out = await S.story_outline(chat, "a girl waits\nfor a train", song_concept="slow piano", narration="こんにちは。",
                                    character=GIRL, sceneries=["a station platform", ""])
        self.assertEqual(out, OUTLINE)
        self.assertEqual(len(chat.calls), 1)
        user = chat.calls[0]["user"]
        self.assertIn("STORY: a girl waits for a train", user)
        self.assertIn("SONG_CONCEPT (background music): slow piano", user)
        self.assertIn("NARRATION", user)
        self.assertIn('CHARACTER_REFERENCE_FACTS: {"subject": "a young girl"', user)
        self.assertIn("are immutable", user)
        self.assertIn('"n": 2, "description": "(no description)"', user)
        self.assertIn("a station platform", user)

    async def test_without_person_or_extras(self):
        chat = FakeChat([OUTLINE])
        await S.story_outline(chat, "", character={}, sceneries=[])
        user = chat.calls[0]["user"]
        self.assertIn("There is no reference character", user)
        self.assertNotIn("CHARACTER_REFERENCE_FACTS", user)
        self.assertNotIn("SCENERY_REFERENCES", user)
        self.assertNotIn("SONG_CONCEPT", user)
        self.assertNotIn("NARRATION", user)

    async def test_result_is_normalized(self):
        chat = FakeChat([{"title": " T\n", "setting": "s", "places": ["a", "", " b "], "continuity_rules": "not a list", "extra": 1}])
        out = await S.story_outline(chat, "x", character={})
        self.assertEqual(out, {"title": "T", "setting": "s", "characters": "", "arc": "", "places": ["a", "b"], "continuity_rules": []})

    async def test_objects_in_place_of_text_are_flattened(self):
        # 2026-11-26: places が {"scene_description": ...} のオブジェクトで返り、"{'scene_description': '...'}" のまま次のプロンプトに入っていた
        chat = FakeChat([{"title": "夜明けの光", "characters": [], "arc": {"from": "storm.", "to": "calm dawn."},
                          "places": [{"scene_description": "A wide shot of the lighthouse at dawn.\n"}, "the lantern room",
                                     {"name": "the coastline", "time": "morning"}, {}, ["harbor", ""]],
                          "continuity_rules": {"1": "same lighthouse", "2": "dawn colors"}}])
        out = await S.story_outline(chat, "x", character={})
        self.assertEqual(out, {"title": "夜明けの光", "setting": "", "characters": "", "arc": "storm; calm dawn",
                               "places": ["A wide shot of the lighthouse at dawn", "the lantern room", "the coastline; morning", "harbor"],
                               "continuity_rules": ["same lighthouse", "dawn colors"]})

    async def test_unreadable_or_non_object_falls_back_to_empty(self):
        chat = FakeChat(["prose", "more prose"])
        with self.assertLogs("studio.shortscript", "WARNING"), self.assertLogs("studio.mvscript", "WARNING"):
            self.assertEqual(await S.story_outline(chat, "x", character={}), {})
        for reply in ('[1, 2, 3]', '{}', '{"title": ""}'):
            chat = FakeChat([reply])
            self.assertEqual(await S.story_outline(chat, "x", character={}), {}, reply)

    async def test_llm_errors_pass_through(self):
        with self.assertRaises(LlmError):
            await S.story_outline(FakeChat([LlmError("down")]), "x", character={})


def plan_items(durations, place="a place"):
    return [{"duration": d, "place": f"{place}{i}", "summary": f"summary{i}"} for i, d in enumerate(durations)]


class PlanScenesTest(unittest.IsolatedAsyncioTestCase):
    def test_scene_count_is_feasible(self):
        for total, lo, hi in ((60, 5, 10), (45, 5, 10), (100, 5, 10), (33, 5, 10), (30, 5, 10), (300, 5, 10)):
            n = S._scene_count(total, lo, hi)
            self.assertTrue(n * lo <= total <= n * hi, (total, n))
        self.assertEqual(S._scene_count(60, 5, 10), 8)          # 真ん中(7.5 秒)に近い個数
        self.assertEqual(S._scene_count(3, 5, 10), 1)           # 満たせない長さは、最小の個数

    async def test_tells_the_llm_the_exact_count_and_total(self):
        chat = FakeChat([plan_items([7.5] * 8)])
        out = await S.plan_scenes(chat, OUTLINE, 60, 5, 10)
        user = chat.calls[0]["user"]
        self.assertIn("exactly 8 scenes", user)
        self.assertIn("between 5 and 10", user)
        self.assertIn("add up to 60", user)
        self.assertIn("Return exactly 8 array items", user)
        self.assertIn('TREATMENT: {"title": "雨の駅"', user)
        self.assertEqual(len(out), 8)
        self.assertEqual(sum(s["duration"] for s in out), 60)
        self.assertEqual(out[2], {"duration": 7.5, "place": "a place2", "summary": "summary2"})

    async def test_durations_are_clamped_then_rescaled_to_the_total(self):
        # 範囲外(3, 20)は丸められ、合計 60 になるよう残りに配られる
        chat = FakeChat([plan_items([3, 20, 7, 7, 7, 7, 7, 7])])
        out = await S.plan_scenes(chat, OUTLINE, 60, 5, 10)
        d = [s["duration"] for s in out]
        self.assertTrue(all(5 <= x <= 10 for x in d), d)
        self.assertAlmostEqual(sum(d), 60, delta=0.001 * len(d))
        self.assertEqual(d[1], 10.0)

    async def test_rescale_when_the_sum_is_off(self):
        chat = FakeChat([plan_items([6, 6, 6, 6, 6, 6, 6, 6])])        # 合計 48 → 60 へ比例して(7.5 ずつ)
        out = await S.plan_scenes(chat, OUTLINE, 60, 5, 10)
        self.assertEqual([s["duration"] for s in out], [7.5] * 8)
        chat = FakeChat([plan_items([9, 9, 9, 5, 5, 5, 5, 5])])        # 合計 52 → 60: 上限に当たる前の分に配る
        d = [s["duration"] for s in await S.plan_scenes(chat, OUTLINE, 60, 5, 10)]
        self.assertAlmostEqual(sum(d), 60, delta=0.008)
        self.assertTrue(all(5 <= x <= 10 for x in d), d)

    async def test_unreadable_duration_becomes_the_average(self):
        items_ = plan_items([7.5] * 8)
        items_[0]["duration"] = "long"
        items_[1].pop("duration")
        out = await S.plan_scenes(FakeChat([items_]), OUTLINE, 60, 5, 10)
        self.assertEqual([s["duration"] for s in out], [7.5] * 8)

    async def test_wrapped_answer_is_read(self):
        out = await S.plan_scenes(FakeChat([{"scenes": plan_items([10] * 4)}]), OUTLINE, 30, 5, 10)
        self.assertEqual([s["duration"] for s in out], [7.5] * 4)         # 30 秒 = 4 つ(真ん中の長さに近い個数)

    async def test_wrong_count_gets_one_correction(self):
        chat = FakeChat([plan_items([7.5] * 7), plan_items([7.5] * 8)])
        out = await S.plan_scenes(chat, OUTLINE, 60, 5, 10)
        self.assertEqual(len(out), 8)
        self.assertEqual(len(chat.calls), 2)
        self.assertTrue(chat.calls[1]["user"].startswith(chat.calls[0]["user"]))
        self.assertIn("CORRECTION: The previous answer did not have exactly 8 scene objects.", chat.calls[1]["user"])

    async def test_wrong_count_twice_raises(self):
        chat = FakeChat([plan_items([7.5] * 7), plan_items([7.5] * 9)])
        with self.assertRaises(S.ScenarioError) as cm:
            await S.plan_scenes(chat, OUTLINE, 60, 5, 10)
        self.assertIn("シーン割りの数が合いません", cm.exception.message)
        self.assertIn("期待 8 個", cm.exception.message)
        self.assertIs(S.ScenarioError, M.ScenarioError)

    async def test_non_object_items_count_as_wrong(self):
        chat = FakeChat([[1, 2, 3, 4, 5, 6, 7, 8], plan_items([7.5] * 8)])
        self.assertEqual(len(await S.plan_scenes(chat, OUTLINE, 60, 5, 10)), 8)
        self.assertEqual(len(chat.calls), 2)

    async def test_bad_arguments(self):
        with self.assertRaises(S.ScenarioError):
            await S.plan_scenes(FakeChat([]), OUTLINE, 0, 5, 10)
        with self.assertRaises(S.ScenarioError):
            await S.plan_scenes(FakeChat([]), OUTLINE, 60, 10, 5)

    def test_fit_durations_keeps_bounds_when_total_is_infeasible(self):
        self.assertEqual(S._fit_durations([5, 5], 40, 5, 10), [10.0, 10.0])      # 20 までしか作れない
        self.assertEqual(S._fit_durations([8, 8], 4, 5, 10), [5.0, 5.0])


class GenerateScenarioTest(unittest.IsolatedAsyncioTestCase):
    async def gen(self, chat, scenes, *, timeline, roles=ROLES, descs=DESCS, character=GIRL, frame="landscape", **kw):
        return await S.generate_scenario(chat, scenes, OUTLINE, character, descs, timeline=timeline, frame=frame,
                                         background_text=kw.pop("background_text", "auto"), roles=roles, **kw)

    async def test_speech_always_includes_the_person_first(self):
        chat = FakeChat([[item(1, refs=[2]), item(2, refs=[]), item(3, refs=[0, 1, 2, 1])]])
        result = await self.gen(chat, [scene(1), scene(2), scene(3)], timeline="speech")
        self.assertEqual([it["refs"] for it in result["scenes"]], [[0, 2], [0], [0, 1, 2]])       # 重複は 1 つ、person は先頭に 1 回
        self.assertEqual(len(chat.calls), 1)                                                      # 概要の追加の呼び出しは無い
        self.assertEqual(result["overview"], OUTLINE)
        batch = chat.calls[0]["user"]
        self.assertIn("NOT a performance - never a stage, concert, microphone, singing or audience", batch)       # 語りであって歌の演技ではない
        self.assertIn("(<Audio 1>)", batch)
        self.assertIn(M.NO_OVERLAY_RULE.strip(), batch)
        self.assertIn('CONTINUITY_RULES: ["same red umbrella", "night all through"]', batch)
        self.assertIn('TREATMENT: {"title": "雨の駅"', batch)
        self.assertIn("PREVIOUS: null", batch)
        self.assertNotIn("no_person", batch)                       # speech に「人物なし」は無い
        self.assertIn('"description": "a rainy station platform with a clock"', batch)
        self.assertNotIn("a girl in a red coat", batch.split("SCENERY_REFERENCES")[1].split("SCENES")[0])      # 人物の画像は景色の一覧に出さない
        for it in result["scenes"]:
            self.assertIn("<Audio 1>: fully_copy", it["prompt"])
            self.assertIn("speaks the supplied narration naturally, as quiet talk or inner monologue", it["prompt"])
            self.assertIn("not singing, no microphone, no stage", it["prompt"])
            self.assertTrue(it["prompt"].count("[Shot 1] Medium close-up, front-facing or three-quarter face view.") == 1, it["prompt"])      # 口が見える構図は歌唱と同じ規則
        self.assertIn("<Subject 2> is the location or object shown in <Picture 2>: a small warm cafe interior.", result["scenes"][0]["prompt"])
        self.assertNotIn("<Picture 2>", result["scenes"][1]["prompt"])

    async def test_speech_keeps_mouth_visible_directions(self):
        chat = FakeChat([[item(1, prompt="Wide shot from behind, profile")]])
        result = await self.gen(chat, [scene(1)], timeline="speech")
        prompt = result["scenes"][0]["prompt"]
        self.assertNotIn("from behind", prompt)
        self.assertNotIn("Wide shot", prompt)

    async def test_music_allows_no_person(self):
        chat = FakeChat([[item(1, refs=[1], no_person=True), item(2, refs=[2]), item(3, refs=[], no_person=True),
                          item(4, refs=[1], no_person="false")]])
        result = await self.gen(chat, [scene(i) for i in range(1, 5)], timeline="music")
        self.assertEqual([it["refs"] for it in result["scenes"]], [[1], [0, 2], [], [0, 1]])
        batch = chat.calls[0]["user"]
        self.assertIn("Set no_person to true", batch)
        self.assertIn('"no_person": false', batch)                  # テンプレートに載る
        self.assertNotIn("<Audio 1>", batch)
        prompts = [it["prompt"] for it in result["scenes"]]
        self.assertNotIn("<Audio 1>", "".join(prompts))
        self.assertIn("<Subject 1> is the location or object shown in <Picture 1>", prompts[0])       # 景色だけ: Picture 1
        self.assertIn("No people, faces or human figures appear", prompts[0])
        self.assertTrue(prompts[2].startswith("integrated_multimodal_description: [Shot 1]"))        # 景色も無ければ t2va
        self.assertIn("<Subject 1> is the exact character shown in <Picture 1>: a young girl", prompts[1])
        self.assertIn("<Subject 2> is the location or object shown in <Picture 2>: a small warm cafe interior.", prompts[1])
        self.assertIn("does not speak; the mouth stays naturally closed", prompts[1])

    async def test_free_with_person_and_scenery(self):
        scenes = [scene(1, place="platform", summary="She waits."), scene(2)]
        chat = FakeChat([[item(1, refs=["1"]), item(2, refs=[9, -1, "x", None, True, 1.0])]])
        result = await self.gen(chat, scenes, timeline="free")
        self.assertEqual([it["refs"] for it in result["scenes"]], [[0, 1], [0, 1]])          # 範囲外・person の添字・読めない値は捨てる
        self.assertIn('"place": "platform", "summary": "She waits"', chat.calls[0]["user"])
        self.assertIn("natural ambience and sound effects", result["scenes"][0]["prompt"].lower())
        self.assertIn("no dialogue and no music", result["scenes"][0]["prompt"].lower())

    async def test_scenery_refs_must_be_scenery_indexes(self):
        chat = FakeChat([[item(1, refs=[0, 0])]])
        result = await self.gen(chat, [scene(1)], timeline="free")
        self.assertEqual(result["scenes"][0]["refs"], [0])         # LLM が person の添字を書いても 1 回だけ

    async def test_without_person_everything_is_scenery_or_t2va(self):
        roles, descs = ["scenery", "scenery"], DESCS[1:]
        chat = FakeChat([[item(1, refs=[1]), item(2, refs=[])]])
        result = await self.gen(chat, [scene(1), scene(2)], timeline="free", roles=roles, descs=descs, character={})
        self.assertEqual([it["refs"] for it in result["scenes"]], [[1], []])
        batch = chat.calls[0]["user"]
        self.assertIn("There is no character in this video", batch)
        self.assertNotIn("CHARACTER_REFERENCE_FACTS", batch)
        self.assertNotIn("no_person", batch)
        self.assertIn("<Subject 1> is the location or object shown in <Picture 1>: a small warm cafe interior.", result["scenes"][0]["prompt"])
        self.assertTrue(result["scenes"][1]["prompt"].startswith("integrated_multimodal_description:"))

    async def test_no_references_at_all_is_all_t2va(self):
        chat = FakeChat([[item(1), item(2)]])
        result = await self.gen(chat, [scene(1), scene(2)], timeline="music", roles=[], descs=[], character={})
        self.assertEqual([it["refs"] for it in result["scenes"]], [[], []])
        for it in result["scenes"]:
            self.assertTrue(it["prompt"].startswith("integrated_multimodal_description: [Shot 1]"))
            self.assertNotIn("subject_definitions", it["prompt"])

    async def test_speech_without_a_person_image_is_an_error(self):
        with self.assertRaises(S.ScenarioError) as cm:
            await self.gen(FakeChat([]), [scene(1)], timeline="speech", roles=["scenery"], descs=DESCS[1:2])
        self.assertIn("人物の画像", cm.exception.message)
        with self.assertRaises(S.ScenarioError):
            await self.gen(FakeChat([]), [scene(1)], timeline="bogus")

    async def test_narration_sentences_land_in_the_right_scenes(self):
        sentences = [{"text": "はじめの文。", "start": 0.0, "end": 3.5}, {"text": "二つめ。", "start": 3.5, "end": 7.9},
                     {"text": "三つめ。", "start": 8.0, "end": 12.0}, {"text": "最後の文。", "start": 12.0, "end": 16.0},
                     {"text": "", "start": 1.0, "end": 2.0}, {"bad": 1}]
        chat = FakeChat([[item(1), item(2)]])
        await self.gen(chat, [scene(1), scene(2)], timeline="speech", narration_sentences=sentences)
        briefs = json.loads(re.search(r"SCENES: (\[.*\])\nReturn", chat.calls[0]["user"]).group(1))
        self.assertEqual(briefs[0]["spoken"], ["はじめの文。", "二つめ。"])
        self.assertEqual(briefs[1]["spoken"], ["三つめ。", "最後の文。"])
        self.assertIn("never quote it", chat.calls[0]["user"])
        # 読み上げの文は、最終のプロンプトには入らない(内容は <Audio 1> から。字幕にもしない)
        self.assertNotIn("はじめの文", "".join(it["prompt"] for it in (await self.gen(
            FakeChat([[item(1), item(2)]]), [scene(1), scene(2)], timeline="speech", narration_sentences=sentences))["scenes"]))

    async def test_voiceover_has_no_lip_sync_and_keeps_the_sentences(self):
        """人物の画像が無いセリフ(voiceover): 口の規則も <Audio 1> も無く、読み上げの文(SPOKEN)で景色を描く。景色の参照は使える。"""
        sentences = [{"text": "海が見えた。", "start": 0.0, "end": 4.0}, {"text": "風が吹く。", "start": 8.0, "end": 12.0}]
        chat = FakeChat([[item(1), item(2, refs=[0])]])
        result = await self.gen(chat, [scene(1), scene(2)], timeline="speech", roles=["scenery"], descs=DESCS[1:2], character={},
                                narration_sentences=sentences, voiceover=True)
        user = chat.calls[0]["user"]
        self.assertIn("off-screen voice-over", user)
        self.assertNotIn("lips move naturally", user)
        self.assertNotIn("CHARACTER_REFERENCE_FACTS", user)
        briefs = json.loads(re.search(r"SCENES: (\[.*\])\nReturn", user).group(1))
        self.assertEqual((briefs[0]["spoken"], briefs[1]["spoken"]), (["海が見えた。"], ["風が吹く。"]))
        self.assertEqual([it["refs"] for it in result["scenes"]], [[], [0]])
        for it in result["scenes"]:
            self.assertNotIn("<Audio 1>", it["prompt"])
            self.assertNotIn("synchroniz", it["prompt"])
        voiceless = await self.gen(FakeChat([[item(1)]]), [scene(1)], timeline="speech", roles=[], descs=[], character={}, voiceover=True)
        self.assertTrue(voiceless["scenes"][0]["prompt"].startswith("integrated_multimodal_description: [Shot 1]"))      # 画像が無ければ t2va

    async def test_narration_sentences_only_for_speech(self):
        chat = FakeChat([[item(1)]])
        await self.gen(chat, [scene(1)], timeline="music", narration_sentences=[{"text": "x", "start": 0, "end": 1}])
        self.assertNotIn("spoken", chat.calls[0]["user"].split("SCENES:")[1])

    def test_sentences_in_scene_boundaries(self):
        ss = [{"text": "a", "start": 0, "end": 4}, {"text": "b", "start": 4, "end": 8}, {"text": "c", "start": 6, "end": 10}]
        self.assertEqual(S.sentences_in_scene(ss, 0, 8), ["a", "b"])           # 中点 2, 6 / c は中点 8 で次のシーン
        self.assertEqual(S.sentences_in_scene(ss, 8, 10), ["c"])
        self.assertEqual(S.sentences_in_scene(None, 0, 8), [])
        self.assertEqual(S.sentences_in_scene(ss, None, None), [])

    async def test_batches_progress_and_previous(self):
        scenes = [scene(i) for i in range(1, 8)]
        chat = FakeChat([[item(i, refs=[1]) for i in range(1, 6)], [item(6), item(7)]])
        progress = []
        result = await self.gen(chat, scenes, timeline="free", on_progress=lambda d, t: progress.append((d, t)))
        self.assertEqual((expected_of(chat.calls[0]["user"]), expected_of(chat.calls[1]["user"])), ([1, 2, 3, 4, 5], [6, 7]))
        self.assertEqual(progress, [(5, 7), (7, 7)])
        self.assertEqual([it["index"] for it in result["scenes"]], list(range(1, 8)))
        previous = json.loads(re.search(r"PREVIOUS: (\{.*\})\nSCENES", chat.calls[1]["user"]).group(1))
        self.assertEqual((previous["index"], previous["prompt"]), (5, "A quiet shot 5"))      # 整形前の項目
        self.assertEqual(result["scenes"][0]["place"], "place1")
        self.assertEqual(set(result["scenes"][0]), {"index", "emotion", "place", "refs", "shot", "camera", "prompt"})

    async def test_place_falls_back_to_the_planned_one(self):
        chat = FakeChat([[item(1, **{"place": ""})]])
        result = await self.gen(chat, [scene(1, place="cafe at night")], timeline="free")
        self.assertEqual(result["scenes"][0]["place"], "cafe at night")

    async def test_correction_and_errors(self):
        chat = FakeChat([[item(0), item(1)], [item(1), item(2)]])
        result = await self.gen(chat, [scene(1), scene(2)], timeline="free")
        self.assertEqual(len(chat.calls), 2)
        self.assertIn("CORRECTION", chat.calls[1]["user"])
        self.assertEqual([it["index"] for it in result["scenes"]], [1, 2])
        with self.assertRaises(S.ScenarioError) as cm:
            await self.gen(FakeChat([[item(1)], [item(1)]]), [scene(1), scene(2)], timeline="free")
        self.assertIn("演出の数が合いません", cm.exception.message)
        with self.assertRaises(S.ScenarioError) as cm:
            await self.gen(FakeChat([[item(4), item(5)], [item(1), item(3)]]), [scene(1), scene(2)], timeline="free")
        self.assertIn("番号が合いません", cm.exception.message)
        with self.assertRaises(LlmError):
            await self.gen(FakeChat([LlmError("down")]), [scene(1)], timeline="free")

    async def test_frame_and_background_rules_reach_the_llm_and_prompt(self):
        chat = FakeChat([[item(1, refs=[1]), item(2, no_person=True)]])
        result = await self.gen(chat, [scene(1), scene(2)], timeline="music", frame="portrait", background_text="none")
        batch = chat.calls[0]["user"]
        self.assertIn("VERTICAL frame", batch)
        self.assertIn("the scene contains no readable letters or words", batch)
        self.assertIn("Vertical frame composition: <Subject 1> is centered horizontally", result["scenes"][0]["prompt"])
        self.assertIn(S.FRAME_SHOT_PREFIX_NO_PERSON["portrait"], result["scenes"][1]["prompt"])       # 人物のいないカットは中立の構図文
        self.assertNotIn("head and shoulders", result["scenes"][1]["prompt"])


class FormatTest(unittest.TestCase):
    def fmt(self, refs, direction="A slow dolly.", *, timeline="free", frame="landscape", character=GIRL, descs=DESCS, roles=ROLES):
        return S.format_h3_prompt({"refs": refs}, direction, character, descs, roles, frame=frame, timeline=timeline)

    def assertSectionsInOrder(self, text):
        positions = [text.index(s) for s in SECTIONS]
        self.assertEqual(positions, sorted(positions))

    def test_t2va_uses_the_official_three_fields(self):
        for timeline in ("free", "music"):
            text = self.fmt([], "A rainy street at night. Subtitles appear at the bottom.", timeline=timeline)
            self.assertTrue(text.startswith("integrated_multimodal_description: [Shot 1] "))
            self.assertEqual([m for m in re.findall(r"^(\w+):", text, re.M)],
                             ["integrated_multimodal_description", "overall_soundscape", "non_diegetic_music"])
            for gone in ("subject_definitions", "retention_analysis", "<Audio 1>", "<Subject", "<Picture"):
                self.assertNotIn(gone, text)
            self.assertIn("A rainy street at night", text)
            self.assertNotIn("Subtitles appear", text)
            self.assertIn(M.NO_OVERLAY_TEXT, text)
            self.assertTrue(text.endswith("non_diegetic_music: N/A"))
        self.assertIn("natural ambience and sound effects", self.fmt([], timeline="free").lower())
        self.assertIn("No dialogue, singing, or vocals", self.fmt([], timeline="music"))
        self.assertIn("Vertical frame composition: the main subject", self.fmt([], frame="portrait"))
        self.assertIn("[Shot 1] Cinematic", self.fmt([], frame="landscape"))

    def test_person_with_speech(self):
        text = self.fmt([0], "She turns to the window.", timeline="speech", frame="portrait")
        self.assertSectionsInOrder(text)
        self.assertIn("<Subject 1> is the exact character shown in <Picture 1>: a young girl, short black hair, red coat. "
                      "The same identity, facial structure, hair, clothing, and visual style must remain unchanged.", text)
        self.assertIn("<Audio 1> is the supplied narration", text)
        self.assertIn("<Audio 1>: fully_copy", text)
        self.assertIn("retained exactly: a young girl, short black hair, red coat.", text)
        self.assertIn("[reference generation + audio reuse]", text)
        self.assertIn("speaks the supplied narration naturally", text)
        self.assertIn("synchronization with <Audio 1>. The spoken content comes only from <Audio 1>.", text)
        self.assertIn("the spoken narration comes only from <Audio 1>", text)
        self.assertIn("[Shot 1] Vertical frame composition: <Subject 1> is centered horizontally", text)
        self.assertIn(M.NO_OVERLAY_TEXT, text)
        self.assertIn("No text overlay.", text)
        self.assertNotIn("Picture 2", text)
        self.assertTrue(text.endswith("non_diegetic_music:\nN/A"))
        # 歌唱の文言は使わない
        self.assertNotIn("supplied vocal", text)

    def test_person_without_speech_has_no_audio_and_a_closed_mouth(self):
        for timeline in ("free", "music"):
            text = self.fmt([0], timeline=timeline)
            self.assertSectionsInOrder(text)
            self.assertNotIn("<Audio 1>", text)
            self.assertIn("<Subject 1> does not speak; the mouth stays naturally closed and relaxed.", text)
            self.assertIn("[reference generation] ", text)
            self.assertNotIn("audio reuse", text)
        self.assertIn("natural ambience and sound effects", self.fmt([0], timeline="free").lower())
        self.assertIn("No dialogue, singing, or vocals", self.fmt([0], timeline="music"))

    def test_person_plus_scenery_numbers_pictures_in_refs_order(self):
        text = self.fmt([0, 2, 1], timeline="speech")
        self.assertSectionsInOrder(text)
        self.assertIn("<Subject 1> is the exact character shown in <Picture 1>", text)
        self.assertIn("<Subject 2> is the location or object shown in <Picture 2>: a small warm cafe interior.", text)     # refs の 2 番目 = 添字 2
        self.assertIn("<Subject 3> is the location or object shown in <Picture 3>: a rainy station platform with a clock.", text)
        self.assertIn("<Subject 2> (appears in [Shot 1]): partially_preserved - the layout, main objects and atmosphere are kept; "
                      "the camera angle, time of day and weather may change.", text)
        self.assertIn("<Subject 3> (appears in [Shot 1]): partially_preserved", text)
        self.assertIn("The shot is set in or around <Subject 2>, <Subject 3>.", text)
        self.assertNotIn("Picture 4", text)
        self.assertEqual(text.count("<Audio 1> is the supplied narration"), 1)
        # 人物は 1 つめ以外でも、その位置の番号を使う
        late = self.fmt([2, 0], timeline="free", frame="portrait")
        self.assertIn("<Subject 1> is the location or object shown in <Picture 1>", late)
        self.assertIn("<Subject 2> is the exact character shown in <Picture 2>", late)
        self.assertIn("<Subject 2> is centered horizontally", late)
        self.assertNotIn("<Subject 1> is centered", late)

    def test_scenery_only(self):
        text = self.fmt([2], "A close view of the coffee cups.", timeline="music", frame="square")
        self.assertSectionsInOrder(text)
        self.assertIn("<Subject 1> is the location or object shown in <Picture 1>: a small warm cafe interior.", text)
        self.assertNotIn("exact character", text)
        self.assertNotIn("<Audio 1>", text)
        self.assertIn("partially_preserved", text)
        self.assertNotIn("fully_preserved", text)
        self.assertIn("with no people in the frame", text)
        self.assertIn("No people, faces or human figures appear in this shot", text)
        self.assertIn("Square frame composition: the main subject of the scene is centered", text)
        self.assertNotIn("<Subject 1> is centered", text)           # 人物用の構図文は使わない
        self.assertIn(M.NO_OVERLAY_TEXT, text)

    def test_audio_only_with_speech_and_a_person(self):
        for refs, roles in (([], ROLES), ([1], ROLES), ([0], ROLES)):
            text = self.fmt(refs, timeline="speech", roles=roles)
            self.assertEqual("<Audio 1>" in text, 0 in refs, refs)

    def test_invalid_refs_and_missing_descriptions_are_tolerated(self):
        text = self.fmt([5, -1, "a", 1], descs=["x"], roles=ROLES)           # 1 だけが有効だが、説明の配列が足りない
        self.assertIn("<Subject 1> is the location or object shown in <Picture 1>.", text)
        self.assertNotIn(": .", text)
        empty = S.format_h3_prompt({"refs": [0]}, "dir", {}, DESCS, ROLES, frame="landscape", timeline="free")
        self.assertIn(M.DEFAULT_SUBJECT, empty)
        self.assertIsInstance(S.format_h3_prompt({}, "d", {}, [], [], frame="landscape", timeline="free"), str)

    def test_long_descriptions_are_clipped_to_one_line(self):
        desc = "a very long place " * 60 + "\nwith breaks"
        text = self.fmt([1], descs=["", desc, ""])
        line = next(l for l in text.splitlines() if l.startswith("<Subject 1> is the location"))
        self.assertLess(len(line), 600)
        self.assertTrue(line.endswith("...."))          # 切り詰めの "..." と文末の "."


if __name__ == "__main__":
    unittest.main()
