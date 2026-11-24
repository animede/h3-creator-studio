import io
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient
from PIL import Image

from studio import main, media, mv, mv_audio
from studio.ledger import Ledger
from studio.mvclip import quantize_scene_plan


def png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (10, 120, 200)).save(buf, "PNG")
    return buf.getvalue()


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


def plan_of(durations) -> list:
    t, out = 0.0, []
    for i, d in enumerate(durations, start=1):
        out.append({"index": i, "start": t, "end": t + d, "duration": d, "vocal_ratio": 0.8, "kind": "vocal"})
        t += d
    return out


class MvApiTest(unittest.TestCase):
    """MV の API。lifespan は使わず、必要な app.state だけを差し替える(Director は実物、JobRunner は StubRunner)。"""

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
        self.image = self.add("image", "girl.png", {"prompt": "a girl with silver hair"})
        self.runner = StubRunner()
        self.saved = getattr(main.app.state, "mv", None)
        self.addCleanup(lambda: setattr(main.app.state, "mv", self.saved))
        main.app.state.mv = mv.Director(self.runner, self.ledger, self.root)
        self.client = TestClient(main.app)

    def add(self, kind, name, params):
        p = self.root / "uploads" / name
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(png_bytes() if kind == "image" else b"RIFFfake")
        return self.ledger.add_output(kind, p, "upload", filename=name, params=params, duration_s=30.0 if kind == "audio" else None)

    def body(self, **kw) -> dict:
        return {"song_id": self.song["id"], "image_id": self.image["id"], **kw}

    def create(self, **kw) -> dict:
        r = self.client.post("/api/mv", json=self.body(**kw))
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def with_scenes(self, state="paused", statuses=("done", "done", "done"), assembled=True, output=True) -> str:
        """分析・演出まで済んだ制作を台帳に作る(各シーンの状態は statuses)。"""
        pid = self.create()["id"]
        scenes = mv.make_scenes(quantize_scene_plan(plan_of([8.0, 6.0, 7.0][:len(statuses)])))
        for s, st in zip(scenes, statuses):
            s.update(status=st, prompt=f"prompt {s['index']}", seed=1000 + s["index"], clip=mv.scene_clip_name(s["index"]) if st == "done" else None)
        fields = {}
        if output:
            dest = self.root / "video" / f"mv_{pid}.mp4"
            dest.parent.mkdir(exist_ok=True)
            dest.write_bytes(b"mp4")
            fields["output_id"] = self.ledger.add_output("video", dest, "mv", None, "x_mv.mp4", {"project_id": pid}, width=768, height=448, duration_s=21.0)["id"]
        self.runner.jobs.clear()
        self.ledger.update_mv_project(pid, state=state, scenes=scenes, params_patch={"analysis": {"duration": 21.0}, "assembled": assembled}, **fields)
        return pid

    # ---- tools ----

    def test_tools(self):
        with mock.patch.object(mv_audio, "tools_python", return_value=Path("/x/python")), mock.patch.object(main.shutil, "which", return_value="/usr/bin/ffmpeg"):
            self.assertEqual(self.client.get("/api/mv/tools").json(), {"available": True, "hint": None})
        missing = mv_audio.MvToolsMissing(f"MV の音声処理の道具(Demucs と librosa)が見つかりません: /x。{mv_audio.MISSING_HINT}")
        with mock.patch.object(mv_audio, "tools_python", side_effect=missing):
            js = self.client.get("/api/mv/tools").json()
        self.assertFalse(js["available"])
        self.assertIn("setup --mv を実行してください", js["hint"])
        with mock.patch.object(mv_audio, "tools_python", return_value=Path("/x/python")), mock.patch.object(main.shutil, "which", return_value=None):
            js = self.client.get("/api/mv/tools").json()
        self.assertEqual((js["available"], js["hint"]), (False, "ffmpeg が見つかりません(PATH に入れてください)"))

    # ---- 作る・見る ----

    def test_create_returns_the_view_and_submits_the_first_job(self):
        js = self.create(character="a girl", aspect="9:16", quality="standard", seed=3, min_s=6, max_s=9)
        self.assertEqual((js["state"], js["error"], js["scenes"], js["output"], js["job_status"]), ("analyzing", None, [], None, "queued"))
        self.assertEqual(js["params"], {"song_id": self.song["id"], "image_id": self.image["id"], "character": "a girl",
                                        "concept": "夜明けの駅\nBallad", "aspect": "9:16", "quality": "standard", "width": 576, "height": 1024,
                                        "min_s": 6.0, "max_s": 9.0, "seed": 3, "background_text": "auto"})
        (job,) = self.runner.jobs.values()
        self.assertEqual((job.tab, job.kind, job.params, js["job_id"]), ("mv", "mv.analyze", {"project_id": js["id"]}, job.id))
        self.assertEqual(self.client.get(f"/api/mv/{js['id']}").json()["id"], js["id"])
        self.assertTrue((self.root / "mv" / js["id"]).is_dir())              # 作業フォルダ
        defaults = self.create()                                              # 既定: 横長 16:9・高速・5〜10 秒
        self.assertEqual((defaults["params"]["aspect"], defaults["params"]["quality"], defaults["params"]["width"], defaults["params"]["height"],
                          defaults["params"]["min_s"], defaults["params"]["max_s"]), ("16:9", "fast", 768, 448, 5.0, 10.0))

    def test_validation_errors_are_400_with_japanese_messages(self):
        for extra, word in (({"song_id": ""}, "曲を選んでください"), ({"song_id": "nope"}, "曲が見つかりません"),
                            ({"song_id": self.image["id"]}, "曲には音声を使ってください"), ({"image_id": ""}, "キャラクター画像を選んでください"),
                            ({"image_id": self.song["id"]}, "キャラクター画像には画像を使ってください"),
                            ({"aspect": "2:1"}, "投稿先は 16:9 / 9:16 / 1:1 / 4:5 のいずれかです"), ({"quality": "ultra"}, "画質は fast / standard / high"),
                            ({"min_s": 3}, "シーンの長さは 5〜10 秒"), ({"max_s": 12}, "シーンの長さは 5〜10 秒"),
                            ({"min_s": 9, "max_s": 6}, "シーンの最短は最長より長くできません")):
            r = self.client.post("/api/mv", json=self.body(**extra))
            self.assertEqual(r.status_code, 400, extra)
            self.assertIn(word, r.json()["detail"], extra)
        self.assertEqual(self.runner.jobs, {})                                # 断ったら何も始めない
        self.assertEqual(self.client.get("/api/mv").json(), [])

    def test_list_newest_first_with_the_final_output_as_a_gallery_item(self):
        first = self.with_scenes(state="completed")
        second = self.create()["id"]
        items = self.client.get("/api/mv").json()
        self.assertEqual([i["id"] for i in items], [second, first])
        done, running = items[1], items[0]
        self.assertEqual((done["state"], done["scenes"], done["done"], done["progress"]), ("completed", 3, 3, 1.0))
        self.assertEqual((done["output"]["kind"], done["output"]["tab"], done["output"]["url"]), ("video", "mv", f"/outputs/video/mv_{first}.mp4"))
        self.assertNotIn("path", done["output"])                              # ギャラリーと同じ形(path は出さない)
        self.assertEqual((running["state"], running["output"], running["scenes"]), ("analyzing", None, 0))
        self.assertEqual(running["thumb_url"], "/outputs/" + self.image["thumb_path"])
        self.assertNotIn("prompt", str(items))                                # 一覧にシーンの中身は含めない
        self.assertEqual(len(self.client.get("/api/mv?limit=1").json()), 1)

    def test_get_shows_scenes_and_the_output_and_404s(self):
        pid = self.with_scenes(state="completed")
        js = self.client.get(f"/api/mv/{pid}").json()
        self.assertEqual((js["state"], js["progress"], js["assembled"], js["job_id"]), ("completed", 1.0, True, None))
        self.assertEqual([s["index"] for s in js["scenes"]], [1, 2, 3])
        self.assertEqual(js["scenes"][0]["prompt"], "prompt 1")
        self.assertEqual(js["output"]["url"], f"/outputs/video/mv_{pid}.mp4")
        r = self.client.get("/api/mv/nope")
        self.assertEqual((r.status_code, r.json()["detail"]), (404, "その MV の制作は見つかりません(削除されたかもしれません)"))
        for method, path in (("post", "/api/mv/nope/pause"), ("post", "/api/mv/nope/resume"), ("post", "/api/mv/nope/cancel"),
                             ("post", "/api/mv/nope/reassemble"), ("post", "/api/mv/nope/scenes/1/retry"), ("delete", "/api/mv/nope")):
            self.assertEqual(getattr(self.client, method)(path).status_code, 404, path)

    # ---- 操作 ----

    def test_pause_resume_cancel(self):
        pid = self.create()["id"]
        r = self.client.post(f"/api/mv/{pid}/pause")                          # まだ始まっていない(順番待ちの)ジョブは、取り下げる
        self.assertEqual((r.status_code, r.json()["state"]), (200, "paused"))
        self.assertEqual(self.runner.cancelled, ["job1"])
        r = self.client.post(f"/api/mv/{pid}/resume")                         # 再開: 新しいジョブを入れて続ける
        self.assertEqual((r.status_code, r.json()["state"]), (200, "analyzing"))
        self.assertEqual([j.kind for j in self.runner.jobs.values()], ["mv.analyze", "mv.analyze"])
        self.runner.jobs["job2"].status = "running"                           # 走り出したジョブは、一時停止しても取り下げず、終わるまで続ける
        r = self.client.post(f"/api/mv/{pid}/pause")
        self.assertEqual((r.json()["state"], self.runner.cancelled), ("paused", ["job1"]))
        self.assertIn("いまの仕事が終わると止まります", r.json()["message"])
        r = self.client.post(f"/api/mv/{pid}/resume")                         # 止まる前に再開: 走っているジョブの続き(新しいジョブは入れない)
        self.assertEqual((r.status_code, r.json()["state"], len(self.runner.jobs)), (200, "analyzing", 2))
        r = self.client.post(f"/api/mv/{pid}/cancel")
        self.assertEqual((r.status_code, r.json()["state"], r.json()["message"]), (200, "cancelled", "中止しました"))
        self.assertEqual(self.runner.cancelled, ["job1", "job2"])
        r = self.client.post(f"/api/mv/{pid}/resume")                         # 中止した制作も、続きから
        self.assertEqual((r.status_code, r.json()["state"]), (200, "analyzing"))
        self.assertEqual([j.kind for j in self.runner.jobs.values()], ["mv.analyze"] * 3)

    def test_state_errors_are_400(self):
        pid = self.with_scenes(state="completed")
        for path, word in ((f"/api/mv/{pid}/pause", "一時停止できるのは"), (f"/api/mv/{pid}/resume", "再開できるのは"),
                           (f"/api/mv/{pid}/cancel", "終わった制作は中止できません")):
            r = self.client.post(path)
            self.assertEqual(r.status_code, 400, path)
            self.assertIn(word, r.json()["detail"])

    def test_retry_scene_with_and_without_a_body(self):
        pid = self.with_scenes(state="completed")
        r = self.client.post(f"/api/mv/{pid}/scenes/2/retry", json={"prompt": "an edited prompt", "seed": 42})
        self.assertEqual(r.status_code, 200, r.text)
        js = r.json()
        self.assertEqual((js["state"], js["assembled"]), ("generating", False))
        self.assertEqual([(s["index"], s["status"]) for s in js["scenes"]], [(1, "done"), (2, "running"), (3, "done")])
        self.assertEqual((js["scenes"][1]["prompt"], js["scenes"][1]["seed"], js["scenes"][1]["clip_url"]), ("an edited prompt", 42, None))
        (job,) = self.runner.jobs.values()
        self.assertEqual((job.kind, job.params), ("mv.scene", {"project_id": pid, "scene": 2}))
        pid2 = self.with_scenes(state="paused")
        r = self.client.post(f"/api/mv/{pid2}/scenes/1/retry")                # 本文なし: seed を取り直す
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotEqual(r.json()["scenes"][0]["seed"], 1001)
        self.assertEqual(r.json()["scenes"][0]["prompt"], "prompt 1")

    def test_retry_errors(self):
        pid = self.with_scenes(state="completed")
        r = self.client.post(f"/api/mv/{pid}/scenes/9/retry")
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "シーン 9 はありません"))
        r = self.client.post(f"/api/mv/{pid}/scenes/1/retry", json={"prompt": "   "})
        self.assertEqual(r.status_code, 400)
        self.assertIn("プロンプトは 1〜8000 文字", r.json()["detail"])
        self.assertEqual(self.client.post(f"/api/mv/{pid}/scenes/1/retry", json={"seed": 1.5}).status_code, 422)      # 整数でない seed は型の検証で断る
        early = self.create()["id"]                                           # 演出がまだ
        r = self.client.post(f"/api/mv/{early}/scenes/1/retry")
        self.assertEqual(r.status_code, 400)
        self.assertIn("演出ができていない", r.json()["detail"])
        self.assertEqual(self.client.get(f"/api/mv/{pid}").json()["scenes"][0]["status"], "done")        # 断ったら変えない
        self.assertEqual(len(self.runner.jobs), 1)                            # early の分析だけ

    def test_reassemble(self):
        pid = self.with_scenes(state="completed")
        r = self.client.post(f"/api/mv/{pid}/reassemble")
        self.assertEqual((r.status_code, r.json()["state"], r.json()["assembled"]), (200, "assembling", False))
        (job,) = self.runner.jobs.values()
        self.assertEqual((job.kind, job.params), ("mv.assemble", {"project_id": pid}))
        r = self.client.post(f"/api/mv/{pid}/reassemble")                     # 結合中は断る
        self.assertEqual(r.status_code, 400)
        self.assertIn("制作中", r.json()["detail"])
        pid2 = self.with_scenes(state="failed", statuses=("done", "failed", "pending"))
        r = self.client.post(f"/api/mv/{pid2}/reassemble")
        self.assertEqual(r.status_code, 400)
        self.assertIn("全部のシーンができてから", r.json()["detail"])

    def test_delete_keeps_the_final_mv_in_the_gallery(self):
        pid = self.with_scenes(state="completed")
        out_id = self.ledger.get_mv_project(pid)["output_id"]
        (self.root / "mv" / pid).mkdir(parents=True, exist_ok=True)
        (self.root / "mv" / pid / "scene_001.mp4").write_bytes(b"clip")
        r = self.client.delete(f"/api/mv/{pid}")
        self.assertEqual((r.status_code, r.json()), (200, {"ok": True}))
        self.assertFalse((self.root / "mv" / pid).exists())
        self.assertEqual(self.client.get(f"/api/mv/{pid}").status_code, 404)
        self.assertIsNotNone(self.ledger.get_output(out_id))
        self.assertEqual(self.client.delete(f"/api/mv/{pid}").status_code, 404)

    def test_jobs_api_does_not_take_mv_jobs(self):
        """MV のジョブは Director が入れる。/api/jobs の tab に mv は増やしていない(そのまま 422)。"""
        pid = self.create()["id"]
        r = self.client.post("/api/jobs", json={"tab": "mv", "kind": "mv.analyze", "params": {"project_id": pid}})
        self.assertEqual(r.status_code, 422)


