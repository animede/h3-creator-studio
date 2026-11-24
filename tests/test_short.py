import asyncio
import io
import sqlite3
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from studio import media, mv, short
from studio.handlers import mv as mvh
from studio.jobs import Ctx, Handler, JobFailed, JobRunner
from studio.ledger import Ledger
from studio.llm import LlmError
from studio.mvclip import quantize_scene_plan
from studio.scheduler import Operation


def png_bytes(size=(64, 48)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (10, 120, 200)).save(buf, "PNG")
    return buf.getvalue()


class FakeSched:
    def __init__(self):
        self.comps = {"h3": types.SimpleNamespace(spec=types.SimpleNamespace(base_url="http://h3.test"))}

    def new_operation(self, kind, name):
        return Operation(kind, name)

    async def ensure(self, name, op=None):
        op.add("準備完了")


def sentences_of(*durations) -> list:
    """読み上げの文(つながっている)。"""
    t, out = 0.0, []
    for i, d in enumerate(durations, start=1):
        out.append({"index": i, "text": f"文{i}。", "start": round(t, 3), "end": round(t + d, 3), "duration": d})
        t += d
    return out


class LedgerBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.ledger = Ledger(self.root)
        self.addCleanup(self.ledger.close)
        for name, ret in (("probe", {"duration_s": None, "width": None, "height": None}), ("video_thumb", False)):
            p = mock.patch.object(media, name, return_value=ret)
            p.start()
            self.addCleanup(p.stop)
        self.song = self.add("audio", "song.mp3", {"theme": "夜明けの駅", "caption": "A warm ballad", "genre": "Ballad"})
        self.girl = self.add("image", "girl.png", {"prompt": "a girl with silver hair, anime style"})
        self.garden = self.add("image", "garden.png", {"prompt": "a quiet garden"})
        self.town = self.add("image", "town.png", {})

    def add(self, kind: str, name: str, params: dict) -> dict:
        p = self.root / "uploads" / name
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(png_bytes() if kind == "image" else b"RIFFfake")
        return self.ledger.add_output(kind, p, "upload", filename=name, params=params, duration_s=30.0 if kind == "audio" else None)

    def resolve(self, **kw) -> dict:
        return short.resolve_params(self.ledger, kw)

    def person(self, **kw) -> dict:
        return {"id": self.girl["id"], "role": "person", **kw}

    def scenery(self, rec=None, **kw) -> dict:
        return {"id": (rec or self.garden)["id"], "role": "scenery", **kw}


