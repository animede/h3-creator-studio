import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient
from PIL import Image

from studio import main, media
from studio.jobs import Job
from studio.ledger import Ledger

JOB_IN_USE = "実行中・順番待ちのジョブで使っています"
MV_IN_USE = "MV の制作(ID p1)で使っています。先に制作を削除してください"        # 題の無い制作は ID で示す


def png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (10, 120, 200)).save(buf, "PNG")
    return buf.getvalue()


class FakeRunner:
    """JobRunner の代わり(削除の確認が見る jobs だけ)。ジョブは、状態と params を決めて入れる。"""

    def __init__(self):
        self.jobs = {}

    def add(self, status: str, params: dict, kind: str = "h3.ref2va") -> Job:
        job = Job(id=f"job{len(self.jobs) + 1}", tab="video", kind=kind, params=params, status=status)
        self.jobs[job.id] = job
        return job


class GalleryDeleteTest(unittest.TestCase):
    """ギャラリーの削除の API。lifespan は使わず、必要な app.state だけを差し替える(台帳は実物、JobRunner は偽物)。"""

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
        self.runner = FakeRunner()
        self.saved = {k: getattr(main.app.state, k, None) for k in ("ledger", "jobs")}
        self.addCleanup(lambda: [setattr(main.app.state, k, v) for k, v in self.saved.items()])
        main.app.state.ledger, main.app.state.jobs = self.ledger, self.runner
        self.client = TestClient(main.app)

    def add(self, kind: str, name: str, params: dict = None) -> dict:
        p = self.root / kind / name
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(png_bytes() if kind == "image" else b"RIFFfake")
        return self.ledger.add_output(kind, p, {"image": "image", "video": "video", "audio": "music"}[kind], filename=name, params=params or {})

    def images(self, n: int) -> list:
        return [self.add("image", f"{i}.png") for i in range(n)]

    def exists(self, rec: dict) -> bool:
        """台帳の行・ファイル・サムネイルが全部残っているか(1 つでも残っていれば True)。"""
        return any([self.ledger.get_output(rec["id"]), (self.root / rec["path"]).exists(),
                    rec["thumb_path"] and (self.root / rec["thumb_path"]).exists()])

    def delete_many(self, ids) -> dict:
        r = self.client.post("/api/gallery/delete", json={"ids": list(ids)})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    # ---- まとめて消す ----

    def test_bulk_delete_removes_files_and_thumbnails_and_reports_each_id(self):
        a, b, keep = self.images(3)
        song = self.add("audio", "song.wav")
        self.assertTrue(all((self.root / r["thumb_path"]).is_file() for r in (a, b, keep)))
        js = self.delete_many([a["id"], "nope", b["id"], a["id"], song["id"]])         # 重なった ID は 1 回、無い ID は missing
        self.assertEqual(js, {"deleted": [a["id"], b["id"], song["id"]], "missing": ["nope"], "refused": []})
        self.assertFalse(any(self.exists(r) for r in (a, b, song)))
        self.assertEqual([o["id"] for o in self.client.get("/api/gallery").json()], [keep["id"]])
        self.assertTrue((self.root / keep["path"]).is_file() and (self.root / keep["thumb_path"]).is_file())
        js = self.delete_many([a["id"], keep["id"]])                                   # もう一度: 消えているものは missing
        self.assertEqual(js, {"deleted": [keep["id"]], "missing": [a["id"]], "refused": []})
        self.assertFalse(self.exists(keep))

    def test_refused_ones_stay_while_the_rest_are_deleted(self):
        free, busy = self.images(2)
        self.runner.add("running", {"references": [busy["id"]]})
        js = self.delete_many([busy["id"], free["id"], "nope"])
        self.assertEqual(js, {"deleted": [free["id"]], "missing": ["nope"], "refused": [{"id": busy["id"], "reason": JOB_IN_USE}]})
        self.assertTrue(self.exists(busy) and (self.root / busy["thumb_path"]).is_file() and (self.root / busy["path"]).is_file())
        self.assertFalse(self.exists(free))

    # ---- 使用中のものは断る: ジョブ ----

    def test_outputs_used_by_unfinished_jobs_are_refused_wherever_the_id_is(self):
        queued, first, last, nested, song, image = self.images(6)
        self.runner.add("queued", {"prompt": "x", "references": ["other", queued["id"]]})                       # 参照のリスト
        self.runner.add("preparing", {"first_image": first["id"], "last_image": last["id"]}, "h3.fl2va")       # 先頭・最後のフレーム
        self.runner.add("running", {"deep": {"items": [{"x": 1}, [None, {"ids": [nested["id"]]}]]}})           # 入れ子のリスト・辞書の中
        self.runner.add("running", {"song_id": song["id"], "image_id": image["id"]}, "mv.scene")               # 曲・画像の ID
        ids = [r["id"] for r in (queued, first, last, nested, song, image)]
        js = self.delete_many(ids)
        self.assertEqual(js["deleted"], [])
        self.assertEqual(js["refused"], [{"id": i, "reason": JOB_IN_USE} for i in ids])
        self.assertTrue(all(self.exists(r) for r in (queued, first, last, nested, song, image)))

    def test_outputs_used_only_by_finished_jobs_or_by_lookalike_values_can_be_deleted(self):
        done, failed, cancelled, partial, as_key, as_number = self.images(6)
        for status, rec in (("completed", done), ("failed", failed), ("cancelled", cancelled)):
            self.runner.add(status, {"references": [rec["id"]]})
        self.runner.add("running", {"prompt": f"see {partial['id']} please", partial["id"] + "x": 1, as_key["id"]: [1, 2], "n": 7, "flag": True, "none": None})
        ids = [r["id"] for r in (done, failed, cancelled, partial, as_key, as_number)]
        js = self.delete_many(ids)                          # 文字列の値が ID と等しいものだけを使用中とみなす(文の一部・キー・数は見ない)
        self.assertEqual((js["deleted"], js["missing"], js["refused"]), (ids, [], []))

    def test_an_output_is_free_again_once_its_job_has_finished(self):
        (rec,) = self.images(1)
        job = self.runner.add("running", {"references": [rec["id"]]})
        self.assertEqual(self.delete_many([rec["id"]])["refused"][0]["reason"], JOB_IN_USE)
        job.status = "completed"
        self.assertEqual(self.delete_many([rec["id"]])["deleted"], [rec["id"]])

    # ---- 使用中のものは断る: MV の制作 ----

    def test_outputs_used_by_mv_projects_in_any_state_are_refused(self):
        # 完成した制作もシーンのやり直し・結合し直し、中止した制作も再開ができるので、状態を問わず曲・画像を守る
        refused, free = {}, self.add("audio", "free.wav")
        for state in ("analyzing", "scripting", "generating", "assembling", "paused", "failed", "completed", "cancelled"):
            song, image = self.add("audio", f"{state}.wav"), self.add("image", f"{state}.png")
            self.ledger.create_mv_project(f"p_{state}", state, {"song_id": song["id"], "image_id": image["id"], "title": f"曲 {state}"})
            refused[state] = (song, image)
        ids = [r["id"] for pair in refused.values() for r in pair]
        js = self.delete_many(ids + [free["id"]])
        self.assertEqual(js["refused"], [{"id": r["id"], "reason": f"MV の制作「曲 {state}」で使っています。先に制作を削除してください"}
                                         for state, pair in refused.items() for r in pair])
        self.assertEqual(js["deleted"], [free["id"]])
        self.assertTrue(all(self.exists(r) for pair in refused.values() for r in pair))

    def test_short_projects_protect_their_song_and_reference_images_and_name_the_project(self):
        song, person, place = self.add("audio", "bgm.wav"), self.add("image", "girl.png"), self.add("image", "sea.png")
        self.ledger.create_mv_project("s1", "completed", {"song_id": song["id"], "images": [{"id": person["id"], "role": "person"},
                                                                                            {"id": place["id"], "role": "scenery"}], "title": "海の日"}, kind="short")
        js = self.delete_many([song["id"], person["id"], place["id"]])
        self.assertEqual([(r["id"], r["reason"]) for r in js["refused"]],
                         [(i, "ショート動画の制作「海の日」で使っています。先に制作を削除してください") for i in (song["id"], person["id"], place["id"])])

    def test_a_paused_project_blocks_its_song_and_image_until_the_project_is_deleted(self):
        song, image = self.add("audio", "s.wav"), self.add("image", "i.png")
        self.ledger.create_mv_project("p1", "paused", {"song_id": song["id"], "image_id": image["id"]})
        self.assertEqual({r["id"] for r in self.delete_many([song["id"], image["id"]])["refused"]}, {song["id"], image["id"]})
        self.ledger.delete_mv_project("p1")                  # 「先に制作を削除してください」のとおりにすれば消せる
        self.assertEqual(self.delete_many([song["id"], image["id"]])["deleted"], [song["id"], image["id"]])

    def test_a_job_reason_comes_before_the_mv_reason(self):
        (rec,) = self.images(1)
        self.ledger.create_mv_project("p1", "paused", {"song_id": "x", "image_id": rec["id"]})
        self.assertEqual(self.delete_many([rec["id"]])["refused"][0]["reason"], MV_IN_USE)
        self.runner.add("queued", {"references": [rec["id"]]})
        self.assertEqual(self.delete_many([rec["id"]])["refused"][0]["reason"], JOB_IN_USE)

    # ---- 1 件ずつ消す(同じ確認。断るときは 409) ----

    def test_single_delete_uses_the_same_check(self):
        free, busy, held = self.images(3)
        self.runner.add("queued", {"references": [busy["id"]]})
        self.ledger.create_mv_project("p1", "paused", {"song_id": "x", "image_id": held["id"]})
        for rec, reason in ((busy, JOB_IN_USE), (held, MV_IN_USE)):
            r = self.client.delete(f"/api/gallery/{rec['id']}")
            self.assertEqual((r.status_code, r.json()["detail"]), (409, reason))
            self.assertTrue(self.exists(rec) and (self.root / rec["path"]).is_file() and (self.root / rec["thumb_path"]).is_file())
        r = self.client.delete(f"/api/gallery/{free['id']}")
        self.assertEqual((r.status_code, r.json()), (200, {"ok": True}))
        self.assertFalse(self.exists(free))
        r = self.client.delete(f"/api/gallery/{free['id']}")
        self.assertEqual((r.status_code, r.json()["detail"]), (404, "その生成物は見つかりません"))
        self.assertEqual(self.client.delete("/api/gallery/nope").status_code, 404)

    # ---- 入力の検査 ----

    def test_request_validation(self):
        (rec,) = self.images(1)
        for body in ({}, {"ids": None}, {"ids": []}, {"ids": "abc"}, {"ids": [1, 2]}, {"ids": [None]}, {"ids": [["a"]]}, {"ids": [{"id": "a"}]},
                     {"ids": [rec["id"], 7]}, {"ids": [rec["id"]] + ["x"] * 500}):
            r = self.client.post("/api/gallery/delete", json=body)
            self.assertEqual(r.status_code, 422, body)
        r = self.client.post("/api/gallery/delete", content=b"not json", headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 422)
        self.assertTrue(self.exists(rec))                                              # 断ったら何も消さない
        js = self.delete_many([f"x{i}" for i in range(500)])                           # ちょうど 500 件は通る
        self.assertEqual((len(js["missing"]), js["deleted"], js["refused"]), (500, [], []))
        self.assertEqual(self.delete_many(["a"] * 500)["missing"], ["a"])             # 同じ ID の繰り返しは 1 件に数える
        self.assertTrue(self.exists(rec))


if __name__ == "__main__":
    unittest.main()