class LifespanTest(unittest.IsolatedAsyncioTestCase):
    """管理役の起動(lifespan)で、Director が JobRunner につながり、途切れた制作が一時停止になる。部品の取り込みと GPU の監視は偽に差し替える。"""

    async def test_director_is_wired_and_unfinished_projects_are_paused(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        ledger = Ledger(root)
        scenes = mv.make_scenes(quantize_scene_plan(plan_of([8.0, 6.0])))
        scenes[0]["status"] = "running"
        ledger.create_mv_project("gen", "generating", {"title": "t"}, scenes, "シーン 1 / 2")
        ledger.create_mv_project("done", "completed", {"title": "t"}, [], "MV ができました")
        ledger.close()

        class FakeMonitor:
            def __init__(self, rss_provider=None, generating=None):
                pass

            def start(self):
                pass

            def stop(self):
                pass

            def snapshot(self):
                return {"gpu": None, "ram": None, "shared_gpu_mib": None, "warning": None}

        async def no_adopt(self):
            pass
        keys = ("sched", "ledger", "tts", "llm", "jobs", "mv", "monitor")          # lifespan が差し込む app.state は、終わったら元に戻す(他のテストに残さない)
        missing = object()
        saved = {k: getattr(main.app.state, k, missing) for k in keys}

        def restore():
            for k, v in saved.items():
                if v is missing:
                    if hasattr(main.app.state, k):
                        delattr(main.app.state, k)
                else:
                    setattr(main.app.state, k, v)
        self.addCleanup(restore)
        with mock.patch.object(main, "OUTPUTS_DIR", root), mock.patch.object(main, "GpuMonitor", FakeMonitor), \
                mock.patch.object(main.Scheduler, "adopt_running", no_adopt):
            async with main.lifespan(main.app):
                director, runner = main.app.state.mv, main.app.state.jobs
                self.assertIsInstance(director, mv.Director)
                self.assertIs(director.runner, runner)
                self.assertIn(director._on_finished, runner._finished_hooks)         # ジョブの終わりが Director に届く
                self.assertEqual(director.ledger, main.app.state.ledger)
                gen = main.app.state.ledger.get_mv_project("gen")
                self.assertEqual((gen["state"], gen["message"]), ("paused", "管理役の再起動で一時停止しました"))
                self.assertEqual(gen["scenes"][0]["status"], "pending")
                self.assertEqual(main.app.state.ledger.get_mv_project("done")["state"], "completed")
                self.assertEqual(runner.jobs, {})                                      # 勝手にジョブを入れない
                self.assertTrue({"mv.analyze", "mv.script", "mv.scene", "mv.assemble"} <= set(runner.handlers))


if __name__ == "__main__":
    unittest.main()
