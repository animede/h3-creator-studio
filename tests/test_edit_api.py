import io
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient
from PIL import Image

from studio import editclip, main, media, upscale
from studio.edit import EditManager
from studio.ledger import Ledger
from tests.test_upscale import fake_status


def png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (10, 120, 200)).save(buf, "PNG")
    return buf.getvalue()


class StubRunner:
    """JobRunner の代わり(EditManager と _in_use が使う口だけ)。"""

    def __init__(self):
        self.jobs, self.cancelled = {}, []

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


class EditApiTest(unittest.TestCase):
    """編集の API。lifespan は使わず、必要な app.state だけを差し替える(EditManager は実物、JobRunner は StubRunner)。"""

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
        self.v1 = self.add("video", "v1.mp4", duration_s=10.0, width=1280, height=720)
        self.v2 = self.add("video", "v2.mp4", duration_s=8.0, width=640, height=360)
        self.img = self.add("image", "p.png")
        self.song = self.add("audio", "s.wav", duration_s=30.0)
        self.spare = self.add("video", "spare.mp4", duration_s=5.0, width=640, height=360)
        self.runner = StubRunner()
        self.saved = {k: getattr(main.app.state, k, None) for k in ("edit", "ledger", "jobs")}
        self.addCleanup(lambda: [setattr(main.app.state, k, v) for k, v in self.saved.items()])
        main.app.state.ledger, main.app.state.jobs = self.ledger, self.runner
        main.app.state.edit = EditManager(self.runner, self.ledger, self.root)
        self.client = TestClient(main.app)

    def add(self, kind, name, **kw) -> dict:
        p = self.root / "uploads" / name
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(png_bytes() if kind == "image" else b"fakemedia")
        return self.ledger.add_output(kind, p, "upload", filename=name, params={}, **kw)

    def state(self, **kw) -> dict:
        return {"clips": [{"id": self.v1["id"], "kind": "video", "in": 0, "out": 4}, {"id": self.v2["id"], "kind": "video", "in": 1, "out": 5},
                          {"id": self.img["id"], "kind": "image", "duration": 3}], **kw}

    def create(self, state=None, name="テスト") -> dict:
        r = self.client.post("/api/edit", json={"name": name, **({"state": state} if state else {})})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def put(self, pid, state):
        return self.client.put(f"/api/edit/{pid}/state", json={"state": state})

    # ---- 作る・見る ----

    def test_create_returns_the_view(self):
        js = self.create(self.state(transitions=[{"after": 0, "type": "crossfade", "duration": 1}]))
        self.assertEqual(set(js), {"id", "name", "created_at", "updated_at", "state", "duration", "timeline", "clips", "can_undo", "can_redo",
                                   "preview", "job", "output", "error"})
        self.assertEqual((js["name"], js["duration"], js["can_undo"], js["can_redo"], js["preview"], js["job"], js["output"]), ("テスト", 10.0, False, False, None, None, None))
        self.assertEqual(js["timeline"][1], {"index": 1, "start": 4.0, "end": 8.0})
        self.assertEqual([c["filename"] for c in js["clips"]], ["v1.mp4", "v2.mp4", "p.png"])
        self.assertTrue(all(c["missing"] is False for c in js["clips"]))
        got = self.client.get(f"/api/edit/{js['id']}").json()
        self.assertEqual(got["id"], js["id"])
        empty = self.create(name="")
        self.assertEqual((empty["name"], empty["state"]["clips"], empty["duration"]), ("無題の編集", [], 0.0))

    def test_create_and_get_errors(self):
        r = self.client.post("/api/edit", json={"name": "x", "state": {"clips": [{"id": "gone", "in": 0, "out": 1}]}})
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "クリップ 1の生成物が見つかりません(ギャラリーから削除されたかもしれません)"))
        r = self.client.get("/api/edit/nope")
        self.assertEqual((r.status_code, r.json()["detail"]), (404, "その編集プロジェクトは見つかりません(削除されたかもしれません)"))
        for method, path, body in (("put", "/state", {"state": {}}), ("post", "/undo", None), ("post", "/redo", None), ("post", "/preview", None),
                                   ("post", "/export", None), ("patch", "", {"name": "x"}), ("delete", "", None)):
            r = getattr(self.client, method)("/api/edit/nope" + path, **({"json": body} if body is not None else {}))
            self.assertEqual(r.status_code, 404, (method, path, r.text))

    def test_list(self):
        a = self.create(self.state(), "一本目")
        b = self.create(None, "二本目")
        rows = self.client.get("/api/edit").json()
        self.assertEqual([r["id"] for r in rows], [b["id"], a["id"]])
        self.assertEqual(set(rows[0]), {"id", "name", "updated_at", "duration", "clips", "thumb_url", "output"})
        self.assertEqual((rows[1]["clips"], rows[1]["duration"], rows[1]["output"]), (3, 11.0, None))

    # ---- state・元に戻す ----

    def test_put_state_validates_and_pushes_history(self):
        js = self.create(self.state())
        pid = js["id"]
        r = self.put(pid, self.state(fade_in=1.0, effects=[{"type": "mono", "start": 1, "end": 3}]))
        self.assertEqual(r.status_code, 200, r.text)
        view = r.json()
        self.assertEqual((view["state"]["fade_in"], view["can_undo"], view["can_redo"], view["job"]), (1.0, True, False, None))   # プレビューは自動では作らない
        self.assertEqual(self.runner.jobs, {})
        for state, message in ((self.state(effects=[{"type": "saturation", "value": 5}]), "効果 1の値(5)は 0〜3"),
                               ({"clips": [{"id": self.v1["id"], "in": 0, "out": 50}]}, "元の動画の長さ(10.00 秒)を超えています"),
                               (self.state(transitions=[{"after": 0, "type": "crossfade", "duration": 9}]), "0.5〜2 秒"),
                               (self.state(audio={"mode": "music"}), "曲を選んでください")):
            r = self.put(pid, state)
            self.assertEqual(r.status_code, 400, r.text)
            self.assertIn(message, r.json()["detail"])
        self.assertEqual(self.client.get(f"/api/edit/{pid}").json()["state"]["fade_in"], 1.0)             # 失敗は履歴に積まない
        self.assertEqual(self.client.put(f"/api/edit/{pid}/state", json={}).status_code, 422)

    def test_undo_redo_roundtrip(self):
        pid = self.create(self.state())["id"]
        self.put(pid, self.state(fade_in=1.0))
        self.put(pid, self.state(fade_in=2.0))
        r = self.client.post(f"/api/edit/{pid}/undo")
        self.assertEqual((r.status_code, r.json()["state"]["fade_in"], r.json()["can_undo"], r.json()["can_redo"]), (200, 1.0, True, True))
        self.client.post(f"/api/edit/{pid}/undo")
        r = self.client.post(f"/api/edit/{pid}/undo")
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "これ以上戻せません"))
        r = self.client.post(f"/api/edit/{pid}/redo")
        self.assertEqual((r.json()["state"]["fade_in"], r.json()["can_redo"]), (1.0, True))
        self.client.post(f"/api/edit/{pid}/redo")
        r = self.client.post(f"/api/edit/{pid}/redo")
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "これ以上やり直せません"))
        self.put(pid, self.state(fade_in=3.0))                                                        # 戻ったあとの新しい操作で、やり直しは消える
        self.client.post(f"/api/edit/{pid}/undo")
        self.client.post(f"/api/edit/{pid}/undo")
        self.client.post(f"/api/edit/{pid}/redo")
        self.assertEqual(self.client.post(f"/api/edit/{pid}/redo").json()["state"]["fade_in"], 3.0)
        self.assertEqual(self.client.post(f"/api/edit/{pid}/redo").status_code, 400)

    # ---- プレビュー・書き出し ----

    def test_preview_submits_and_replaces_running_job(self):
        pid = self.create(self.state())["id"]
        r = self.client.post(f"/api/edit/{pid}/preview")
        self.assertEqual(r.status_code, 200, r.text)
        job = self.runner.jobs["job1"]
        self.assertEqual((job.tab, job.kind, job.params["project_id"], job.params["seq"]), ("edit", "edit.preview", pid, 1))
        self.assertEqual(r.json()["job"], {"id": "job1", "kind": "edit.preview", "status": "queued", "progress": 0.0, "message": "順番を待っています", "error": None})
        job.status, job.progress, job.message = "running", 0.4, "プレビューを作っています 40%"
        self.assertEqual(self.client.get(f"/api/edit/{pid}").json()["job"]["progress"], 0.4)
        r = self.client.post(f"/api/edit/{pid}/preview")
        self.assertEqual((self.runner.cancelled, r.json()["job"]["id"]), (["job1"], "job2"))
        self.assertEqual(self.runner.jobs["job2"].params["seq"], 2)

    def test_preview_of_empty_project_is_400(self):
        pid = self.create()["id"]
        r = self.client.post(f"/api/edit/{pid}/preview")
        self.assertEqual(r.status_code, 400)
        self.assertIn("クリップがありません", r.json()["detail"])

    def test_export_conflict_is_409(self):
        pid = self.create(self.state())["id"]
        r = self.client.post(f"/api/edit/{pid}/export")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["job"]["kind"], self.runner.jobs["job1"].params["state"]["clips"][0]["id"]), ("edit.export", self.v1["id"]))
        r = self.client.post(f"/api/edit/{pid}/export")
        self.assertEqual(r.status_code, 409)
        self.assertIn("書き出しの途中", r.json()["detail"])
        self.runner.jobs["job1"].status = "completed"
        self.assertEqual(self.client.post(f"/api/edit/{pid}/export").status_code, 200)

    def test_export_with_upscale(self):
        pid = self.create(self.state())["id"]
        with mock.patch.object(upscale, "status", return_value=fake_status(realesrgan=True, rtxvsr=False)):
            r = self.client.post(f"/api/edit/{pid}/export", json={"upscale": {"method": "auto", "scale": 2}})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["job"]["kind"], "edit.export")
            self.assertEqual(self.runner.jobs["job1"].params["upscale"], {"method": "realesrgan", "scale": 2, "width": 2560, "height": 1440})
            self.runner.jobs["job1"].status = "completed"
            r = self.client.post(f"/api/edit/{pid}/export", json={"upscale": {"method": "rtxvsr", "scale": 2}})
            self.assertEqual((r.status_code, r.json()["detail"]), (400, "GPU の条件"))                     # 使えない方式は、その理由を日本語で
            r = self.client.post(f"/api/edit/{pid}/export", json={"upscale": {"method": "realesrgan", "scale": 4}})
            self.assertEqual(r.status_code, 400)
            self.assertEqual(r.json()["detail"], "拡大後の長辺が 3840px を超えます(5120×2880)。大きさを下げてください")
            r = self.client.post(f"/api/edit/{pid}/export", json={"upscale": {"method": "realesrgan", "scale": 4}}, headers={"Accept-Language": "en"})
            self.assertEqual(r.json()["detail"], "The long edge after upscaling exceeds 3840px (5120×2880). Choose a smaller size")
            r = self.client.post(f"/api/edit/{pid}/export", json={"upscale": {"method": "auto", "scale": 1}})     # 倍率 1 は拡大しない
            self.assertEqual(r.status_code, 200, r.text)
            self.assertNotIn("upscale", self.runner.jobs["job2"].params)
            self.runner.jobs["job2"].status = "completed"
            self.assertEqual(self.client.post(f"/api/edit/{pid}/export", json={}).status_code, 200)        # 空の body も、body なしも、前と同じ
            self.runner.jobs["job3"].status = "completed"
            self.assertEqual(self.client.post(f"/api/edit/{pid}/export").status_code, 200)
            self.assertNotIn("upscale", self.runner.jobs["job4"].params)
        with mock.patch.object(upscale, "status", return_value=fake_status(realesrgan=False, rtxvsr=False)):
            self.runner.jobs["job4"].status = "completed"
            r = self.client.post(f"/api/edit/{pid}/export", json={"upscale": {"method": "auto", "scale": 2}})
            self.assertEqual(r.status_code, 400)
            self.assertEqual(r.json()["detail"], "拡大の方式が入っていません。拡大の欄の「入れる」で Real-ESRGAN か RTX Video Super Resolution を入れてください")
        self.assertEqual(len(self.runner.jobs), 4)

    def test_view_shows_stale_preview_and_public_output(self):
        pid = self.create(self.state())["id"]
        state = self.client.get(f"/api/edit/{pid}").json()["state"]
        d = self.root / "edit" / pid
        d.mkdir(parents=True)
        (d / "preview_1.mp4").write_bytes(b"mp4")
        self.ledger.update_edit_project(pid, preview={"n": 1, "hash": editclip.state_hash(state), "path": f"edit/{pid}/preview_1.mp4"})
        dest = self.root / "video" / f"edit_{pid}.mp4"
        dest.parent.mkdir()
        dest.write_bytes(b"mp4")
        rec = self.ledger.add_output("video", dest, "edit", "j1", "テスト_edit.mp4", {"project_id": pid, "state": state, "name": "テスト"},
                                     width=1280, height=720, duration_s=11.0)
        self.ledger.update_edit_project(pid, output_id=rec["id"])
        js = self.client.get(f"/api/edit/{pid}").json()
        self.assertEqual(js["preview"], {"url": f"/outputs/edit/{pid}/preview_1.mp4", "stale": False})
        out = js["output"]
        self.assertEqual((out["url"], out["filename"], out["width"], out["height"], out["duration_s"], out["params"]["project_id"]),
                         (f"/outputs/video/edit_{pid}.mp4", "テスト_edit.mp4", 1280, 720, 11.0, pid))
        self.assertNotIn("path", out)
        self.assertIn("thumb_url", out)
        self.assertTrue(self.client.put(f"/api/edit/{pid}/state", json={"state": {**state, "fade_out": 1.0}}).json()["preview"]["stale"])
        row = self.client.get("/api/edit").json()[0]
        self.assertEqual(row["output"]["url"], f"/outputs/video/edit_{pid}.mp4")

    # ---- 名前・削除・probe ----

    def test_rename_and_delete(self):
        pid = self.create(self.state())["id"]
        r = self.client.patch(f"/api/edit/{pid}", json={"name": "改名"})
        self.assertEqual((r.status_code, r.json()["name"]), (200, "改名"))
        r = self.client.patch(f"/api/edit/{pid}", json={"name": "  "})
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "名前を入力してください"))
        d = self.root / "edit" / pid
        d.mkdir(parents=True)
        (d / "preview_1.mp4").write_bytes(b"x")
        self.client.post(f"/api/edit/{pid}/preview")
        r = self.client.delete(f"/api/edit/{pid}")
        self.assertEqual((r.status_code, r.json()), (200, {"ok": True}))
        self.assertFalse(d.exists())
        self.assertEqual(self.runner.cancelled, ["job1"])
        self.assertEqual(self.client.get(f"/api/edit/{pid}").status_code, 404)
        self.assertIsNotNone(self.ledger.get_output(self.v1["id"]))                                    # 素材はギャラリーに残る

    def test_probe(self):
        r = self.client.post("/api/edit/probe", json={"id": self.v1["id"]})
        self.assertEqual((r.status_code, r.json()), (200, {"kind": "video", "length_s": 10.0, "width": 1280, "height": 720, "filename": "v1.mp4", "thumb_url": None}))
        r = self.client.post("/api/edit/probe", json={"id": self.img["id"]})
        self.assertEqual((r.json()["kind"], r.json()["length_s"], r.json()["thumb_url"]), ("image", None, "/outputs/" + self.img["thumb_path"]))
        self.assertEqual(self.client.post("/api/edit/probe", json={"id": self.song["id"]}).json()["length_s"], 30.0)
        r = self.client.post("/api/edit/probe", json={"id": "nope"})
        self.assertEqual(r.status_code, 404)

    # ---- ギャラリーの削除の保護 ----

    def test_gallery_delete_is_protected_for_clips_and_song(self):
        pid = self.create(self.state(audio={"mode": "music", "song_id": self.song["id"]}))["id"]
        r = self.client.post("/api/gallery/delete", json={"ids": [self.v1["id"], self.img["id"], self.song["id"], self.spare["id"]]})
        js = r.json()
        self.assertEqual(js["deleted"], [self.spare["id"]])                                             # 使われていないものだけ消える
        self.assertEqual({x["id"]: x["reason"] for x in js["refused"]},
                         {oid: main.EDIT_IN_USE for oid in (self.v1["id"], self.img["id"], self.song["id"])})
        r = self.client.delete(f"/api/gallery/{self.v2['id']}")
        self.assertEqual((r.status_code, r.json()["detail"]), (409, main.EDIT_IN_USE))
        self.assertIn("編集プロジェクト", main.EDIT_IN_USE)
        # state から外せば消せる(履歴に残っていても、いまの state が使っていなければよい)
        self.put(pid, {"clips": [{"id": self.v1["id"], "in": 0, "out": 4}]})
        self.assertEqual(self.client.delete(f"/api/gallery/{self.v2['id']}").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/gallery/{self.v1['id']}").status_code, 409)
        self.client.delete(f"/api/edit/{pid}")                                                          # プロジェクトを消せば消せる
        self.assertEqual(self.client.delete(f"/api/gallery/{self.v1['id']}").status_code, 200)

    def test_gallery_delete_protects_ids_in_pending_edit_jobs(self):
        pid = self.create(self.state())["id"]
        self.client.post(f"/api/edit/{pid}/export")                                                      # ジョブの params にも state の写しが入る
        self.put(pid, {"clips": [{"id": self.v2["id"], "in": 0, "out": 4}]})
        r = self.client.delete(f"/api/gallery/{self.v1['id']}")
        self.assertEqual((r.status_code, r.json()["detail"]), (409, main.JOB_IN_USE))


if __name__ == "__main__":
    unittest.main()
