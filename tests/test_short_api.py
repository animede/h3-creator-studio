import io
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient
from PIL import Image

from studio import main, media, mv, short
from studio.ledger import Ledger
from studio.llm import LlmError
from studio.mvclip import quantize_scene_plan


def png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (10, 120, 200)).save(buf, "PNG")
    return buf.getvalue()


class FakeLlm:
    """Llm の代わり(chat だけ)。replies を順に返す(Exception なら投げる)。"""

    def __init__(self, *replies):
        self.replies, self.chats = list(replies), []

    async def chat(self, system, user, max_tokens=0, temperature=0):
        self.chats.append(user)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class StubRunner:
    """JobRunner の代わり(Director が使う口だけ)。入れられたジョブを残し、状態は手で動かす。"""

    def __init__(self):
        self.jobs, self.cancelled, self.callbacks = {}, [], []

    def on_finished(self, callback):
        self.callbacks.append(callback)

    def submit(self, tab, kind, params):
        job = types.SimpleNamespace(id=f"job{len(self.jobs) + 1}", tab=tab, kind=kind, params=params, status="queued", progress=0.0,
                                    message="順番を待っています", error=None, cancel_requested=False)
        self.jobs[job.id] = job
        return job

    def get(self, job_id):
        return self.jobs.get(job_id)

    async def cancel(self, job_id):
        self.cancelled.append(job_id)
        self.jobs[job_id].cancel_requested, self.jobs[job_id].status = True, "cancelled"
        return self.jobs[job_id]