class ResolveParamsTest(LedgerBase):
    def assertRejects(self, word: str, **kw):
        with self.assertRaises(ValueError, msg=str(kw)) as cm:
            self.resolve(**kw)
        self.assertIn(word, str(cm.exception))

    def test_timeline_is_decided_by_what_was_given(self):
        speech = self.resolve(narration="こんにちは。", speaker_id=3, images=[self.person()], song_id=self.song["id"])
        music = self.resolve(song_id=self.song["id"])
        free = self.resolve(story="雨の街の物語")
        self.assertEqual((speech["timeline"], music["timeline"], free["timeline"]), ("speech", "music", "free"))
        self.assertEqual((speech["song_id"], music["song_id"], free["song_id"]), (self.song["id"], self.song["id"], None))
        self.assertEqual((speech["kind"], music["kind"], free["kind"]), ("short", "short", "short"))

    def test_length_is_only_for_the_free_timeline(self):
        self.assertEqual(self.resolve(story="x")["length_s"], 60.0)                    # 既定 60 秒
        self.assertEqual(self.resolve(story="x", length_s="90")["length_s"], 90.0)
        self.assertEqual(self.resolve(story="x", length_s=30)["length_s"], 30.0)
        self.assertEqual(self.resolve(story="x", length_s=900)["length_s"], 900.0)     # 上限は無い
        self.assertIsNone(self.resolve(story="x", song_id=self.song["id"], length_s=10)["length_s"])      # 曲があれば時間軸は曲(長さは見ない)
        self.assertRejects("長さは 30 秒以上", story="x", length_s=29.9)
        self.assertRejects("長さ は数字", story="x", length_s="long")

    def test_story_is_required_without_a_song_and_without_narration(self):
        self.assertRejects("物語・映像のイメージを書いてください", story="  ")
        self.assertRejects("物語・映像のイメージを書いてください")
        self.resolve(song_id=self.song["id"])                                                    # 曲があれば物語は任意
        self.resolve(narration="話す。", speaker_id=1, images=[self.person()])                      # セリフがあれば物語は任意

    def test_narration_needs_exactly_one_person_image_and_a_speaker(self):
        self.assertRejects("口を合わせる人物の画像", narration="話す。", speaker_id=1)
        self.assertRejects("口を合わせる人物の画像", narration="話す。", speaker_id=1, images=[self.scenery()])
        self.assertRejects("話者を選んでください", narration="話す。", images=[self.person()])
        self.assertRejects("話者を選んでください", narration="話す。", speaker_id="", images=[self.person()])
        self.assertRejects("話者は整数", narration="話す。", speaker_id="abc", images=[self.person()])
        self.assertRejects("人物の画像は 1 枚まで", narration="話す。", speaker_id=1, images=[self.person(), self.person(id=self.town["id"])])
        self.assertEqual(self.resolve(narration="話す。", speaker_id="7", images=[self.person()])["speaker_id"], 7)
        self.assertIsNone(self.resolve(story="x")["speaker_id"])

    def test_narration_length_limit(self):
        self.assertRejects("4000 文字まで", narration="あ" * 4001, speaker_id=1, images=[self.person()])

    def test_images_count_roles_and_existence(self):
        five = [self.person(), self.scenery(), self.scenery(self.town), self.scenery(), self.scenery()]
        self.assertRejects("参照画像は 4 枚までです(いま 5 枚)", story="x", images=five)
        self.assertRejects("人物の画像は 1 枚まで", story="x", images=[self.person(), self.person(id=self.garden["id"])])
        self.assertRejects("参照画像 1 の役割", story="x", images=[{"id": self.girl["id"]}])
        self.assertRejects("参照画像 2 の役割", story="x", images=[self.person(), {"id": self.garden["id"], "role": "actor"}])
        self.assertRejects("参照画像 1が見つかりません", story="x", images=[{"id": "nope", "role": "scenery"}])
        self.assertRejects("参照画像 1を選んでください", story="x", images=[{"id": "", "role": "scenery"}])
        self.assertRejects("参照画像 1には画像を使ってください", story="x", images=[{"id": self.song["id"], "role": "scenery"}])
        self.assertRejects("参照画像は {id, role} の並び", story="x", images=["abc"])
        self.assertEqual(self.resolve(story="x", images=[])["images"], [])
        self.assertEqual(len(self.resolve(story="x", images=five[:4])["images"]), 4)

    def test_person_comes_first_and_descriptions_default_to_the_ledger_prompt(self):
        p = self.resolve(story="x", images=[self.scenery(), self.person(), self.scenery(self.town, description="夕方の街")])
        self.assertEqual([(i["id"], i["role"]) for i in p["images"]], [(self.girl["id"], "person"), (self.garden["id"], "scenery"), (self.town["id"], "scenery")])
        self.assertEqual([i["description"] for i in p["images"]], ["a girl with silver hair, anime style", "a quiet garden", "夕方の街"])
        self.assertEqual(short.person_index(p), 0)
        self.assertIsNone(short.person_index(self.resolve(story="x", images=[self.scenery()])))
        self.assertTrue(all(set(i) == {"id", "role", "description"} for i in p["images"]))       # 内部の印は残らない

    def test_character_comes_from_typed_text_then_the_image_prompt(self):
        p = self.resolve(story="x", images=[self.person()])
        self.assertEqual((p["character"], p["character_source"]), ("a girl with silver hair, anime style", "image"))
        p = self.resolve(story="x", images=[self.person(description="青い髪の少女")])
        self.assertEqual((p["character"], p["character_source"]), ("青い髪の少女", "typed"))
        p = self.resolve(story="x", images=[self.person(description="青い髪の少女")], character="赤い服の少年")
        self.assertEqual((p["character"], p["character_source"]), ("赤い服の少年", "typed"))
        p = self.resolve(story="x", images=[self.person(id=self.town["id"])])                      # 台帳にプロンプトの無い画像: 空 → script が画像を見て作る
        self.assertEqual((p["character"], p["character_source"]), ("", ""))
        p = self.resolve(story="x", images=[self.scenery()])                                       # 人物なし
        self.assertEqual((p["character"], p["character_source"]), ("", ""))

    def test_format_and_final_audio_follow_mv_rules(self):
        p = self.resolve(story="x")
        self.assertEqual((p["aspect"], p["quality"], p["width"], p["height"], p["min_s"], p["max_s"], p["seed"], p["background_text"], p["final_audio"]),
                         ("16:9", "fast", 768, 448, 5.0, 10.0, None, "auto", "auto"))
        p = self.resolve(story="x", aspect="9:16", quality="high", min_s=6, max_s=9, seed="42", background_text="none", final_audio="none")
        self.assertEqual((p["width"], p["height"], p["min_s"], p["max_s"], p["seed"], p["background_text"], p["final_audio"]), (768, 1344, 6.0, 9.0, 42, "none", "none"))
        self.assertRejects("投稿先は", story="x", aspect="3:2")
        self.assertRejects("画質は", story="x", quality="ultra")
        self.assertRejects("シーンの長さは 5〜10 秒", story="x", min_s=3)
        self.assertRejects("シーンの最短は最長より長くできません", story="x", min_s=9, max_s=6)
        self.assertRejects("seed は整数", story="x", seed="abc")
        self.assertRejects("background_text は", story="x", background_text="big")
        self.assertRejects("最終の音声は auto / none のいずれかです", story="x", final_audio="mix")

    def test_song_is_checked(self):
        self.assertRejects("曲が見つかりません", story="x", song_id="nope")
        self.assertRejects("曲には音声を使ってください", story="x", song_id=self.girl["id"])
        self.assertIsNone(self.resolve(story="x", song_id="")["song_id"])
        self.assertIsNone(self.resolve(story="x", song_id=None)["song_id"])

    def test_title_comes_from_the_story_then_the_song_then_the_narration(self):
        self.assertEqual(self.resolve(story="雨の街で\n二行目")["title"], "雨の街で")
        self.assertEqual(self.resolve(story="雨の駅で、旅立つ友人を見送る少女。最後に電車の窓越しに手を振る")["title"], "雨の駅で、旅立つ友人を見送る少女")      # 最初の文まで
        self.assertEqual(self.resolve(song_id=self.song["id"])["title"], "夜明けの駅")
        p = self.resolve(narration="こんにちは。今日は晴れです。", speaker_id=1, images=[self.person()])
        self.assertEqual(p["title"], "こんにちは")
        self.assertEqual(len(self.resolve(story="あ" * 100)["title"]), 24)

    def test_resolved_params_are_json_friendly_and_complete(self):
        p = self.resolve(story="雨の街", song_id=self.song["id"], images=[self.person(), self.scenery()])
        for key in ("kind", "story", "song_id", "narration", "speaker_id", "images", "length_s", "timeline", "aspect", "quality", "width", "height",
                    "min_s", "max_s", "seed", "background_text", "final_audio", "character", "character_source", "character_note", "title"):
            self.assertIn(key, p)
        self.assertEqual((p["narration"], p["timeline"]), ("", "music"))


