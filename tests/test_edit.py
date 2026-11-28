import asyncio
import io
import sqlite3
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from studio import edit, editclip, media, mv_audio, upscale
from studio.edit import EditManager, ExportBusy
from studio.handlers import edit as eh
from studio.jobs import Ctx, Job, JobCancelled, JobFailed
from studio.ledger import EDIT_HISTORY_MAX, Ledger
from tests.test_upscale import fake_status


def png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (10, 120, 200)).save(buf, "PNG")
    return buf.getvalue()


class StubRunner:
    """JobRunner の代わり(EditManager が使う口だけ)。入れられたジョブを残し、状態は手で動かす。"""

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


class EditBase(unittest.TestCase):
    """台帳(実物)に、動画 2 本・画像・曲を登録しておく。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.ledger = Ledger(self.root)
        self.addCleanup(self.ledger.close)
        for name, ret in (("probe", {"duration_s": None, "width": None, "height": None}), ("video_thumb", False), ("has_audio", True)):
            p = mock.patch.object(media, name, return_value=ret)
            p.start()
            self.addCleanup(p.stop)
        self.v1 = self.add("video", "v1.mp4", duration_s=10.0, width=1280, height=720)
        self.v2 = self.add("video", "v2.mp4", duration_s=8.0, width=640, height=360)
        self.img = self.add("image", "p.png")
        self.song = self.add("audio", "s.wav", duration_s=30.0)
        self.runner = StubRunner()
        self.mgr = EditManager(self.runner, self.ledger, self.root)

    def add(self, kind, name, **kw) -> dict:
        p = self.root / "uploads" / name
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(png_bytes() if kind == "image" else b"fakemedia")
        return self.ledger.add_output(kind, p, "upload", filename=name, params={}, **kw)

    def raw(self, **kw) -> dict:
        base = {"clips": [{"id": self.v1["id"], "in": 0, "out": 4}, {"id": self.v2["id"], "in": 1, "out": 5}, {"id": self.img["id"], "duration": 3}]}
        return {**base, **kw}

    def create(self, **kw) -> dict:
        return self.mgr.create("テスト", self.raw(**kw))

    def go(self, coro):
        return asyncio.run(coro)


class LedgerEditTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.ledger = Ledger(self.root)
        self.addCleanup(self.ledger.close)

    def s(self, n) -> dict:
        return {**editclip.empty_state(), "fade_in": float(n)}

    def test_create_get_list(self):
        p = self.ledger.create_edit_project("e1", "一本目", self.s(0))
        self.assertEqual((p["id"], p["name"], p["cursor"], p["history_len"], p["can_undo"], p["can_redo"], p["preview"], p["output_id"]),
                         ("e1", "一本目", 0, 1, False, False, None, None))
        self.assertEqual(p["state"], self.s(0))
        self.assertNotIn("history", p)
        self.assertEqual(self.ledger.get_edit_project("e1", history=True)["history"], [self.s(0)])
        self.assertIsNone(self.ledger.get_edit_project("nope"))
        time.sleep(0.01)
        self.ledger.create_edit_project("e2", "二本目", self.s(0))
        self.assertEqual([x["id"] for x in self.ledger.list_edit_projects()], ["e2", "e1"])       # 更新が新しい順
        self.ledger.push_edit_state("e1", self.s(1))
        self.assertEqual([x["id"] for x in self.ledger.list_edit_projects()], ["e1", "e2"])
        self.assertNotIn("history", self.ledger.list_edit_projects()[0])

    def test_push_undo_redo_cursor(self):
        L = self.ledger
        L.create_edit_project("e", "x", self.s(0))
        for n in (1, 2, 3):
            p = L.push_edit_state("e", self.s(n))
        self.assertEqual((p["cursor"], p["history_len"], p["state"]["fade_in"], p["can_undo"], p["can_redo"]), (3, 4, 3.0, True, False))
        p = L.move_edit_cursor("e", -1)
        self.assertEqual((p["cursor"], p["state"]["fade_in"], p["can_undo"], p["can_redo"]), (2, 2.0, True, True))
        p = L.move_edit_cursor("e", -1)
        p = L.move_edit_cursor("e", -1)
        self.assertEqual((p["cursor"], p["state"]["fade_in"], p["can_undo"]), (0, 0.0, False))
        with self.assertRaisesRegex(ValueError, "これ以上戻せません"):
            L.move_edit_cursor("e", -1)
        p = L.move_edit_cursor("e", +1)
        self.assertEqual((p["cursor"], p["state"]["fade_in"]), (1, 1.0))
        p = L.push_edit_state("e", self.s(9))                    # 途中で新しい操作 → やり直しの枝(2, 3)は捨てる
        self.assertEqual((p["cursor"], p["history_len"], p["can_redo"]), (2, 3, False))
        self.assertEqual([h["fade_in"] for h in L.get_edit_project("e", history=True)["history"]], [0.0, 1.0, 9.0])
        with self.assertRaisesRegex(ValueError, "これ以上やり直せません"):
            L.move_edit_cursor("e", +1)
        self.assertIsNone(L.push_edit_state("nope", self.s(1)))
        self.assertIsNone(L.move_edit_cursor("nope", -1))

    def test_identical_push_is_noop(self):
        self.ledger.create_edit_project("e", "x", self.s(0))
        p = self.ledger.push_edit_state("e", self.s(0))
        self.assertEqual((p["cursor"], p["history_len"]), (0, 1))
        self.ledger.push_edit_state("e", self.s(1))
        p = self.ledger.push_edit_state("e", self.s(1))
        self.assertEqual(p["history_len"], 2)

    def test_history_is_capped_at_100(self):
        self.ledger.create_edit_project("e", "x", self.s(0))
        for n in range(1, 131):
            p = self.ledger.push_edit_state("e", self.s(n))
        self.assertEqual((EDIT_HISTORY_MAX, p["history_len"], p["cursor"]), (100, 100, 99))
        hist = self.ledger.get_edit_project("e", history=True)["history"]
        self.assertEqual((hist[0]["fade_in"], hist[-1]["fade_in"]), (31.0, 130.0))              # 古いものから落とす
        for _ in range(99):
            p = self.ledger.move_edit_cursor("e", -1)
        self.assertEqual((p["cursor"], p["state"]["fade_in"], p["can_undo"]), (0, 31.0, False))

    def test_update_and_delete(self):
        L = self.ledger
        L.create_edit_project("e", "x", self.s(0))
        before = L.get_edit_project("e")["updated_at"]
        p = L.update_edit_project("e", preview={"n": 2, "hash": "h", "path": "edit/e/preview_2.mp4"}, output_id="o1")
        self.assertEqual((p["preview"], p["output_id"], p["updated_at"]), ({"n": 2, "hash": "h", "path": "edit/e/preview_2.mp4"}, "o1", before))
        p = L.update_edit_project("e", preview=None, name="新しい名前")
        self.assertEqual((p["preview"], p["name"]), (None, "新しい名前"))
        self.assertGreater(p["updated_at"], before)
        with self.assertRaises(ValueError):
            L.update_edit_project("e", state={})
        self.assertIsNone(L.update_edit_project("nope", name="y"))
        self.assertTrue(L.delete_edit_project("e"))
        self.assertFalse(L.delete_edit_project("e"))
        self.assertIsNone(L.get_edit_project("e"))

    def test_persists_and_old_database_is_migrated(self):
        self.ledger.create_edit_project("e", "x", self.s(0))
        self.ledger.push_edit_state("e", self.s(1))
        self.ledger.move_edit_cursor("e", -1)
        self.ledger.close()
        again = Ledger(self.root)                                   # 再起動後も履歴の位置が残る
        self.addCleanup(again.close)
        p = again.get_edit_project("e")
        self.assertEqual((p["cursor"], p["history_len"], p["can_redo"]), (0, 2, True))
        old = Path(self.root) / "old"                               # 編集の表が無い古い DB を開いても、表が足される
        old.mkdir()
        db = sqlite3.connect(old / "studio.db")
        db.execute("CREATE TABLE outputs (id TEXT PRIMARY KEY, kind TEXT NOT NULL, tab TEXT NOT NULL, job_id TEXT, path TEXT NOT NULL, filename TEXT,"
                   " width INTEGER, height INTEGER, duration_s REAL, created_at REAL NOT NULL, params_json TEXT, thumb_path TEXT)")
        db.commit()
        db.close()
        migrated = Ledger(old)
        self.addCleanup(migrated.close)
        self.assertEqual(migrated.list_edit_projects(), [])
        migrated.create_edit_project("x", "y", self.s(0))


class EditManagerTest(EditBase):
    def test_create_empty_and_with_state(self):
        v = self.mgr.create("", None)
        self.assertEqual((v["name"], v["duration"], v["timeline"], v["clips"], v["preview"], v["job"], v["output"], v["can_undo"], v["can_redo"], v["error"]),
                         ("無題の編集", 0.0, [], [], None, None, None, False, False, None))
        self.assertEqual(v["state"], editclip.empty_state())
        v = self.create(transitions=[{"after": 0, "type": "crossfade", "duration": 1}])
        self.assertEqual((v["name"], v["duration"]), ("テスト", 10.0))                              # 4 + 4 + 3 - 1
        self.assertEqual(v["timeline"], [{"index": 0, "start": 0.0, "end": 4.0}, {"index": 1, "start": 4.0, "end": 8.0}, {"index": 2, "start": 8.0, "end": 11.0}])
        self.assertEqual((v["state"]["width"], v["state"]["height"]), (1280, 720))
        with self.assertRaisesRegex(ValueError, "クリップ 1の生成物が見つかりません"):
            self.mgr.create("x", {"clips": [{"id": "gone", "in": 0, "out": 1}]})
        with self.assertRaisesRegex(ValueError, "名前は 80 文字まで"):
            self.mgr.create("あ" * 81)

    def test_view_clips_are_enriched_and_missing_is_flagged(self):
        v = self.create()
        c0, c1, c2 = v["clips"]
        self.assertEqual((c0["id"], c0["kind"], c0["in"], c0["out"], c0["filename"], c0["length_s"], c0["width"], c0["height"], c0["missing"]),
                         (self.v1["id"], "video", 0.0, 4.0, "v1.mp4", 10.0, 1280, 720, False))
        self.assertEqual((c1["length_s"], c1["width"]), (8.0, 640))
        self.assertEqual((c2["kind"], c2["length_s"], c2["missing"]), ("image", None, False))
        self.assertEqual(c2["thumb_url"], "/outputs/" + self.img["thumb_path"])
        self.assertIsNone(c0["thumb_url"])                                                           # 動画のサムネイルを作れなかった
        self.ledger.abs_path(self.v1["path"]).unlink()
        self.assertTrue(self.mgr.view(v["id"])["clips"][0]["missing"])
        self.ledger.delete_output(self.v2["id"])                                                     # 台帳から消えたもの(削除の保護をすり抜けた場合)
        gone = self.mgr.view(v["id"])["clips"][1]
        self.assertEqual((gone["missing"], gone["filename"], gone["id"], gone["in"]), (True, None, self.v2["id"], 1.0))
        self.assertIsNone(self.mgr.view("nope"))

    def test_set_state_undo_redo(self):
        v = self.create()
        pid = v["id"]
        new = self.mgr.set_state(pid, {**v["state"], "fade_in": 1.0, "effects": [{"type": "mono", "start": 0, "end": 2}]})
        self.assertEqual((new["state"]["fade_in"], new["can_undo"], new["can_redo"]), (1.0, True, False))
        v3 = self.mgr.set_state(pid, {**new["state"], "fade_out": 0.5})
        undone = self.mgr.undo(pid)
        self.assertEqual((undone["state"]["fade_out"], undone["state"]["fade_in"], undone["can_redo"]), (0.0, 1.0, True))
        self.mgr.undo(pid)
        self.assertEqual(self.mgr.view(pid)["state"]["fade_in"], 0.0)
        with self.assertRaisesRegex(ValueError, "これ以上戻せません"):
            self.mgr.undo(pid)
        self.assertEqual(self.mgr.redo(pid)["state"]["fade_in"], 1.0)
        self.assertEqual(self.mgr.redo(pid)["state"], v3["state"])
        with self.assertRaisesRegex(ValueError, "これ以上やり直せません"):
            self.mgr.redo(pid)
        # 検証の失敗は履歴に積まない
        with self.assertRaisesRegex(ValueError, "終了が元の動画の長さ"):
            self.mgr.set_state(pid, {"clips": [{"id": self.v1["id"], "in": 0, "out": 99}]})
        with self.assertRaisesRegex(ValueError, "state は辞書"):
            self.mgr.set_state(pid, "x")
        self.assertEqual(self.mgr.view(pid)["state"], v3["state"])
        for fn in (self.mgr.undo, self.mgr.redo, lambda p: self.mgr.set_state(p, {}), lambda p: self.mgr.rename(p, "x")):
            with self.assertRaises(LookupError):
                fn("nope")

    def test_audio_state_validation_uses_ledger_sources(self):
        v = self.create(audio={"mode": "music", "song_id": self.song["id"]})
        self.assertEqual(v["state"]["audio"]["song_id"], self.song["id"])
        with self.assertRaisesRegex(ValueError, "曲には音声を使ってください"):
            self.mgr.set_state(v["id"], self.raw(audio={"mode": "music", "song_id": self.v2["id"]}))
        with self.assertRaisesRegex(ValueError, "クリップ 1には動画か画像を使ってください"):
            self.mgr.set_state(v["id"], {"clips": [{"id": self.song["id"], "in": 0, "out": 1}]})

    def test_preview_staleness_follows_the_state(self):
        v = self.create()
        pid = v["id"]
        self.assertIsNone(v["preview"])
        dst = edit.work_dir(self.root, pid) / "preview_1.mp4"
        dst.parent.mkdir(parents=True)
        dst.write_bytes(b"mp4")
        self.ledger.update_edit_project(pid, preview={"n": 1, "hash": editclip.state_hash(v["state"]), "path": self.ledger.rel_path(dst)})
        self.assertEqual(self.mgr.view(pid)["preview"], {"url": f"/outputs/edit/{pid}/preview_1.mp4", "stale": False})
        changed = self.mgr.set_state(pid, {**v["state"], "fade_out": 1.0})
        self.assertEqual(changed["preview"]["stale"], True)                                           # 最後の操作より古い
        self.assertEqual(self.mgr.undo(pid)["preview"]["stale"], False)                              # 戻せば、その状態で作ったプレビューに合う
        self.assertEqual(self.mgr.redo(pid)["preview"]["stale"], True)
        dst.unlink()
        self.assertIsNone(self.mgr.view(pid)["preview"])                                              # ファイルが無ければ無し

    def test_preview_submits_job_and_replaces_the_running_one(self):
        v = self.create()
        pid = v["id"]
        v1 = self.go(self.mgr.preview(pid))
        (job1,) = self.runner.jobs.values()
        self.assertEqual((job1.tab, job1.kind, job1.params["project_id"], job1.params["seq"], job1.params["state"]), ("edit", "edit.preview", pid, 1, v["state"]))
        self.assertEqual(v1["job"], {"id": job1.id, "kind": "edit.preview", "status": "queued", "progress": 0.0, "message": "順番を待っています", "error": None})
        job1.status = "running"
        self.mgr.set_state(pid, {**v["state"], "fade_in": 1.0})
        v2 = self.go(self.mgr.preview(pid))
        job2 = self.runner.jobs["job2"]
        self.assertEqual(self.runner.cancelled, [job1.id])                                            # 走っているプレビューは中止して新しいものを入れる
        self.assertEqual((job2.params["seq"], job2.params["state"]["fade_in"], v2["job"]["id"]), (2, 1.0, job2.id))
        self.go(self.mgr.preview(pid))                                                              # 順番待ちのものも中止
        self.assertEqual(self.runner.cancelled, [job1.id, job2.id])
        job2.status = "completed"
        self.assertEqual(self.runner.jobs["job3"].params["seq"], 3)
        self.runner.jobs["job3"].status = "completed"
        self.assertIsNone(self.mgr.view(pid)["job"])                                                  # 終わったジョブは出さない
        self.go(self.mgr.preview(pid))
        self.assertEqual(self.runner.cancelled, [job1.id, job2.id])                                   # 終わっているものは中止しない

    def test_preview_numbering_continues_after_restart(self):
        v = self.create()
        pid = v["id"]
        d = edit.work_dir(self.root, pid)
        d.mkdir(parents=True)
        (d / "preview_7.mp4").write_bytes(b"x")
        self.go(self.mgr.preview(pid))
        self.assertEqual(self.runner.jobs["job1"].params["seq"], 8)
        fresh = EditManager(self.runner, self.ledger, self.root)                                       # 管理役を作り直しても、番号は戻らない
        self.ledger.update_edit_project(pid, preview={"n": 12, "hash": "h", "path": "edit/x/preview_12.mp4"})
        self.go(fresh.preview(pid))
        self.assertEqual(self.runner.jobs["job2"].params["seq"], 13)

    def test_export_refuses_while_exporting(self):
        v = self.create()
        pid = v["id"]
        out = self.mgr.export(pid)
        (job,) = self.runner.jobs.values()
        self.assertEqual((job.kind, job.params["project_id"], job.params["state"], out["job"]["kind"]), ("edit.export", pid, v["state"], "edit.export"))
        with self.assertRaises(ExportBusy):
            self.mgr.export(pid)
        self.assertTrue(issubclass(ExportBusy, ValueError))
        job.status = "running"
        with self.assertRaises(ExportBusy):
            self.mgr.export(pid)
        job.status = "completed"
        self.mgr.export(pid)                                                                          # 終われば、もう一度書き出せる
        self.assertEqual(len(self.runner.jobs), 2)

    def test_export_with_upscale_resolves_against_the_state_size(self):
        v = self.create()
        pid = v["id"]
        with mock.patch.object(upscale, "status", return_value=fake_status(realesrgan=True, rtxvsr=True)) as st:
            self.mgr.export(pid, {"method": "auto", "scale": 2})
        self.assertEqual(self.runner.jobs["job1"].params["upscale"], {"method": "rtxvsr", "scale": 2, "width": 2560, "height": 1440})   # 1280×720 の 2 倍
        self.assertIs(st.call_args.args[0], self.mgr.settings)
        self.runner.jobs["job1"].status = "completed"
        with mock.patch.object(upscale, "status", return_value=fake_status(realesrgan=True, rtxvsr=False)):
            self.mgr.export(pid, {"method": "auto", "scale": 1})                                   # 倍率 1 は拡大しない
            self.assertNotIn("upscale", self.runner.jobs["job2"].params)
            self.runner.jobs["job2"].status = "completed"
            self.mgr.export(pid, {"scale": 3})                                                     # 方式を省けば auto
            self.assertEqual(self.runner.jobs["job3"].params["upscale"]["method"], "realesrgan")
            self.runner.jobs["job3"].status = "completed"
            with self.assertRaisesRegex(ValueError, "^GPU の条件$"):                                 # 使えない方式は、その理由
                self.mgr.export(pid, {"method": "rtxvsr", "scale": 2})
            with self.assertRaisesRegex(ValueError, "3840px を超えます"):
                self.mgr.export(pid, {"method": "realesrgan", "scale": 4})
            with self.assertRaisesRegex(ValueError, "upscale は"):
                self.mgr.export(pid, "x")
        with mock.patch.object(upscale, "status", return_value=fake_status(realesrgan=False, rtxvsr=False)):
            with self.assertRaisesRegex(ValueError, "拡大の方式が入っていません"):
                self.mgr.export(pid, {"method": "auto", "scale": 2})
        self.assertEqual(len(self.runner.jobs), 3)                                                   # 失敗はジョブを入れない
        fresh = EditManager(self.runner, self.ledger, self.root, settings=object())
        self.assertIsInstance(fresh.settings, object)

    def test_export_and_preview_run_side_by_side_and_view_prefers_export(self):
        v = self.create()
        pid = v["id"]
        self.go(self.mgr.preview(pid))
        self.mgr.export(pid)
        self.assertEqual(self.runner.cancelled, [])                                                   # 書き出しはプレビューを中止しない
        self.assertEqual(self.mgr.view(pid)["job"]["kind"], "edit.export")
        self.runner.jobs["job2"].status = "completed"
        self.assertEqual(self.mgr.view(pid)["job"]["kind"], "edit.preview")

    def test_preview_and_export_need_clips_and_files(self):
        empty = self.mgr.create("空")
        with self.assertRaisesRegex(ValueError, "クリップがありません"):
            self.go(self.mgr.preview(empty["id"]))
        with self.assertRaisesRegex(ValueError, "クリップがありません"):
            self.mgr.export(empty["id"])
        v = self.create()
        self.ledger.abs_path(self.v2["path"]).unlink()
        with self.assertRaisesRegex(ValueError, "クリップ 2 のファイルが見つかりません"):
            self.go(self.mgr.preview(v["id"]))
        with self.assertRaisesRegex(ValueError, "クリップ 2 のファイルが見つかりません"):
            self.mgr.export(v["id"])
        self.assertEqual(self.runner.jobs, {})

    def test_failed_job_is_reported_until_the_next_job(self):
        v = self.create()
        self.go(self.mgr.preview(v["id"]))
        job = self.runner.jobs["job1"]
        job.status, job.error = "failed", "プレビューを作っていますに失敗しました(ffmpeg): boom"
        out = self.mgr.view(v["id"])
        self.assertEqual((out["job"], out["error"]), (None, job.error))
        self.go(self.mgr.preview(v["id"]))
        self.assertIsNone(self.mgr.view(v["id"])["error"])

    def test_rename(self):
        v = self.create()
        self.assertEqual(self.mgr.rename(v["id"], "  新しい名前 ")["name"], "新しい名前")
        for bad in ("", "   "):
            with self.assertRaisesRegex(ValueError, "名前を入力してください"):
                self.mgr.rename(v["id"], bad)
        with self.assertRaisesRegex(ValueError, "80 文字まで"):
            self.mgr.rename(v["id"], "x" * 81)

    def test_delete_removes_row_previews_and_cancels_jobs(self):
        v = self.create()
        pid = v["id"]
        d = edit.work_dir(self.root, pid)
        d.mkdir(parents=True)
        (d / "preview_1.mp4").write_bytes(b"x")
        self.go(self.mgr.preview(pid))
        self.mgr.export(pid)
        rec = self.add("video", "exported.mp4", duration_s=3.0)
        self.ledger.update_edit_project(pid, output_id=rec["id"])
        self.go(self.mgr.delete(pid))
        self.assertIsNone(self.ledger.get_edit_project(pid))
        self.assertFalse(d.exists())
        self.assertEqual(sorted(self.runner.cancelled), ["job1", "job2"])
        self.assertIsNotNone(self.ledger.get_output(rec["id"]))                                       # 書き出した動画はギャラリーに残る
        with self.assertRaises(LookupError):
            self.go(self.mgr.delete(pid))

    def test_output_and_list(self):
        v = self.create()
        pid = v["id"]
        rec = self.add("video", "out.mp4", duration_s=10.0, width=1280, height=720)
        self.ledger.update_edit_project(pid, output_id=rec["id"])
        self.assertEqual(self.mgr.view(pid)["output"]["id"], rec["id"])
        self.ledger.delete_output(rec["id"])
        self.assertIsNone(self.mgr.view(pid)["output"])                                               # ギャラリーから消されたら無し
        time.sleep(0.01)
        other = self.mgr.create("二本目")
        rows = self.mgr.list()
        self.assertEqual([r["id"] for r in rows], [other["id"], pid])
        self.assertEqual((rows[1]["name"], rows[1]["clips"], rows[1]["duration"], rows[1]["output"], rows[1]["thumb_url"]),
                         ("テスト", 3, 11.0, None, None))                                              # 先頭は動画(サムネイル無し)
        self.assertEqual((rows[0]["clips"], rows[0]["duration"]), (0, 0.0))
        self.assertEqual(set(rows[0]), {"id", "name", "updated_at", "duration", "clips", "thumb_url", "output"})

    def test_fps_comes_from_the_first_video(self):
        v30 = self.add("video", "v30.mp4", duration_s=6.0, width=1280, height=720, fps=29.97)
        v = self.mgr.create("30fps", {"clips": [{"id": self.img["id"], "duration": 2}, {"id": v30["id"], "in": 0, "out": 3}, {"id": self.v1["id"], "in": 0, "out": 2}]})
        self.assertEqual((v["state"]["width"], v["state"]["height"], v["state"]["fps"]), (1280, 720, 30))     # 先頭の動画(画像は見ない)
        self.assertEqual(self.create()["state"]["fps"], 24)                                                    # コマ率の分からない動画(前の版の行)は 24

    def test_extract_frame_stays_inside_the_last_frame_of_the_clip(self):
        pid = self.mgr.create("コマ", {"clips": [{"id": self.v1["id"], "in": 0, "out": 4}, {"id": self.v2["id"], "in": 1, "out": 5}]})["id"]
        seen = []

        def fake_run(argv, dst, what):
            seen.append(float(argv[argv.index("-ss") + 1]))
            Path(dst).parent.mkdir(parents=True, exist_ok=True)
            Path(dst).write_bytes(png_bytes())
            return dst
        with mock.patch.object(mv_audio, "_ffmpeg", return_value="ffmpeg"), mock.patch.object(mv_audio, "_run_ffmpeg", side_effect=fake_run):
            mid = self.mgr.extract_frame(pid, 1.0)
            end = self.mgr.extract_frame(pid, 99)                     # 再生位置が動画の終わり: v2 の out(5 秒)ちょうどは、そこから始まるコマが無い
            edge = self.mgr.extract_frame(pid, 3.99)                  # v1 の最後のコマの中(境目の手前)
        self.assertEqual(seen, [1.0, round(5 - 1 / 24, 3), round(4 - 1 / 24, 3)])
        self.assertEqual((mid["params"]["source_id"], end["params"]["source_id"], edge["params"]["source_id"]), (self.v1["id"], self.v2["id"], self.v1["id"]))
        self.assertEqual(end["params"]["source_s"], round(5 - 1 / 24, 3))

    def test_probe(self):
        self.assertEqual(self.mgr.probe(self.v1["id"]), {"kind": "video", "length_s": 10.0, "width": 1280, "height": 720, "filename": "v1.mp4", "thumb_url": None})
        img = self.mgr.probe(self.img["id"])
        self.assertEqual((img["kind"], img["length_s"], img["width"], img["thumb_url"]), ("image", None, 64, "/outputs/" + self.img["thumb_path"]))
        self.assertEqual((self.mgr.probe(self.song["id"])["kind"], self.mgr.probe(self.song["id"])["length_s"]), ("audio", 30.0))
        with self.assertRaises(LookupError):
            self.mgr.probe("nope")


class FakeFfmpeg:
    """handlers.edit.run_ffmpeg の代わり: 引数を残し、出力先(.part を外した dst)に中身を書く(本物の run_ffmpeg は os.replace まで行う)。"""

    def __init__(self):
        self.calls = []

    def __call__(self, job, argv, dst, total_s, label):
        self.calls.append({"argv": list(argv), "dst": Path(dst), "total_s": total_s, "label": label})
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_bytes(b"ffmpeg-output")


class EditHandlerTest(EditBase):
    def setUp(self):
        super().setUp()
        self.ff = FakeFfmpeg()
        for target, value in ((eh, ("run_ffmpeg", self.ff)), (mv_audio, ("_ffmpeg", lambda: "ffmpeg"))):
            p = mock.patch.object(target, value[0], value[1])
            p.start()
            self.addCleanup(p.stop)
        self.ctx = Ctx(None, self.ledger, self.root)

    def job(self, kind, pid, state, **extra) -> Job:
        return Job(id="j1", tab="edit", kind=kind, params=eh.normalize(kind, {"project_id": pid, "state": state, **extra}, self.ledger))

    def test_the_jobs_table_keeps_the_state_hash_not_the_state(self):
        v = self.create()
        params = eh.normalize("edit.preview", {"project_id": v["id"]}, self.ledger)
        self.assertEqual(eh.HANDLERS["edit.preview"].record(params), {"project_id": v["id"], "state_hash": editclip.state_hash(v["state"]), "seq": 1})
        exported = eh.HANDLERS["edit.export"].record(eh.normalize("edit.export", {"project_id": v["id"]}, self.ledger))
        self.assertEqual(exported, {"project_id": v["id"], "state_hash": editclip.state_hash(v["state"])})

    def test_normalize(self):
        v = self.create()
        out = eh.normalize("edit.preview", {"project_id": v["id"]}, self.ledger)
        self.assertEqual((out["project_id"], out["state"], out["seq"]), (v["id"], v["state"], 1))
        self.assertNotIn("seq", eh.normalize("edit.export", {"project_id": v["id"], "seq": 5}, self.ledger))
        with self.assertRaisesRegex(ValueError, "編集プロジェクトが見つかりません"):
            eh.normalize("edit.export", {"project_id": "nope"}, self.ledger)
        empty = self.mgr.create("空")
        with self.assertRaisesRegex(ValueError, "クリップがありません"):
            eh.normalize("edit.export", {"project_id": empty["id"]}, self.ledger)
        with self.assertRaisesRegex(ValueError, "プレビューの番号"):
            eh.normalize("edit.preview", {"project_id": v["id"], "seq": "x"}, self.ledger)
        # 渡された state は台帳で検証し直す(フィルタの文に入る寸法・効果の値は、検証した数だけ)
        forged = {**v["state"], "width": "1;movie=/etc/passwd", "effects": [{"type": "eq", "value": "x"}]}
        with self.assertRaisesRegex(ValueError, "効果 1の種類"):
            eh.normalize("edit.export", {"project_id": v["id"], "state": forged}, self.ledger)
        out = eh.normalize("edit.export", {"project_id": v["id"], "state": {**v["state"], "width": "1;movie=/etc/passwd"}}, self.ledger)
        self.assertEqual(out["state"], v["state"])                                                     # 寸法は先頭の動画から決め直す
        with self.assertRaisesRegex(ValueError, "クリップ 1の生成物が見つかりません"):
            eh.normalize("edit.export", {"project_id": v["id"], "state": {**v["state"], "clips": [{"id": "gone", "in": 0, "out": 1}]}}, self.ledger)

    def test_preview_writes_numbered_file_records_hash_and_keeps_only_the_newest(self):
        v = self.create(audio={"mode": "music", "song_id": self.song["id"]})
        pid, d = v["id"], edit.work_dir(self.root, v["id"])
        d.mkdir(parents=True)
        for n in range(1, 10):
            (d / f"preview_{n}.mp4").write_bytes(b"old")
        (d / "notes.txt").write_text("keep")
        job = self.job("edit.preview", pid, v["state"], seq=10)
        asyncio.run(eh.run_preview(job, self.ctx))
        (call,) = self.ff.calls
        h = editclip.state_hash(v["state"])
        name = f"preview_10_{h}.mp4"
        self.assertEqual(call["dst"], d / name)                                                          # 名前に状態の指紋
        self.assertEqual((call["label"], call["total_s"]), ("プレビューを作っています", 11.0))
        argv = call["argv"]
        self.assertEqual(argv[argv.index("-crf") + 1], "28")                                            # プレビューの設定
        self.assertEqual(argv[-1], str(d / (name + ".part")))
        self.assertIn("-progress", argv)
        self.assertIn(str(self.ledger.abs_path(self.song["path"])), argv)                               # 曲の入力
        self.assertEqual(sorted(p.name for p in d.iterdir()),                                           # 新しい順に 8 本だけ残り(1・2 は消える)、関係ないファイルは残る
                         sorted(["notes.txt", name, *(f"preview_{n}.mp4" for n in range(3, 10))]))
        rec = self.ledger.get_edit_project(pid)["preview"]
        self.assertEqual(rec, {"n": 10, "hash": h, "path": f"edit/{pid}/{name}"})
        self.assertEqual(self.mgr.view(pid)["preview"], {"url": f"/outputs/edit/{pid}/{name}", "stale": False})

    def test_undo_reuses_the_previous_states_preview_without_a_job(self):
        """元に戻す・やり直すで、その状態のプレビューが残っていれば、ジョブを入れずにそれを出す(stale でない)。"""
        v = self.create()
        pid, d = v["id"], edit.work_dir(self.root, v["id"])
        d.mkdir(parents=True)
        h1 = editclip.state_hash(v["state"])
        (d / f"preview_1_{h1}.mp4").write_bytes(b"one")
        self.ledger.update_edit_project(pid, preview={"n": 1, "hash": h1, "path": f"edit/{pid}/preview_1_{h1}.mp4"})
        st = dict(v["state"]); st["fade_in"] = 1.0
        v2 = self.mgr.set_state(pid, st)
        h2 = editclip.state_hash(v2["state"])
        self.assertEqual(v2["preview"]["stale"], True)
        (d / f"preview_2_{h2}.mp4").write_bytes(b"two")
        self.ledger.update_edit_project(pid, preview={"n": 2, "hash": h2, "path": f"edit/{pid}/preview_2_{h2}.mp4"})
        back = self.mgr.undo(pid)
        self.assertEqual(back["preview"], {"url": f"/outputs/edit/{pid}/preview_1_{h1}.mp4", "stale": False})      # 前の状態のプレビューがそのまま出る
        before = len(self.runner.jobs)
        again = asyncio.run(self.mgr.preview(pid))
        self.assertEqual(len(self.runner.jobs), before)                                                               # ジョブは入れない
        self.assertEqual(again["preview"]["url"], f"/outputs/edit/{pid}/preview_1_{h1}.mp4")
        self.assertEqual(self.ledger.get_edit_project(pid)["preview"]["n"], 1)

    def test_preview_that_lost_the_race_or_whose_project_vanished_is_discarded(self):
        v = self.create()
        pid, d = v["id"], edit.work_dir(self.root, v["id"])
        self.ledger.update_edit_project(pid, preview={"n": 5, "hash": "h", "path": f"edit/{pid}/preview_5.mp4"})
        job = self.job("edit.preview", pid, v["state"], seq=4)                                          # もっと新しい番号が先にできている
        asyncio.run(eh.run_preview(job, self.ctx))
        self.assertFalse((d / "preview_4.mp4").exists())
        self.assertEqual(self.ledger.get_edit_project(pid)["preview"]["n"], 5)
        job = self.job("edit.preview", pid, v["state"], seq=6)

        async def go():
            real = eh._encode

            async def encode_then_delete(*a, **k):
                await real(*a, **k)
                self.ledger.delete_edit_project(pid)                                                  # 作っている間にプロジェクトが消された
            with mock.patch.object(eh, "_encode", encode_then_delete):
                await eh.run_preview(job, self.ctx)
        asyncio.run(go())
        self.assertFalse((d / "preview_6.mp4").exists())

    def test_export_registers_video_in_gallery_and_names_files(self):
        v = self.create(transitions=[{"after": 0, "type": "crossfade", "duration": 1}])
        pid = v["id"]
        job = self.job("edit.export", pid, v["state"])
        asyncio.run(eh.run_export(job, self.ctx))
        (call,) = self.ff.calls
        self.assertEqual(call["dst"], self.root / "video" / f"edit_{pid}.mp4")
        self.assertEqual((call["label"], call["total_s"]), ("書き出しています", 10.0))
        argv = call["argv"]
        self.assertEqual((argv[argv.index("-crf") + 1], argv[argv.index("-b:a") + 1]), ("18", "256k"))
        self.assertIn("scale=1280:720:", " ".join(argv))
        (oid,) = job.outputs
        rec = self.ledger.get_output(oid)
        self.assertEqual((rec["kind"], rec["tab"], rec["job_id"], rec["path"], rec["filename"], rec["width"], rec["height"], rec["duration_s"]),
                         ("video", "edit", "j1", f"video/edit_{pid}.mp4", "テスト_edit.mp4", 1280, 720, 10.0))
        self.assertEqual(rec["params"], {"project_id": pid, "state": v["state"], "name": "テスト"})
        self.assertEqual(self.ledger.get_edit_project(pid)["output_id"], oid)
        self.assertEqual(self.mgr.view(pid)["output"]["id"], oid)
        self.assertFalse((self.root / "edit" / pid).exists())                                           # プレビューは作らない
        job2 = self.job("edit.export", pid, v["state"])
        asyncio.run(eh.run_export(job2, self.ctx))                                                      # 2 回目は _2(前の動画を上書きしない)
        self.assertEqual(self.ff.calls[1]["dst"], self.root / "video" / f"edit_{pid}_2.mp4")
        self.assertEqual(self.ledger.get_edit_project(pid)["output_id"], job2.outputs[0])

    def test_export_uses_the_state_in_params_not_the_current_one(self):
        v = self.create()
        job = self.job("edit.export", v["id"], v["state"])
        self.mgr.set_state(v["id"], {**v["state"], "fade_in": 2.0})                                     # 順番待ちの間に操作が続いた
        asyncio.run(eh.run_export(job, self.ctx))
        self.assertNotIn("afade=t=in", " ".join(self.ff.calls[0]["argv"]))

    def test_silent_video_is_filled_with_silence_and_missing_files_fail(self):
        v = self.create()
        job = self.job("edit.preview", v["id"], v["state"], seq=1)
        with mock.patch.object(media, "has_audio", side_effect=lambda p: p.name != "v2.mp4"):
            asyncio.run(eh.run_preview(job, self.ctx))
        g = self.ff.calls[0]["argv"][self.ff.calls[0]["argv"].index("-filter_complex") + 1]
        self.assertIn("[0:a]atrim", g)
        self.assertNotIn("[1:a]", g)                                                                    # v2 は音声なし → anullsrc
        self.assertEqual(g.count("anullsrc"), 2)                                                        # v2 と画像
        self.ledger.abs_path(self.v2["path"]).unlink()
        job = self.job("edit.preview", v["id"], v["state"], seq=2)
        with self.assertRaisesRegex(JobFailed, "クリップ 2 のファイルが見つかりません"):
            asyncio.run(eh.run_preview(job, self.ctx))
        self.assertEqual(len(self.ff.calls), 1)

    def test_missing_song_file_fails(self):
        gone = self.create(audio={"mode": "music", "song_id": self.song["id"]})
        self.ledger.abs_path(self.song["path"]).unlink()
        with self.assertRaisesRegex(JobFailed, "曲のファイルが見つかりません"):
            asyncio.run(eh.run_export(self.job("edit.export", gone["id"], gone["state"]), self.ctx))
        self.assertEqual(self.ff.calls, [])

    def test_cancel_requested_before_start(self):
        v = self.create()
        job = self.job("edit.export", v["id"], v["state"])
        job.cancel_requested = True
        with self.assertRaises(JobCancelled):
            asyncio.run(eh.run_export(job, self.ctx))
        self.assertEqual(self.ff.calls, [])

    def test_missing_project_fails(self):
        v = self.create()
        job = self.job("edit.export", v["id"], v["state"])
        self.ledger.delete_edit_project(v["id"])
        with self.assertRaisesRegex(JobFailed, "編集プロジェクトが見つかりません"):
            asyncio.run(eh.run_export(job, self.ctx))

    # ---- 書き出しの AI 拡大 ----

    def upscale_job(self, v, method="realesrgan", scale=2) -> Job:
        return self.job("edit.export", v["id"], v["state"], upscale={"method": method, "scale": scale})

    def fake_runner(self, record: list, fail=None):
        """upscale.run_realesrgan / run_rtxvsr の代わり: 引数と、呼ばれた時点の job.message を残し、dst を書く。"""
        def run(job, tool, src, dst, scale, fps, work, ffmpeg, procs, *rest):
            record.append({"tool": tool, "src": Path(src), "dst": Path(dst), "scale": scale, "fps": fps, "work": Path(work), "ffmpeg": ffmpeg,
                           "procs": procs, "message": job.message, "rest": rest})
            if fail is not None:
                raise fail
            Path(dst).write_bytes(b"upscaled")
            return Path(dst)
        return run

    def test_normalize_and_record_keep_the_upscale(self):
        v = self.create()
        job = self.upscale_job(v, "rtxvsr", 3)
        self.assertEqual(job.params["upscale"], {"method": "rtxvsr", "scale": 3, "width": 3840, "height": 2160})
        self.assertEqual(eh.HANDLERS["edit.export"].record(job.params),
                         {"project_id": v["id"], "state_hash": editclip.state_hash(v["state"]), "upscale": {"method": "rtxvsr", "scale": 3, "width": 3840, "height": 2160}})
        self.assertNotIn("upscale", eh.normalize("edit.export", {"project_id": v["id"]}, self.ledger))
        with self.assertRaisesRegex(ValueError, "拡大の方式が不明です"):
            eh.normalize("edit.export", {"project_id": v["id"], "upscale": {"method": "nope", "scale": 2}}, self.ledger)
        self.assertNotIn("upscale", eh.normalize("edit.preview", {"project_id": v["id"], "upscale": {"method": "realesrgan", "scale": 2}}, self.ledger))

    def test_export_with_upscale_registers_two_outputs_and_points_the_project_at_the_upscaled_one(self):
        v = self.create()
        pid, seen = v["id"], []
        job = self.upscale_job(v, "realesrgan", 2)
        with mock.patch.object(upscale, "run_realesrgan", self.fake_runner(seen)), mock.patch.object(upscale, "realesrgan_exe", return_value=Path("/t/exe")):
            asyncio.run(eh.run_export(job, self.ctx))
        (call,) = seen
        dest = self.root / "video" / f"edit_{pid}.mp4"
        self.assertEqual((call["tool"], call["src"], call["dst"], call["scale"], call["fps"], call["work"], call["ffmpeg"]),
                         (Path("/t/exe"), dest, self.root / "video" / f"edit_{pid}_x2.mp4", 2, 24, self.root / "edit" / pid / "upscale", "ffmpeg"))
        self.assertIs(call["procs"], eh._procs)                                                      # 中止は同じ _procs から止める
        self.assertEqual(len(job.outputs), 2)
        orig, up = (self.ledger.get_output(oid) for oid in job.outputs)
        self.assertEqual((orig["filename"], orig["width"], orig["height"], orig["path"]), ("テスト_edit.mp4", 1280, 720, f"video/edit_{pid}.mp4"))
        self.assertEqual((up["filename"], up["width"], up["height"], up["duration_s"], up["tab"], up["job_id"], up["path"]),
                         ("テスト_edit_x2.mp4", 2560, 1440, 11.0, "edit", "j1", f"video/edit_{pid}_x2.mp4"))
        self.assertEqual(up["params"]["upscale"], {"method": "realesrgan", "scale": 2, "source_output_id": orig["id"]})
        self.assertEqual((up["params"]["project_id"], up["params"]["name"], up["params"]["state"]), (pid, "テスト", v["state"]))
        self.assertNotIn("upscale", orig["params"])
        self.assertEqual(self.ledger.get_edit_project(pid)["output_id"], up["id"])
        self.assertEqual((job.progress, job.message), (0.99, "ギャラリーに登録しています"))
        # rtxvsr は道具の Python と品質を渡す
        seen.clear()
        job2 = self.upscale_job(v, "rtxvsr", 3)
        with mock.patch.object(upscale, "run_rtxvsr", self.fake_runner(seen)), mock.patch.object(upscale, "rtxvsr_python", return_value=Path("/t/py")):
            asyncio.run(eh.run_export(job2, self.ctx))
        self.assertEqual((seen[0]["tool"], seen[0]["dst"].name, seen[0]["rest"]), (Path("/t/py"), f"edit_{pid}_2_x3.mp4", ("ULTRA",)))

    def test_upscale_failure_keeps_the_original_and_says_so(self):
        v = self.create()
        job = self.upscale_job(v)
        boom = JobFailed("拡大に失敗しました(Real-ESRGAN(アニメ向け)): boom", "tail")
        with mock.patch.object(upscale, "run_realesrgan", self.fake_runner([], fail=boom)), mock.patch.object(upscale, "realesrgan_exe", return_value=Path("/t/exe")):
            with self.assertRaises(JobFailed) as cm:
                asyncio.run(eh.run_export(job, self.ctx))
        self.assertEqual(cm.exception.message, "拡大に失敗しました(Real-ESRGAN(アニメ向け)): boom(元の大きさの動画はギャラリーに入っています)")
        self.assertEqual(cm.exception.log_tail, "tail")
        (oid,) = job.outputs
        self.assertEqual(self.ledger.get_edit_project(v["id"])["output_id"], oid)
        self.assertEqual(self.ledger.get_output(oid)["filename"], "テスト_edit.mp4")

    def test_upscale_waits_until_the_gpu_engines_are_idle(self):
        v = self.create()
        seen, busy = [], {"h3": 1}
        ctx = Ctx(types.SimpleNamespace(_busy=busy), self.ledger, self.root)
        job = self.upscale_job(v)
        messages = []
        real_setattr = Job.__setattr__

        def spy(self_, name, value):
            if name == "message":
                messages.append(value)
            real_setattr(self_, name, value)

        async def go():
            asyncio.get_running_loop().call_later(0.05, busy.clear)                                  # 生成が終わる
            with mock.patch.object(upscale, "run_realesrgan", self.fake_runner(seen)), mock.patch.object(upscale, "realesrgan_exe", return_value=Path("/t/exe")):
                await eh.run_export(job, ctx)
        with mock.patch.object(upscale, "GPU_WAIT_POLL_S", 0.01), mock.patch.object(Job, "__setattr__", spy):
            asyncio.run(go())
        self.assertEqual(seen[0]["message"], "GPU の生成が終わるのを待っています")                           # 待ってから走った
        self.assertEqual(messages.count("GPU の生成が終わるのを待っています"), 1)
        self.assertEqual(len(job.outputs), 2)
        # 待っている間の中止
        job2 = self.upscale_job(v)
        busy["h3"] = 1

        async def cancel_while_waiting():
            asyncio.get_running_loop().call_later(0.03, lambda: setattr(job2, "cancel_requested", True))
            with mock.patch.object(upscale, "run_realesrgan", self.fake_runner(seen)):
                await eh.run_export(job2, ctx)
        with mock.patch.object(upscale, "GPU_WAIT_POLL_S", 0.01), self.assertRaises(JobCancelled):
            asyncio.run(cancel_while_waiting())
        self.assertEqual((len(seen), len(job2.outputs)), (1, 1))                                     # 元の動画は登録済み、拡大は走らない

    def test_export_without_upscale_is_unchanged(self):
        v = self.create()
        job = self.job("edit.export", v["id"], v["state"])
        with mock.patch.object(upscale, "run_realesrgan") as run:
            asyncio.run(eh.run_export(job, self.ctx))
        run.assert_not_called()
        self.assertEqual(len(job.outputs), 1)


class RunFfmpegTest(unittest.TestCase):
    """本物の run_ffmpeg を、ffmpeg の代わりの Python(-progress の行を流す)で動かす。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dst = Path(tmp.name) / "sub" / "out.mp4"

    def script(self, body: str) -> list:
        return [sys.executable, "-c", "import sys, time\nout = sys.argv[1]\n" + body, str(mv_audio._part(self.dst))]

    def job(self) -> Job:
        return Job(id="jr", tab="edit", kind="edit.export", params={})

    def test_progress_lines_update_the_job_and_result_is_moved_into_place(self):
        seen = []
        job = self.job()
        body = ("print('frame=1', flush=True)\n"
                "print('out_time=N/A', flush=True)\n"
                "print('out_time_us=2500000', flush=True)\n"
                "time.sleep(0.2)\n"
                "print('out_time=00:00:05.000000', flush=True)\n"
                "open(out, 'wb').write(b'data')\n"
                "print('progress=end', flush=True)\n")
        real_setattr = Job.__setattr__

        def spy(self_, name, value):
            if name == "message" and "%" in str(value):
                seen.append(value)
            real_setattr(self_, name, value)
        with mock.patch.object(Job, "__setattr__", spy):
            eh.run_ffmpeg(job, self.script(body), self.dst, 10.0, "書き出しています")
        self.assertEqual(seen, ["書き出しています 25%", "書き出しています 50%"])
        self.assertEqual(self.dst.read_bytes(), b"data")
        self.assertFalse(mv_audio._part(self.dst).exists())
        self.assertEqual(job.progress, 0.5)
        self.assertEqual(eh._procs, {})

    def test_failure_reports_last_stderr_line_and_leaves_nothing(self):
        body = "open(out, 'wb').write(b'half')\nprint('Error: bad filter', file=sys.stderr)\nprint('Conversion failed!', file=sys.stderr)\nsys.exit(1)\n"
        with self.assertRaises(JobFailed) as cm:
            eh.run_ffmpeg(self.job(), self.script(body), self.dst, 10.0, "プレビューを作っています")
        self.assertEqual(cm.exception.message, "プレビューを作っていますに失敗しました(ffmpeg): Conversion failed!")
        self.assertIn("Error: bad filter", cm.exception.log_tail)
        self.assertFalse(self.dst.exists())
        self.assertFalse(mv_audio._part(self.dst).exists())

    def test_empty_output_is_a_failure(self):
        with self.assertRaises(JobFailed):
            eh.run_ffmpeg(self.job(), self.script("open(out, 'wb').close()\n"), self.dst, 1.0, "書き出し")

    def test_missing_executable(self):
        with self.assertRaisesRegex(JobFailed, "ffmpeg を起動できません"):
            eh.run_ffmpeg(self.job(), ["/nonexistent/ffmpeg-xyz"], self.dst, 1.0, "書き出し")

    def test_cancel_kills_the_process(self):
        job = self.job()
        body = "open(out, 'wb').write(b'half')\nprint('out_time_us=1000000', flush=True)\ntime.sleep(60)\n"
        result = {}

        def run():
            try:
                eh.run_ffmpeg(job, self.script(body), self.dst, 10.0, "書き出し")
            except Exception as e:  # noqa: BLE001
                result["error"] = e
        t = threading.Thread(target=run)
        t.start()
        deadline = time.monotonic() + 10
        while job.id not in eh._procs and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(0.3)
        job.cancel_requested = True
        asyncio.run(eh.cancel_ffmpeg(job, None))
        t.join(15)
        self.assertFalse(t.is_alive())
        self.assertIsInstance(result.get("error"), JobCancelled)
        self.assertFalse(mv_audio._part(self.dst).exists())
        self.assertFalse(self.dst.exists())
        self.assertEqual(eh._procs, {})

    def test_stalled_process_is_stopped(self):
        job = self.job()
        real_sleep = time.sleep
        with mock.patch.object(eh, "STALL_S", 0.3):
            with mock.patch.object(eh.time, "sleep", lambda s: real_sleep(0.05)):
                with self.assertRaisesRegex(JobFailed, "進まないので止めました"):
                    eh.run_ffmpeg(job, self.script("time.sleep(60)\n"), self.dst, 10.0, "書き出し")

    def test_progress_seconds(self):
        f = eh.progress_seconds
        self.assertEqual(f("out_time_us=1500000\n"), 1.5)
        self.assertEqual(f("out_time_ms=3000000"), 3.0)
        self.assertEqual(f("out_time=00:01:02.500000"), 62.5)
        self.assertEqual(f("out_time=01:00:00.000000"), 3600.0)
        for line in ("out_time=N/A", "out_time_us=N/A", "out_time=-00:00:00.033", "frame=4", "progress=continue", "", "garbage"):
            self.assertIsNone(f(line), line)
        self.assertEqual(f("out_time_us=-5"), 0.0)


if __name__ == "__main__":
    unittest.main()