class ShortApiTest(unittest.TestCase):
    """ショート動画の API。lifespan は使わず、必要な app.state だけを差し替える(Director は実物、JobRunner は StubRunner)。"""

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
        self.song = self.add("audio", "song.mp3", {"theme": "夜明けの駅", "genre": "Ballad"})
        self.girl = self.add("image", "girl.png", {"prompt": "a girl with silver hair"})
        self.garden = self.add("image", "garden.png", {"prompt": "a quiet garden"})
        self.runner = StubRunner()
        keys = ("mv", "short", "ledger", "jobs", "llm")
        self.saved = {k: getattr(main.app.state, k, None) for k in keys}
        self.addCleanup(lambda: [setattr(main.app.state, k, v) for k, v in self.saved.items()])
        main.app.state.mv = mv.Director(self.runner, self.ledger, self.root)
        main.app.state.short = short.ShortDirector(self.runner, self.ledger, self.root)
        main.app.state.ledger, main.app.state.jobs = self.ledger, self.runner
        self.llm = main.app.state.llm = FakeLlm()
        self.client = TestClient(main.app)

    def add(self, kind, name, params):
        p = self.root / "uploads" / name
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(png_bytes() if kind == "image" else b"RIFFfake")
        return self.ledger.add_output(kind, p, "upload", filename=name, params=params, duration_s=30.0 if kind == "audio" else None)

    def person(self):
        return {"id": self.girl["id"], "role": "person"}

    def scenery(self):
        return {"id": self.garden["id"], "role": "scenery"}

    def body(self, **kw) -> dict:
        return {"story": "雨の街の物語", **kw}

    def create(self, **kw) -> dict:
        r = self.client.post("/api/short", json=self.body(**kw))
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def finished(self, state="completed", statuses=("done", "done", "done"), assembled=True, output=True, **kw) -> str:
        """分析・演出・生成まで済んだ制作を台帳に作る。"""
        pid = self.create(**kw)["id"]
        plan = [{"index": i, "start": 8.0 * (i - 1), "end": 8.0 * i, "duration": 8.0, "kind": "free", "vocal_ratio": 0.0, "boundary_kind": "plan", "place": f"p{i}"}
                for i in range(1, len(statuses) + 1)]
        scenes = short.make_scenes(quantize_scene_plan(plan), "free")
        for s, st in zip(scenes, statuses):
            s.update(status=st, prompt=f"prompt {s['index']}", seed=1000 + s["index"], clip=mv.scene_clip_name(s["index"]) if st == "done" else None)
        fields = {}
        if output:
            dest = self.root / "video" / f"short_{pid}.mp4"
            dest.parent.mkdir(exist_ok=True)
            dest.write_bytes(b"mp4")
            fields["output_id"] = self.ledger.add_output("video", dest, "short", None, "x_short.mp4", {"project_id": pid}, width=768, height=448, duration_s=24.0)["id"]
        self.runner.jobs.clear()
        self.ledger.update_mv_project(pid, state=state, scenes=scenes, params_patch={"analysis": {"duration": 24.0, "scenes": len(scenes)}, "assembled": assembled}, **fields)
        return pid

    # ---- 作る・見る ----

    def test_create_free_returns_the_view_and_submits_the_first_job(self):
        js = self.create(length_s=90, aspect="9:16", quality="standard", seed=3, min_s=6, max_s=9, final_audio="none")
        self.assertEqual((js["state"], js["error"], js["scenes"], js["output"], js["job_status"]), ("analyzing", None, [], None, "queued"))
        p = js["params"]
        self.assertEqual((p["kind"], p["timeline"], p["story"], p["length_s"], p["song_id"], p["narration"], p["speaker_id"], p["images"]),
                         ("short", "free", "雨の街の物語", 90.0, None, "", None, []))
        self.assertEqual((p["aspect"], p["quality"], p["width"], p["height"], p["min_s"], p["max_s"], p["seed"], p["final_audio"], p["background_text"]),
                         ("9:16", "standard", 576, 1024, 6.0, 9.0, 3, "none", "auto"))
        (job,) = self.runner.jobs.values()
        self.assertEqual((job.tab, job.kind, job.params, js["job_id"]), ("short", "short.analyze", {"project_id": js["id"]}, job.id))
        self.assertEqual(self.ledger.get_mv_project(js["id"])["kind"], "short")
        self.assertTrue((self.root / "short" / js["id"]).is_dir())
        self.assertEqual(self.client.get(f"/api/short/{js['id']}").json()["id"], js["id"])

    def test_create_speech_and_music(self):
        speech = self.create(story="", narration="こんにちは。", speaker_id=4, images=[self.scenery(), self.person()], song_id=self.song["id"])
        self.assertEqual((speech["params"]["timeline"], speech["params"]["speaker_id"], speech["params"]["song_id"]), ("speech", 4, self.song["id"]))
        self.assertEqual([i["role"] for i in speech["params"]["images"]], ["person", "scenery"])                 # 人物が先頭
        self.assertEqual(speech["params"]["images"][0]["description"], "a girl with silver hair")
        music = self.create(story="", song_id=self.song["id"])
        self.assertEqual((music["params"]["timeline"], music["params"]["length_s"]), ("music", None))

    def test_validation_errors_are_400_with_japanese_messages(self):
        for extra, word in (({"story": ""}, "物語・映像のイメージを書いてください"), ({"length_s": 10}, "長さは 30〜3600 秒"),
                            ({"story": "", "narration": "話す。", "speaker_id": 1}, "口を合わせる人物の画像"),
                            ({"story": "", "narration": "話す。", "images": [self.person()]}, "話者を選んでください"),
                            ({"images": [self.person(), {"id": self.garden["id"], "role": "person"}]}, "人物の画像は 1 枚まで"),
                            ({"images": [self.scenery()] * 5}, "参照画像は 4 枚まで"), ({"images": [{"id": self.girl["id"], "role": "boss"}]}, "参照画像 1 の役割"),
                            ({"images": [{"id": "nope", "role": "scenery"}]}, "参照画像 1が見つかりません"), ({"song_id": "nope"}, "曲が見つかりません"),
                            ({"aspect": "2:1"}, "投稿先は"), ({"quality": "ultra"}, "画質は"), ({"min_s": 3}, "シーンの長さは 5〜10 秒"),
                            ({"final_audio": "mix"}, "最終の音声は auto / none のいずれかです")):
            r = self.client.post("/api/short", json=self.body(**extra))
            self.assertEqual(r.status_code, 400, extra)
            self.assertIn(word, r.json()["detail"], extra)
        self.assertEqual(self.runner.jobs, {})
        self.assertEqual(self.client.get("/api/short").json(), [])

    def test_list_and_view_are_separate_from_mv(self):
        mv_r = self.client.post("/api/mv", json={"song_id": self.song["id"], "image_id": self.girl["id"]})
        self.assertEqual(mv_r.status_code, 200, mv_r.text)
        mv_id, short_id = mv_r.json()["id"], self.create()["id"]
        self.assertEqual([i["id"] for i in self.client.get("/api/mv").json()], [mv_id])                         # MV の一覧にショート動画は混ざらない
        self.assertEqual([i["id"] for i in self.client.get("/api/short").json()], [short_id])
        self.assertEqual(self.client.get(f"/api/short/{mv_id}").status_code, 404)                                # 別の種類の ID は無い扱い
        self.assertEqual(self.client.get(f"/api/mv/{short_id}").status_code, 404)
        for path in ("pause", "resume", "cancel", "reassemble", "scenes/1/retry"):
            self.assertEqual(self.client.post(f"/api/short/{mv_id}/{path}").status_code, 404, path)
            self.assertEqual(self.client.post(f"/api/mv/{short_id}/{path}").status_code, 404, path)
        self.assertEqual(self.client.delete(f"/api/short/{mv_id}").status_code, 404)
        self.assertEqual(self.client.delete(f"/api/mv/{short_id}").status_code, 404)
        self.assertEqual(self.ledger.get_mv_project(mv_id)["kind"], "mv")                                       # 触られていない
        (item,) = self.client.get("/api/short").json()
        self.assertEqual((item["timeline"], item["final_audio"], item["scenes"], item["state"]), ("free", "auto", 0, "analyzing"))

    def test_list_newest_first_with_the_output_as_a_gallery_item_and_the_cover(self):
        first = self.finished(images=[self.scenery(), self.person()])
        second = self.create(images=[self.scenery(), self.person()])["id"]
        items = self.client.get("/api/short").json()
        self.assertEqual([i["id"] for i in items], [second, first])
        done, running = items[1], items[0]
        self.assertEqual((done["state"], done["scenes"], done["done"], done["progress"]), ("completed", 3, 3, 1.0))
        self.assertEqual((done["output"]["kind"], done["output"]["tab"], done["output"]["url"]), ("video", "short", f"/outputs/video/short_{first}.mp4"))
        self.assertNotIn("path", done["output"])
        self.assertEqual(running["thumb_url"], "/outputs/" + self.girl["thumb_path"])                           # 動画ができるまでは、先頭(人物)の画像
        self.assertNotIn("prompt", str(items))
        self.assertEqual(len(self.client.get("/api/short?limit=1").json()), 1)

    def test_get_shows_all_params_scenes_and_404s(self):
        pid = self.finished()
        js = self.client.get(f"/api/short/{pid}").json()
        self.assertEqual((js["state"], js["progress"], js["assembled"], js["job_id"], js["title"]), ("completed", 1.0, True, None, "雨の街の物語"))
        self.assertEqual([s["index"] for s in js["scenes"]], [1, 2, 3])
        self.assertEqual((js["scenes"][0]["prompt"], js["scenes"][0]["place"], js["scenes"][0]["refs"], js["scenes"][0]["speech"]), ("prompt 1", "p1", [], False))
        for key in ("kind", "story", "song_id", "narration", "speaker_id", "images", "length_s", "timeline", "final_audio", "character", "character_source"):
            self.assertIn(key, js["params"])
        self.assertEqual(js["output"]["url"], f"/outputs/video/short_{pid}.mp4")
        r = self.client.get("/api/short/nope")
        self.assertEqual((r.status_code, r.json()["detail"]), (404, "そのショート動画の制作は見つかりません(削除されたかもしれません)"))
        for method, path in (("post", "/api/short/nope/pause"), ("post", "/api/short/nope/resume"), ("post", "/api/short/nope/cancel"),
                             ("post", "/api/short/nope/reassemble"), ("post", "/api/short/nope/scenes/1/retry"), ("delete", "/api/short/nope")):
            self.assertEqual(getattr(self.client, method)(path).status_code, 404, path)

    # ---- 操作 ----

    def test_pause_resume_cancel_retry(self):
        pid = self.create()["id"]
        self.assertEqual(self.client.post(f"/api/short/{pid}/pause").json()["state"], "paused")
        self.assertEqual(self.client.post(f"/api/short/{pid}/resume").json()["state"], "analyzing")
        self.assertEqual(self.client.post(f"/api/short/{pid}/cancel").json()["state"], "cancelled")
        done = self.finished(state="paused", statuses=("done", "failed", "pending"), assembled=False, output=False)
        r = self.client.post(f"/api/short/{done}/scenes/2/retry", json={"prompt": "new prompt", "seed": 5})
        self.assertEqual(r.status_code, 200, r.text)
        s = self.ledger.get_mv_project(done)["scenes"][1]
        self.assertEqual((s["prompt"], s["seed"], s["status"]), ("new prompt", 5, "running"))                  # 再開で、このシーンから始まる
        self.assertEqual([j.kind for j in self.runner.jobs.values()], ["short.scene"])
        self.assertEqual(self.client.post(f"/api/short/{done}/scenes/9/retry").status_code, 400)

    def test_reassemble_without_a_body_keeps_the_setting(self):
        pid = self.finished()
        r = self.client.post(f"/api/short/{pid}/reassemble")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["state"], self.ledger.get_mv_project(pid)["params"]["final_audio"]), ("assembling", "auto"))
        (job,) = self.runner.jobs.values()
        self.assertEqual((job.tab, job.kind), ("short", "short.assemble"))
        self.assertEqual(self.client.post(f"/api/short/{pid}/reassemble").status_code, 400)                    # 結合中は断る

    def test_reassemble_with_final_audio_writes_it_to_the_project_first(self):
        pid = self.finished()
        for body, expected in (({"final_audio": "none"}, "none"), ({"final_audio": None}, "none"), ({}, "none"), ({"final_audio": "auto"}, "auto")):
            self.runner.jobs.clear()
            self.ledger.update_mv_project(pid, state="completed", params_patch={"assembled": True})
            main.app.state.short._active.clear()
            r = self.client.post(f"/api/short/{pid}/reassemble", json=body)
            self.assertEqual(r.status_code, 200, (body, r.text))
            self.assertEqual(self.ledger.get_mv_project(pid)["params"]["final_audio"], expected, body)
            self.assertEqual(r.json()["params"]["final_audio"], expected)

    def test_reassemble_errors_do_not_change_the_setting(self):
        pid = self.finished()
        r = self.client.post(f"/api/short/{pid}/reassemble", json={"final_audio": "mix"})
        self.assertEqual((r.status_code, "最終の音声は auto / none のいずれかです" in r.json()["detail"]), (400, True))
        part = self.finished(state="paused", statuses=("done", "pending"), assembled=False, output=False)
        r = self.client.post(f"/api/short/{part}/reassemble", json={"final_audio": "none"})
        self.assertEqual((r.status_code, "全部のシーンができてから結合できます" in r.json()["detail"]), (400, True))
        self.assertEqual(self.ledger.get_mv_project(part)["params"]["final_audio"], "auto")
        self.assertEqual(self.ledger.get_mv_project(pid)["params"]["final_audio"], "auto")

    def test_mv_reassemble_ignores_a_body_and_still_works(self):
        mv_r = self.client.post("/api/mv", json={"song_id": self.song["id"], "image_id": self.girl["id"]})
        pid = mv_r.json()["id"]
        scenes = mv.make_scenes(quantize_scene_plan([{"index": 1, "start": 0.0, "end": 8.0, "duration": 8.0, "vocal_ratio": 0.8, "kind": "vocal"}]))
        scenes[0].update(status="done", prompt="p", seed=1, clip="scene_001.mp4")
        self.runner.jobs.clear()
        self.ledger.update_mv_project(pid, state="completed", scenes=scenes, params_patch={"analysis": {"duration": 8.0}, "assembled": True})
        main.app.state.mv._active.clear()
        r = self.client.post(f"/api/mv/{pid}/reassemble", json={"final_audio": "none"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("final_audio", self.ledger.get_mv_project(pid)["params"])
        self.assertEqual([j.kind for j in self.runner.jobs.values()], ["mv.assemble"])

    def test_delete_keeps_the_final_video_and_removes_the_work_dir(self):
        pid = self.finished()
        work = self.root / "short" / pid
        work.mkdir(parents=True, exist_ok=True)
        (work / "narration.wav").write_bytes(b"x")
        out_id = self.ledger.get_mv_project(pid)["output_id"]
        r = self.client.delete(f"/api/short/{pid}")
        self.assertEqual((r.status_code, r.json()), (200, {"ok": True}))
        self.assertFalse(work.exists())
        self.assertIsNone(self.ledger.get_mv_project(pid))
        self.assertIsNotNone(self.ledger.get_output(out_id))                                                  # ギャラリーの動画は残る
        self.assertEqual(self.client.delete(f"/api/short/{pid}").status_code, 404)

    def test_delete_with_output_removes_the_videos_too(self):
        pid = self.finished()
        out_id = self.ledger.get_mv_project(pid)["output_id"]
        js = self.client.delete(f"/api/short/{pid}?with_output=1").json()
        self.assertEqual(js, {"ok": True, "deleted": [out_id], "missing": [], "refused": []})
        self.assertIsNone(self.ledger.get_output(out_id))

    def test_reference_images_of_a_live_project_cannot_be_deleted_from_the_gallery(self):
        pid = self.create(images=[self.person(), self.scenery()])["id"]
        for oid in (self.girl["id"], self.garden["id"]):
            r = self.client.delete(f"/api/gallery/{oid}")
            self.assertEqual(r.status_code, 409, r.text)
            self.assertIn("ショート動画の制作", r.json()["detail"])
        self.assertIsNotNone(self.ledger.get_output(self.girl["id"]))
        self.client.delete(f"/api/short/{pid}")
        self.assertEqual(self.client.delete(f"/api/gallery/{self.girl['id']}").status_code, 200)

    # ---- ナレーションの案 ----

    def test_describe_narration_returns_a_japanese_draft(self):
        self.llm.replies.append("朝の駅に、少女が立っていた。電車はまだ来ない。")
        r = self.client.post("/api/short/describe/narration", json={"story": "朝の駅で待つ少女"})
        self.assertEqual((r.status_code, r.json()), (200, {"text": "朝の駅に、少女が立っていた。電車はまだ来ない。"}))
        self.assertIn("朝の駅で待つ少女", self.llm.chats[0])
        self.assertNotIn("BACKGROUND MUSIC", self.llm.chats[0])
        self.assertEqual(self.runner.jobs, {})                                                                 # 制作は作らない

    def test_describe_narration_uses_the_song_concept_when_given(self):
        self.llm.replies.append("切ない夜明けの物語。")
        r = self.client.post("/api/short/describe/narration", json={"story": "朝の駅", "song_id": self.song["id"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("BACKGROUND MUSIC", self.llm.chats[0])
        self.assertIn("夜明けの駅", self.llm.chats[0])
        r = self.client.post("/api/short/describe/narration", json={"story": "朝の駅", "song_id": "nope"})
        self.assertEqual((r.status_code, "曲が見つかりません" in r.json()["detail"]), (400, True))

    def test_describe_narration_errors(self):
        r = self.client.post("/api/short/describe/narration", json={"story": "  "})
        self.assertEqual((r.status_code, "物語の文章を書いてください" in r.json()["detail"]), (400, True))
        self.llm.replies.append(LlmError("LLM が起動していません"))
        r = self.client.post("/api/short/describe/narration", json={"story": "物語"})
        self.assertEqual((r.status_code, r.json()["detail"]), (503, "LLM が起動していません"))
        self.llm.replies.extend(["English.", "Still English."])
        r = self.client.post("/api/short/describe/narration", json={"story": "物語"})
        self.assertEqual((r.status_code, "日本語で答えませんでした" in r.json()["detail"]), (503, True))


if __name__ == "__main__":
    unittest.main()