class SentencesTest(unittest.TestCase):
    def test_split_keeps_marks_and_breaks_on_newlines(self):
        self.assertEqual(short.split_sentences("おはよう。今日は晴れ！ほんとう？\nうん\n\n  最後です  "),
                         ["おはよう。", "今日は晴れ！", "ほんとう？", "うん", "最後です"])
        self.assertEqual(short.split_sentences("Hello! How are you? Fine."), ["Hello!", "How are you?", "Fine."])
        self.assertEqual(short.split_sentences("  \n "), [])

    def test_scenes_fall_on_sentence_boundaries_within_the_length_range(self):
        sentences = sentences_of(3.0, 4.0, 2.5, 3.5, 4.5, 3.0, 2.0, 5.0, 3.0)         # 合計 30.5 秒
        scenes = short.plan_speech_scenes(sentences, 5.0, 10.0)
        ends = {s["end"] for s in sentences}
        self.assertEqual([s["index"] for s in scenes], list(range(1, len(scenes) + 1)))
        self.assertEqual((scenes[0]["start"], scenes[-1]["end"]), (0.0, 30.5))
        for a, b in zip(scenes, scenes[1:]):
            self.assertEqual(a["end"], b["start"])                                        # つながっている
        for s in scenes:
            self.assertIn(round(s["end"], 3), ends)                                       # 切れ目は文の終わり
            self.assertLessEqual(s["duration"], 10.0)
            self.assertGreaterEqual(s["duration"], 5.0)
            self.assertAlmostEqual(s["duration"], s["end"] - s["start"])
            self.assertEqual((s["vocal_ratio"], s["kind"]), (1.0, "vocal"))
        self.assertEqual(scenes[-1]["boundary_kind"], "end")
        self.assertEqual({s["boundary_kind"] for s in scenes[:-1]}, {"sentence"})
        self.assertEqual(len(scenes), 4)                                                  # 30.5 秒 ÷ 目標 7.5 秒 ≈ 4

    def test_a_long_sentence_is_split_evenly_and_marked(self):
        scenes = short.plan_speech_scenes(sentences_of(24.0), 5.0, 10.0)
        self.assertEqual([round(s["duration"], 3) for s in scenes], [8.0, 8.0, 8.0])
        self.assertEqual([s["boundary_kind"] for s in scenes], ["split", "split", "end"])
        scenes = short.plan_speech_scenes(sentences_of(3.0, 14.0, 3.0), 5.0, 10.0)
        self.assertTrue(all(s["duration"] <= 10.0 + 1e-6 for s in scenes))
        self.assertAlmostEqual(scenes[-1]["end"], 20.0)

    def test_short_total_is_one_scene_and_a_short_tail_is_kept_when_it_cannot_be_helped(self):
        (only,) = short.plan_speech_scenes(sentences_of(2.0, 1.5), 5.0, 10.0)
        self.assertEqual((only["start"], only["end"], only["duration"]), (0.0, 3.5, 3.5))          # 5 秒に満たなくても 1 シーン(音声は H3 に渡すとき 5 秒まで延ばす)
        self.assertEqual(short.plan_speech_scenes([], 5.0, 10.0), [])
        scenes = short.plan_speech_scenes(sentences_of(10.0, 10.0, 2.0), 10.0, 10.0)               # 最短 = 最長 = 10: 最後が足りない
        self.assertEqual([round(s["duration"], 3) for s in scenes], [10.0, 10.0, 2.0])

    def test_balanced_cut_avoids_a_tiny_last_scene(self):
        # 10 + 3 秒の先頭に貪欲に詰めると、最後が 3 秒になる。目標に近い切り方なら、最後も 5 秒以上
        scenes = short.plan_speech_scenes(sentences_of(4.0, 4.0, 4.0, 4.0, 4.0, 4.0), 5.0, 10.0)      # 合計 24
        self.assertTrue(all(5.0 <= s["duration"] <= 10.0 for s in scenes), [s["duration"] for s in scenes])
        self.assertEqual(len(scenes), 3)

    def test_quantized_plan_keeps_the_extra_keys_and_covers_the_narration(self):
        sentences = sentences_of(3.0, 4.0, 2.5, 3.5, 4.5)
        plan = short.plan_speech_scenes(sentences, 5.0, 10.0)
        for s in plan:
            s["text"] = short.sentence_text(sentences, s["start"], s["end"], last=s["index"] == len(plan))
        scenes = short.make_scenes(quantize_scene_plan(plan), "speech", 0)
        self.assertEqual("".join(s["text"] for s in scenes), "".join(s["text"] for s in sentences))      # どの文も、ちょうど 1 つのシーンに入る
        self.assertEqual(sum(s["frame_count"] for s in scenes), round(17.5 * 24))
        for s in scenes:
            self.assertEqual((s["speech"], s["refs"], s["status"], s["prompt"], s["place"], s["vocal_ratio"]), (True, [0], "pending", "", "", 1.0))

    def test_make_scenes_per_timeline(self):
        plan = quantize_scene_plan([{"index": 1, "start": 0, "end": 6.0, "duration": 6.0, "kind": "free", "boundary_kind": "plan", "place": "駅", "summary": "待つ"}])
        (s,) = short.make_scenes(plan, "free")
        self.assertEqual((s["speech"], s["refs"], s["place"], s["summary"], s["kind"], s["vocal_ratio"]), (False, [], "駅", "待つ", "free", 0.0))
        (s,) = short.make_scenes(plan, "music")
        self.assertEqual((s["speech"], s["refs"]), (False, []))
        (s,) = short.make_scenes(plan, "speech", None)                       # 人物が無ければ refs は空(resolve が弾くので通らない形)
        self.assertEqual((s["speech"], s["refs"]), (True, []))


class NextStepTest(unittest.TestCase):
    def proj(self, timeline, analysis=True, scenes=(), assembled=False):
        return {"params": {"timeline": timeline, "analysis": {"duration": 20.0} if analysis else None, "assembled": assembled}, "scenes": list(scenes)}

    def scene(self, index, status="pending", prompt="p"):
        return {"index": index, "status": status, "prompt": prompt}

    def test_speech_and_music_go_analyze_script_scenes_assemble(self):
        for timeline in ("speech", "music"):
            self.assertEqual(short.next_step(self.proj(timeline, analysis=False)), ("analyze",))
            self.assertEqual(short.next_step(self.proj(timeline, analysis=True)), ("analyze",))                    # 分析はあるが、シーンが無い(壊れている)→ やり直し
            self.assertEqual(short.next_step(self.proj(timeline, scenes=[self.scene(1, prompt=""), self.scene(2, prompt="")])), ("script",))
            self.assertEqual(short.next_step(self.proj(timeline, scenes=[self.scene(1, "done"), self.scene(2), self.scene(3)])), ("scene", 2))
            self.assertEqual(short.next_step(self.proj(timeline, scenes=[self.scene(1, "done"), self.scene(2, "failed")])), ("scene", 2))
            self.assertEqual(short.next_step(self.proj(timeline, scenes=[self.scene(1, "done")])), ("assemble",))
            self.assertEqual(short.next_step(self.proj(timeline, scenes=[self.scene(1, "done")], assembled=True)), ("done",))

    def test_free_goes_analyze_plan_script_scenes_assemble(self):
        self.assertEqual(short.next_step(self.proj("free", analysis=False)), ("analyze",))
        self.assertEqual(short.next_step(self.proj("free", analysis=True)), ("plan",))                       # 分析(長さを置く)の次は、シーン割り
        self.assertEqual(short.next_step(self.proj("free", scenes=[self.scene(1, prompt="")])), ("script",))
        self.assertEqual(short.next_step(self.proj("free", scenes=[self.scene(2), self.scene(1, "done")])), ("scene", 2))
        self.assertEqual(short.next_step(self.proj("free", scenes=[self.scene(1, "done")])), ("assemble",))
        self.assertEqual(short.next_step(self.proj("free", scenes=[self.scene(1, "done")], assembled=True)), ("done",))

    def test_mv_next_step_is_unchanged_and_knows_no_plan(self):
        proj = {"params": {"analysis": {"duration": 20.0}}, "scenes": []}
        self.assertEqual(mv.next_step(proj), ("analyze",))


class LedgerKindTest(LedgerBase):
    def test_kind_defaults_to_mv_and_filters_the_list(self):
        self.ledger.create_mv_project("a", "analyzing", {}, [], "")
        self.ledger.create_mv_project("b", "analyzing", {}, [], "", kind="short")
        self.assertEqual([self.ledger.get_mv_project(i)["kind"] for i in "ab"], ["mv", "short"])
        self.assertEqual([p["id"] for p in self.ledger.list_mv_projects()], ["b", "a"])                     # 絞らなければ全部
        self.assertEqual([p["id"] for p in self.ledger.list_mv_projects(kind="mv")], ["a"])
        self.assertEqual([p["id"] for p in self.ledger.list_mv_projects(kind="short")], ["b"])
        self.ledger.update_mv_project("b", state="paused")
        self.assertEqual(self.ledger.get_mv_project("b")["kind"], "short")

    def test_an_old_database_without_the_kind_column_is_migrated(self):
        root = self.root / "old"
        root.mkdir()
        db = sqlite3.connect(root / "studio.db")
        db.executescript("""CREATE TABLE mv_projects (
            id TEXT PRIMARY KEY, created_at REAL NOT NULL, updated_at REAL NOT NULL, state TEXT NOT NULL,
            params_json TEXT, scenes_json TEXT, output_id TEXT, error TEXT, message TEXT);
            INSERT INTO mv_projects VALUES ('old1', 1.0, 1.0, 'completed', '{}', '[]', NULL, NULL, '');""")
        db.commit()
        db.close()
        ledger = Ledger(root)
        self.addCleanup(ledger.close)
        self.assertEqual(ledger.get_mv_project("old1")["kind"], "mv")                                     # 既存の制作は mv
        ledger.create_mv_project("new1", "analyzing", {}, [], "", kind="short")
        self.assertEqual([p["id"] for p in ledger.list_mv_projects(kind="mv")], ["old1"])
        self.assertEqual([p["id"] for p in ledger.list_mv_projects(kind="short")], ["new1"])
        Ledger(root).close()                                                                              # もう一度開いても(列があっても)壊れない


class ShortBase(unittest.IsolatedAsyncioTestCase):
    """台帳(実物)・JobRunner(実物)の上に、short.* のジョブの代わり(偽のハンドラ)を載せて ShortDirector を動かす。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.ledger = Ledger(self.root)
        self.addCleanup(self.ledger.close)
        for name, ret in (("probe", {"duration_s": None, "width": None, "height": None}), ("video_thumb", False)):
            p = mock.patch.object(media, name, return_value=ret)
            p.start()
            self.addCleanup(p.stop)
        self.song = self.add("audio", "song.mp3", {"theme": "夜明けの駅"})
        self.girl = self.add("image", "girl.png", {"prompt": "a girl with silver hair"})
        self.garden = self.add("image", "garden.png", {"prompt": "a quiet garden"})
        self.log, self.durations, self.failing = [], [8.0, 6.0, 7.0], {}

    def add(self, kind: str, name: str, params: dict) -> dict:
        p = self.root / "uploads" / name
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(png_bytes() if kind == "image" else b"RIFFfake")
        return self.ledger.add_output(kind, p, "upload", filename=name, params=params, duration_s=30.0 if kind == "audio" else None)

    def plan(self, timeline) -> list:
        t, out = 0.0, []
        for i, d in enumerate(self.durations, start=1):
            out.append({"index": i, "start": t, "end": t + d, "duration": d, "vocal_ratio": 1.0 if timeline == "speech" else 0.0,
                        "kind": "free" if timeline == "free" else "vocal", "boundary_kind": "plan", "place": f"place{i}" if timeline == "free" else ""})
            t += d
        return short.make_scenes(quantize_scene_plan(out), timeline, 0 if timeline == "speech" else None)

    def handlers(self) -> dict:
        def gate(name):
            if self.failing.get(name, 0) > 0:
                self.failing[name] -= 1
                raise JobFailed(f"{name} が失敗した")

        async def analyze(job, ctx):
            self.log.append("analyze")
            gate("analyze")
            pid = job.params["project_id"]
            p = ctx.ledger.get_mv_project(pid)["params"]
            scenes = [] if p["timeline"] == "free" else self.plan(p["timeline"])
            duration = p["length_s"] if p["timeline"] == "free" else sum(self.durations)
            ctx.ledger.update_mv_project(pid, scenes=scenes, params_patch={"analysis": {"duration": duration, "scenes": len(scenes)}, "assembled": False})

        async def plan(job, ctx):
            self.log.append("plan")
            gate("plan")
            ctx.ledger.update_mv_project(job.params["project_id"], scenes=self.plan("free"), params_patch={"outline": {}, "assembled": False})

        async def script(job, ctx):
            self.log.append("script")
            gate("script")
            proj = ctx.ledger.get_mv_project(job.params["project_id"])
            ctx.ledger.update_mv_project(proj["id"], params_patch={"script": {"title": "T"}}, scene_patches={
                s["index"]: {"prompt": f"prompt {s['index']}", "seed": 1000 + s["index"], "refs": []} for s in proj["scenes"]})

        async def scene(job, ctx):
            i, pid = job.params["scene"], job.params["project_id"]
            self.log.append(f"scene{i}")
            gate(f"scene{i}")
            clip = short.work_dir(ctx.outputs_dir, pid) / mv.scene_clip_name(i)
            clip.parent.mkdir(parents=True, exist_ok=True)
            clip.write_bytes(b"clip")
            ctx.ledger.update_mv_project(pid, scene_patches={i: {"status": "done", "clip": clip.name, "thumb": None, "error": None}})

        async def assemble(job, ctx):
            self.log.append("assemble")
            gate("assemble")
            pid = job.params["project_id"]
            dest = mvh.final_path(ctx.outputs_dir, pid, "short")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"mp4")
            rec = ctx.ledger.add_output("video", dest, job.tab, job.id, "s.mp4", {"project_id": pid}, width=768, height=448, duration_s=21.0)
            job.outputs.append(rec["id"])
            ctx.ledger.update_mv_project(pid, output_id=rec["id"], params_patch={"assembled": True})

        async def cancel(job, ctx):
            pass

        def h(component, run, gpu=True):
            return Handler(component, mvh.normalize, run, cancel, gpu=gpu)
        return {"short.analyze": h(None, analyze, False), "short.plan": h(None, plan, False), "short.script": h(None, script, False),
                "short.scene": h("h3", scene), "short.assemble": h(None, assemble, False)}

    def start(self):
        self.runner = JobRunner(Ctx(FakeSched(), self.ledger, self.root), self.handlers(), step_poll_s=0.01)
        self.runner.start()
        self.addAsyncCleanup(self.runner.stop)
        self.director = short.ShortDirector(self.runner, self.ledger, self.root)
        self.mv_director = mv.Director(self.runner, self.ledger, self.root)
        return self.director

    def create(self, timeline="music", **kw) -> dict:
        base = {"music": {"song_id": self.song["id"]}, "free": {"story": "雨の街の物語"},
                "speech": {"narration": "こんにちは。", "speaker_id": 1, "images": [{"id": self.girl["id"], "role": "person"}]}}[timeline]
        return self.director.create({**base, **kw})

    async def until(self, cond, what="条件", timeout=5.0):
        for _ in range(int(timeout / 0.01)):
            if cond():
                return
            await asyncio.sleep(0.01)
        self.fail(f"待っても {what} にならない")

    def state(self, pid) -> str:
        return self.ledger.get_mv_project(pid)["state"]

    async def until_state(self, pid, *states):
        await self.until(lambda: self.state(pid) in states, f"状態が {states}(いま {self.state(pid)})")

    def jobs(self, kind=None) -> list:
        return [j for j in self.runner.jobs.values() if kind is None or j.kind == kind]


class DirectorFlowTest(ShortBase):
    async def test_speech_and_music_flow(self):
        self.start()
        for timeline in ("speech", "music"):
            self.log.clear()
            view = self.create(timeline)
            self.assertEqual(view["state"], "analyzing")
            await self.until_state(view["id"], "completed")
            self.assertEqual(self.log, ["analyze", "script", "scene1", "scene2", "scene3", "assemble"], timeline)
            self.assertEqual(self.ledger.get_mv_project(view["id"])["message"], "ショート動画ができました")

    async def test_free_flow_has_a_plan_step_after_analyze(self):
        self.start()
        view = self.create("free")
        await self.until_state(view["id"], "completed")
        self.assertEqual(self.log, ["analyze", "plan", "script", "scene1", "scene2", "scene3", "assemble"])
        proj = self.ledger.get_mv_project(view["id"])
        self.assertEqual([s["place"] for s in proj["scenes"]], ["place1", "place2", "place3"])
        self.assertEqual(proj["params"]["analysis"]["duration"], 60.0)

    async def test_jobs_use_the_short_prefix_tab_and_lanes(self):
        self.start()
        pid = self.create("free")["id"]
        await self.until_state(pid, "completed")
        self.assertEqual([j.kind for j in self.jobs()], ["short.analyze", "short.plan", "short.script", "short.scene", "short.scene", "short.scene", "short.assemble"])
        self.assertTrue(all(j.tab == "short" and j.params["project_id"] == pid for j in self.jobs()))
        self.assertEqual([j.lane for j in self.jobs()], ["cpu", "cpu", "cpu", "h3", "h3", "h3", "cpu"])
        out = self.ledger.get_output(self.ledger.get_mv_project(pid)["output_id"])
        self.assertEqual((out["kind"], out["tab"], out["params"]["project_id"]), ("video", "short", pid))
        self.assertIn("short_" + pid, out["path"])

    async def test_states_follow_the_stages(self):
        self.start()
        seen = []

        def spy(kind):
            base = self.handlers()[kind]

            async def run(job, ctx):
                seen.append((kind, self.state(job.params["project_id"])))
                await base.run(job, ctx)
            return Handler(base.component, base.normalize, run, base.cancel, gpu=base.gpu)
        self.runner.handlers.update({k: spy(k) for k in ("short.analyze", "short.plan", "short.script", "short.scene", "short.assemble")})
        pid = self.create("free")["id"]
        await self.until_state(pid, "completed")
        self.assertEqual([s for s in seen if s[0] != "short.scene"], [("short.analyze", "analyzing"), ("short.plan", "scripting"), ("short.script", "scripting"),
                                                                     ("short.assemble", "assembling")])
        self.assertEqual({s[1] for s in seen if s[0] == "short.scene"}, {"generating"})

    async def test_failed_plan_resumes_from_the_plan(self):
        self.start()
        self.failing["plan"] = 1
        pid = self.create("free")["id"]
        await self.until_state(pid, "failed")
        proj = self.ledger.get_mv_project(pid)
        self.assertEqual((proj["message"], proj["error"]), ("シーン割りに失敗しました", "plan が失敗した"))
        self.director.resume(pid)
        await self.until_state(pid, "completed")
        self.assertEqual(self.log, ["analyze", "plan", "plan", "script", "scene1", "scene2", "scene3", "assemble"])

    async def test_failed_analyze_message_names_the_audio_preparation(self):
        self.start()
        self.failing["analyze"] = 1
        pid = self.create("speech")["id"]
        await self.until_state(pid, "failed")
        self.assertEqual(self.ledger.get_mv_project(pid)["message"], "音声の準備に失敗しました")

    async def test_retry_scene_and_reassemble_with_final_audio(self):
        self.start()
        pid = self.create("music")["id"]
        await self.until_state(pid, "completed")
        self.log.clear()
        self.director.retry_scene(pid, 2)
        await self.until_state(pid, "completed")
        self.assertEqual(self.log, ["scene2", "assemble"])
        with self.assertRaises(ValueError) as cm:
            self.director.reassemble(pid, "mix")
        self.assertIn("最終の音声は auto / none のいずれかです", str(cm.exception))
        self.assertEqual(self.ledger.get_mv_project(pid)["params"]["final_audio"], "auto")             # 不正なら書き換えない
        self.director.reassemble(pid, "none")
        await self.until(lambda: self.log.count("assemble") == 2, "結合し直す")
        await self.until_state(pid, "completed")
        self.assertEqual(self.ledger.get_mv_project(pid)["params"]["final_audio"], "none")
        self.director.reassemble(pid)                                                                 # 渡さなければ、設定は変えない
        await self.until(lambda: self.log.count("assemble") == 3, "もう一度結合")
        await self.until_state(pid, "completed")
        self.assertEqual(self.ledger.get_mv_project(pid)["params"]["final_audio"], "none")
        self.assertEqual(len(self.director.output_ids(pid)), 4)                                       # 結合し直しても、前の動画は残る
        self.assertTrue(any(p.name == f"short_{pid}_4.mp4" for p in (self.root / "video").iterdir()))

    async def test_reassemble_refuses_before_the_scenes_are_done_without_touching_the_setting(self):
        self.start()
        pid = self.create("music")["id"]
        with self.assertRaises(ValueError):
            self.director.reassemble(pid, "none")
        self.assertEqual(self.ledger.get_mv_project(pid)["params"]["final_audio"], "auto")
        await self.until_state(pid, "completed")


class SeparationTest(ShortBase):
    async def test_list_and_view_are_per_kind(self):
        self.start()
        mv_params = {"song_id": self.song["id"], "image_id": self.girl["id"]}
        mv_view = self.mv_director.create(mv_params)
        short_view = self.create("music")
        await self.until_state(mv_view["id"], "analyzing", "scripting", "generating", "assembling", "completed", "failed")
        self.assertEqual([p["id"] for p in self.director.list()], [short_view["id"]])
        self.assertEqual([p["id"] for p in self.mv_director.list()], [mv_view["id"]])
        self.assertIsNone(self.director.view(mv_view["id"]))
        self.assertIsNone(self.mv_director.view(short_view["id"]))
        for method, args in (("pause", ()), ("resume", ()), ("cancel", ()), ("retry_scene", (1,)), ("reassemble", ()), ("delete", ()), ("output_ids", ())):
            with self.assertRaises(LookupError, msg=method):
                result = getattr(self.director, method)(mv_view["id"], *args)
                if asyncio.iscoroutine(result):
                    await result

    async def test_view_has_all_params_scenes_and_the_short_scene_keys(self):
        self.start()
        pid = self.create("speech")["id"]
        await self.until_state(pid, "completed")
        v = self.director.view(pid)
        self.assertEqual((v["title"], v["state"], v["progress"]), ("こんにちは", "completed", 1.0))
        for key in ("kind", "story", "song_id", "narration", "speaker_id", "images", "length_s", "timeline", "final_audio", "character", "title", "outline"):
            self.assertIn(key, v["params"])
        self.assertEqual((v["params"]["timeline"], v["params"]["speaker_id"], v["params"]["images"][0]["role"]), ("speech", 1, "person"))
        self.assertEqual(v["script"], {"title": "T"})
        self.assertTrue(v["scenes"][0]["clip_url"].startswith(f"/outputs/short/{pid}/scene_001.mp4"))
        for key in ("place", "summary", "refs", "speech", "prompt", "text"):
            self.assertIn(key, v["scenes"][0])
        (item,) = self.director.list()
        self.assertEqual((item["timeline"], item["final_audio"], item["scenes"], item["done"]), ("speech", "auto", 3, 3))
        self.assertIsNone(self.director.view("nope"))

    async def test_cover_thumb_comes_from_the_first_image_until_the_video_exists(self):
        self.start()
        proj = self.director.create({"story": "x", "images": [{"id": self.garden["id"], "role": "scenery"}, {"id": self.girl["id"], "role": "person"}]})
        self.assertEqual(self.director._cover_id(self.ledger.get_mv_project(proj["id"])["params"]), self.girl["id"])        # 人物が先頭
        self.assertIsNone(self.director._cover_id({"images": []}))

    async def test_restore_pauses_only_its_own_kind(self):
        self.start()
        self.ledger.create_mv_project("m1", "generating", {}, [], "", kind="mv")
        self.ledger.create_mv_project("s1", "generating", {}, [], "", kind="short")
        self.assertEqual(self.director.restore(), 1)
        self.assertEqual((self.state("m1"), self.state("s1")), ("generating", "paused"))
        self.assertEqual(self.mv_director.restore(), 1)
        self.assertEqual(self.state("m1"), "paused")

    async def test_delete_removes_the_short_work_dir(self):
        self.start()
        pid = self.create("music")["id"]
        await self.until_state(pid, "completed")
        work = short.work_dir(self.root, pid)
        self.assertTrue(work.is_dir())
        await self.director.delete(pid)
        self.assertFalse(work.exists())
        self.assertIsNone(self.ledger.get_mv_project(pid))


class NarrationProposalTest(unittest.IsolatedAsyncioTestCase):
    class Llm:
        def __init__(self, *replies):
            self.replies, self.calls = list(replies), []

        async def chat(self, system, user, max_tokens=0, temperature=0):
            self.calls.append(user)
            reply = self.replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

    async def test_returns_clean_japanese_text(self):
        llm = self.Llm("ナレーション: 「朝の駅に、少女が立っていた。\n電車はまだ来ない。」")
        text = await short.propose_narration(llm, "朝の駅で待つ少女の物語", "切ないバラード")
        self.assertEqual(text, "朝の駅に、少女が立っていた。\n電車はまだ来ない。")
        self.assertIn("朝の駅で待つ少女の物語", llm.calls[0])
        self.assertIn("BACKGROUND MUSIC", llm.calls[0])
        self.assertIn("切ないバラード", llm.calls[0])
        llm = self.Llm("A girl waits.")                               # 日本語でなければ頼み直す
        llm.replies.append("少女は待っていた。")
        self.assertEqual(await short.propose_narration(llm, "物語"), "少女は待っていた。")
        self.assertEqual(len(llm.calls), 2)
        self.assertNotIn("BACKGROUND MUSIC", llm.calls[0])
        self.assertIn("must be written in Japanese", llm.calls[1])

    async def test_errors(self):
        with self.assertRaises(LlmError) as cm:
            await short.propose_narration(self.Llm(), "  ")
        self.assertIn("物語の文章を書いてください", cm.exception.message)
        with self.assertRaises(LlmError) as cm:
            await short.propose_narration(self.Llm("English only.", "Still English."), "物語")
        self.assertIn("日本語で答えませんでした", cm.exception.message)
        with self.assertRaises(LlmError):
            await short.propose_narration(self.Llm(LlmError("LLM が止まっています")), "物語")


if __name__ == "__main__":
    unittest.main()
