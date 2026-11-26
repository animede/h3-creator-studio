import asyncio
import io
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from studio import media, mv
from studio.handlers import mv as mvh
from studio.jobs import Ctx, Handler, JobCancelled, JobFailed, JobRunner
from studio.ledger import Ledger
from studio.mvclip import quantize_scene_plan
from studio.scheduler import Operation


def png_bytes(size=(64, 48)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (10, 120, 200)).save(buf, "PNG")
    return buf.getvalue()


class FakeSched:
    """ensure の代わり(H3 は、いつでも載っている)。hold を渡すと、それがセットされるまで H3 の準備を「準備中」のまま止める。"""

    def __init__(self, hold=None):
        self.hold = hold
        self.comps = {"h3": types.SimpleNamespace(spec=types.SimpleNamespace(base_url="http://h3.test"))}

    def new_operation(self, kind, name):
        return Operation(kind, name)

    async def ensure(self, name, op=None):
        if self.hold is not None and name == "h3":
            await self.hold.wait()
        op.add("準備完了")


def plan_of(durations) -> list:
    t, out = 0.0, []
    for i, d in enumerate(durations, start=1):
        out.append({"index": i, "start": t, "end": t + d, "duration": d, "vocal_ratio": 0.8 if i % 2 else 0.0,
                    "kind": "vocal" if i % 2 else "instrumental", "boundary_kind": "bar"})
        t += d
    return out


class MvBase(unittest.IsolatedAsyncioTestCase):
    """台帳(実物)・JobRunner(実物)の上に、4 つのジョブの代わり(偽のハンドラ)を載せて Director を動かす。"""
    DURATIONS = [8.0, 6.0, 7.0]

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.ledger = Ledger(self.root)
        self.addCleanup(self.ledger.close)
        for name, ret in (("probe", {"duration_s": None, "width": None, "height": None}), ("video_thumb", False)):      # ダミーの動画で ffmpeg を呼ばない
            p = mock.patch.object(media, name, return_value=ret)
            p.start()
            self.addCleanup(p.stop)
        self.song = self.add("audio", "song.mp3", {"theme": "夜明けの駅", "caption": "A warm ballad", "genre": "Ballad"})
        self.image = self.add("image", "girl.png", {"prompt": "a girl with silver hair, anime style"})
        self.log = []                  # 偽のハンドラが始まった順: ("analyze", None) / ("scene", 2) / ("other", None) など
        self.seen = []                 # 偽のシーンのハンドラが見た (番号, プロンプト, seed)
        self.hold = {}                 # 名前("scene2" など)-> Event。セットされるまで、その仕事を止めておく
        self.waiting = {}              # ジョブの ID -> 止まっている Event(cancel で外す)
        self.failing = {}              # 名前 -> 残り回数(その回数だけ失敗させる)
        self.late_file = False         # True なら、中止された仕事が、終わる直前に作業フォルダへファイルを書く(消された制作の後始末の確認)
        self.ensure_hold = None        # Event を入れると、それがセットされるまで H3 の準備が終わらない(ジョブが準備中のまま)
        self.record_listen = True      # False なら、曲を聞く偽のハンドラは、終わっても結果(曲の内容・listen = done)を制作に書かない
        self.durations = list(self.DURATIONS)

    def add(self, kind: str, name: str, params: dict) -> dict:
        p = self.root / "uploads" / name
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(png_bytes() if kind == "image" else b"RIFFfake")
        return self.ledger.add_output(kind, p, "upload", filename=name, params=params, duration_s=30.0 if kind == "audio" else None)

    # ---- 偽のハンドラ ----

    async def gate(self, job, name):
        ev = self.hold.get(name)
        if ev is not None:
            self.waiting[job.id] = ev
            await ev.wait()
            self.waiting.pop(job.id, None)
        if job.cancel_requested:
            if self.late_file:
                work = mv.work_dir(self.root, job.params["project_id"])
                for _ in range(500):                      # delete() の rmtree(別スレッド)が終わって、フォルダが無くなってから書く(同時に走ると mkdir と競る)
                    if not work.exists():
                        break
                    await asyncio.sleep(0.01)
                work.mkdir(parents=True, exist_ok=True)
                (work / "late.wav").write_bytes(b"x")
            raise JobCancelled()
        if self.failing.get(name, 0) > 0:
            self.failing[name] -= 1
            raise JobFailed(f"{name} が失敗した")

    def handlers(self) -> dict:
        async def analyze(job, ctx):
            self.log.append(("analyze", None))
            await self.gate(job, "analyze")
            scenes = mv.make_scenes(quantize_scene_plan(plan_of(self.durations)))
            ctx.ledger.update_mv_project(job.params["project_id"], scenes=scenes, params_patch={
                "analysis": {"duration": sum(self.durations), "bpm": 120.0, "has_vocals": True, "scenes": len(scenes)}, "assembled": False})

        async def listen(job, ctx):
            self.log.append(("listen", None))
            await self.gate(job, "listen")
            if self.record_listen:
                ctx.ledger.update_mv_project(job.params["project_id"], params_patch={"concept": "聞いて作った案", "concept_source": "listen", "listen": "done"})

        async def script(job, ctx):
            self.log.append(("script", None))
            await self.gate(job, "script")
            proj = ctx.ledger.get_mv_project(job.params["project_id"])
            ctx.ledger.update_mv_project(proj["id"], params_patch={"script": {"title": "T"}}, scene_patches={
                s["index"]: {"prompt": f"prompt {s['index']}", "seed": 1000 + s["index"], "emotion": "calm"} for s in proj["scenes"]})

        async def scene(job, ctx):
            i, pid = job.params["scene"], job.params["project_id"]
            sc = next(s for s in ctx.ledger.get_mv_project(pid)["scenes"] if s["index"] == i)
            self.log.append(("scene", i))
            self.seen.append((i, sc["prompt"], sc["seed"]))
            job.progress, job.message = 0.5, f"シーン {i}: 生成中 2/4"
            await self.gate(job, f"scene{i}")
            clip = mv.work_dir(ctx.outputs_dir, pid) / mv.scene_clip_name(i)
            clip.write_bytes(b"clip")
            ctx.ledger.update_mv_project(pid, scene_patches={i: {"status": "done", "clip": clip.name, "thumb": None, "elapsed_s": 1.5,
                                                                 "peak_vram_gb": 8.0, "error": None}})

        async def assemble(job, ctx):
            self.log.append(("assemble", None))
            await self.gate(job, "assemble")
            pid = job.params["project_id"]
            dest = mvh.final_path(ctx.outputs_dir, pid)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"mp4")
            rec = ctx.ledger.add_output("video", dest, job.tab, job.id, "mv.mp4", {"project_id": pid}, width=768, height=448, duration_s=21.0)
            job.outputs.append(rec["id"])
            ctx.ledger.update_mv_project(pid, output_id=rec["id"], params_patch={"assembled": True})

        async def cancel(job, ctx):
            ev = self.waiting.get(job.id)
            if ev is not None:
                ev.set()

        async def other(job, ctx):          # 他のタブの仕事(H3 のレーンを使う)
            self.log.append(("other", None))
            await self.gate(job, "other")

        def h(component, run, gpu=True):
            return Handler(component, mvh.normalize, run, cancel, gpu=gpu)
        return {"mv.analyze": h(None, analyze, False), "mv.listen": h(None, listen, False), "mv.script": h(None, script, False), "mv.scene": h("h3", scene),
                "mv.assemble": h(None, assemble, False),
                "h3.t2va": Handler("h3", lambda kind, params, ledger: dict(params), other, cancel)}

    def start(self, handlers=None) -> mv.Director:
        self.runner = JobRunner(Ctx(FakeSched(self.ensure_hold), self.ledger, self.root), handlers or self.handlers(), step_poll_s=0.01)
        self.runner.start()
        self.addAsyncCleanup(self.runner.stop)
        self.director = mv.Director(self.runner, self.ledger, self.root)
        return self.director

    def new_project(self, **kw) -> dict:
        return self.director.create({"song_id": self.song["id"], "image_id": self.image["id"], **kw})

    # ---- 待つ・読む ----

    async def until(self, cond, what="条件", timeout=5.0):
        for _ in range(int(timeout / 0.01)):
            if cond():
                return
            await asyncio.sleep(0.01)
        self.fail(f"待っても {what} にならない")

    def state(self, pid) -> str:
        return self.ledger.get_mv_project(pid)["state"]

    async def until_state(self, pid, *states, timeout=5.0):
        await self.until(lambda: self.state(pid) in states, f"状態が {states}(いま {self.state(pid)})", timeout)

    def scene_status(self, pid) -> list:
        return [s["status"] for s in self.ledger.get_mv_project(pid)["scenes"]]

    def mv_jobs(self, kind=None) -> list:
        return [j for j in self.runner.jobs.values() if j.tab == "mv" and (kind is None or j.kind == kind)]

    async def settle(self, seconds=0.1):
        await asyncio.sleep(seconds)


class FlowTest(MvBase):
    async def test_full_flow_in_order_to_completed(self):
        self.start()
        view = self.new_project()
        pid = view["id"]
        self.assertEqual((view["state"], view["progress"] < 0.05), ("analyzing", True))
        await self.until_state(pid, "completed")
        self.assertEqual(self.log, [("analyze", None), ("script", None), ("scene", 1), ("scene", 2), ("scene", 3), ("assemble", None)])
        proj = self.ledger.get_mv_project(pid)
        self.assertEqual((proj["message"], proj["error"]), ("MV ができました", None))
        self.assertEqual(self.scene_status(pid), ["done"] * 3)
        out = self.ledger.get_output(proj["output_id"])
        self.assertEqual((out["kind"], out["tab"], out["params"]["project_id"]), ("video", "mv", pid))
        v = self.director.view(pid)
        self.assertEqual((v["state"], v["progress"], v["job_id"], v["output"]["id"], v["assembled"]), ("completed", 1.0, None, out["id"], True))
        self.assertEqual([j.kind for j in self.mv_jobs()], ["mv.analyze", "mv.script", "mv.scene", "mv.scene", "mv.scene", "mv.assemble"])
        self.assertTrue(all(j.tab == "mv" and j.params["project_id"] == pid for j in self.mv_jobs()))
        self.assertEqual([j.lane for j in self.mv_jobs()], ["cpu", "cpu", "h3", "h3", "h3", "cpu"])           # 分析・演出・結合は CPU のレーン
        (item,) = self.director.list()
        self.assertEqual((item["id"], item["state"], item["scenes"], item["done"], item["output"]["id"]), (pid, "completed", 3, 3, out["id"]))

    async def test_states_follow_the_stages(self):
        self.start()
        names = ("analyze", "script", "scene1", "scene2", "scene3", "assemble")
        for name in names:
            self.hold[name] = asyncio.Event()
        pid = self.new_project()["id"]
        seen = []
        for k, name in enumerate(names):          # 段階ごとに、そのジョブが動いている間の制作の状態を見てから、次へ進める
            await self.until(lambda k=k: len(self.mv_jobs()) > k and self.mv_jobs()[k].id in self.waiting, f"{name} が動き出す")
            seen.append((self.mv_jobs()[k].kind, self.state(pid)))
            self.hold[name].set()
            await self.until(lambda k=k: self.mv_jobs()[k].status == "completed", f"{name} が終わる")
        await self.until_state(pid, "completed")
        self.assertEqual(seen, [("mv.analyze", "analyzing"), ("mv.script", "scripting"), ("mv.scene", "generating"), ("mv.scene", "generating"),
                                ("mv.scene", "generating"), ("mv.assemble", "assembling")])

    async def test_next_scene_waits_for_the_previous_and_other_tab_jobs_interleave(self):
        self.start()
        self.hold["scene1"] = asyncio.Event()
        pid = self.new_project()["id"]
        await self.until(lambda: ("scene", 1) in self.log, "シーン 1 が始まる")
        self.assertEqual([j.params["scene"] for j in self.mv_jobs("mv.scene")], [1])           # 次のシーンは、まだ入れていない
        other = self.runner.submit("video", "h3.t2va", {})                                     # 別のタブの仕事(同じ H3 のレーン)
        self.hold["scene1"].set()
        await self.until_state(pid, "completed")
        self.assertEqual(other.status, "completed")
        self.assertEqual(self.log[2:6], [("scene", 1), ("other", None), ("scene", 2), ("scene", 3)])      # シーン 1 のすぐ後に、割り込んだ仕事
        order = list(self.runner.jobs)
        self.assertLess(order.index(other.id), order.index(self.mv_jobs("mv.scene")[1].id))               # 受け付けた順

    async def test_two_projects_run_independently(self):
        self.start()
        a, b = self.new_project()["id"], self.new_project()["id"]
        await self.until_state(a, "completed")
        await self.until_state(b, "completed")
        self.assertEqual({j.params["project_id"] for j in self.mv_jobs()}, {a, b})
        self.assertEqual(len([e for e in self.log if e == ("assemble", None)]), 2)

    async def test_failed_scene_marks_the_scene_and_resume_continues(self):
        self.start()
        self.failing["scene2"] = 1
        pid = self.new_project()["id"]
        await self.until_state(pid, "failed")
        proj = self.ledger.get_mv_project(pid)
        self.assertEqual(self.scene_status(pid), ["done", "failed", "pending"])
        self.assertEqual((proj["error"], proj["message"]), ("scene2 が失敗した", "シーンの生成に失敗しました"))
        self.assertEqual(proj["scenes"][1]["error"], "scene2 が失敗した")
        self.assertEqual(self.director.view(pid)["scenes"][1]["status"], "failed")
        await self.settle()
        self.assertEqual(self.log.count(("scene", 3)), 0)          # 失敗したら、続きは入れない
        self.director.resume(pid)                                  # 再開: 失敗したシーンから続ける
        await self.until_state(pid, "completed")
        self.assertEqual(self.log[2:], [("scene", 1), ("scene", 2), ("scene", 2), ("scene", 3), ("assemble", None)])
        self.assertEqual((self.ledger.get_mv_project(pid)["error"], self.scene_status(pid)), (None, ["done"] * 3))
        self.assertIsNone(self.ledger.get_mv_project(pid)["scenes"][1]["error"])

    async def test_failed_analyze_and_script_stages_resume_from_the_same_stage(self):
        self.start()
        self.failing["analyze"], self.failing["script"] = 1, 1
        pid = self.new_project()["id"]
        await self.until_state(pid, "failed")
        self.assertEqual(self.ledger.get_mv_project(pid)["message"], "曲の分析に失敗しました")
        self.director.resume(pid)
        await self.until(lambda: self.ledger.get_mv_project(pid)["message"] == "演出の作成に失敗しました", "演出の失敗")
        self.assertEqual(self.log, [("analyze", None), ("analyze", None), ("script", None)])      # 分析は済んだので、演出の続きから
        self.director.resume(pid)
        await self.until_state(pid, "completed")
        self.assertEqual(self.log.count(("script", None)), 2)
        self.assertEqual(self.log.count(("analyze", None)), 2)

    async def test_completed_job_without_a_recorded_result_fails_instead_of_looping(self):
        handlers = self.handlers()

        async def lazy_analyze(job, ctx):
            self.log.append(("analyze", None))             # 結果を何も書かずに完了する
        handlers["mv.analyze"] = Handler(None, mvh.normalize, lazy_analyze, handlers["mv.analyze"].cancel, gpu=False)
        self.start(handlers)
        pid = self.new_project()["id"]
        await self.until_state(pid, "failed")
        await self.settle()
        self.assertEqual(self.log, [("analyze", None)])       # 同じジョブを繰り返し入れない
        self.assertIn("結果を記録できませんでした", self.ledger.get_mv_project(pid)["message"])

    async def test_job_cancelled_outside_pauses_the_project(self):
        self.start()
        self.hold["scene2"] = asyncio.Event()
        pid = self.new_project()["id"]
        await self.until(lambda: ("scene", 2) in self.log, "シーン 2 が始まる")
        job = self.mv_jobs("mv.scene")[1]
        await self.runner.cancel(job.id)                      # ジョブの一覧など、制作の画面の外から中止された
        await self.until_state(pid, "paused")
        self.assertEqual(self.scene_status(pid), ["done", "pending", "pending"])
        self.assertIn("中止された", self.ledger.get_mv_project(pid)["message"])
        self.director.resume(pid)
        self.hold["scene2"].set()
        await self.until_state(pid, "completed")


class PauseCancelTest(MvBase):
    async def test_pause_lets_the_running_job_finish_and_submits_nothing_new_until_resume(self):
        self.start()
        self.hold["scene2"] = asyncio.Event()
        pid = self.new_project()["id"]
        await self.until(lambda: ("scene", 2) in self.log and self.waiting, "シーン 2 が走る")
        view = await self.director.pause(pid)
        self.assertEqual(view["state"], "paused")
        self.assertIn("いまの仕事が終わる", view["message"])
        self.assertIsNotNone(view["job_id"])                  # 走っているジョブは、そのまま続く
        self.assertEqual((await self.director.pause(pid))["state"], "paused")       # 二重に押しても同じ
        self.hold["scene2"].set()
        await self.until(lambda: self.scene_status(pid)[1] == "done", "シーン 2 が終わる")
        await self.settle(0.15)
        self.assertEqual((self.state(pid), self.scene_status(pid)), ("paused", ["done", "done", "pending"]))
        self.assertEqual(self.log.count(("scene", 3)), 0)     # 次は入れない
        self.assertEqual(self.ledger.get_mv_project(pid)["message"], "一時停止しました")
        self.assertIsNone(self.director.view(pid)["job_id"])
        self.director.resume(pid)
        await self.until_state(pid, "completed")
        self.assertEqual(self.log[-2:], [("scene", 3), ("assemble", None)])         # 続きから(やり直さない)
        self.assertEqual(self.log.count(("scene", 1)), 1)

    async def test_resume_while_the_paused_job_is_still_running_just_continues(self):
        self.start()
        self.hold["scene1"] = asyncio.Event()
        pid = self.new_project()["id"]
        await self.until(lambda: ("scene", 1) in self.log, "シーン 1 が始まる")
        await self.director.pause(pid)
        view = self.director.resume(pid)                      # 止まる前に再開: 走っているジョブの続きとして扱う
        self.assertEqual(view["state"], "generating")
        self.hold["scene1"].set()
        await self.until_state(pid, "completed")
        self.assertEqual([e for e in self.log if e[0] == "scene"], [("scene", 1), ("scene", 2), ("scene", 3)])

    async def test_pause_withdraws_a_queued_job_so_it_does_not_start_later(self):
        self.start()
        self.hold["other"] = asyncio.Event()
        self.runner.submit("video", "h3.t2va", {})                              # H3 のレーンを他のタブの仕事が塞いでいる
        await self.until(lambda: ("other", None) in self.log, "他のタブの仕事が走る")
        pid = self.new_project()["id"]
        await self.until(lambda: any(j.kind == "mv.scene" for j in self.mv_jobs()), "シーン 1 のジョブが入る(順番待ち)")
        job = self.mv_jobs("mv.scene")[0]
        self.assertEqual(job.status, "queued")
        view = await self.director.pause(pid)
        self.assertEqual((job.status, view["state"]), ("cancelled", "paused"))     # まだ始まっていないジョブは取り下げる
        await self.until(lambda: self.scene_status(pid)[0] == "pending", "シーンが未生成に戻る")
        self.hold["other"].set()
        await self.settle(0.2)
        self.assertEqual(self.log.count(("scene", 1)), 0)                       # 順番が来ても、勝手に始まらない
        self.assertEqual((self.state(pid), self.ledger.get_mv_project(pid)["message"]), ("paused", "一時停止しました"))
        self.assertIsNone(self.director.view(pid)["job_id"])
        self.director.resume(pid)
        await self.until_state(pid, "completed")
        self.assertEqual([e for e in self.log if e[0] == "scene"], [("scene", 1), ("scene", 2), ("scene", 3)])

    async def test_pause_withdraws_a_job_that_is_still_preparing(self):
        self.ensure_hold = asyncio.Event()                                     # H3 の準備が終わらない(ジョブが準備中のまま)
        self.start()
        pid = self.new_project()["id"]
        await self.until(lambda: any(j.kind == "mv.scene" and j.status == "preparing" for j in self.mv_jobs()), "シーン 1 が準備中になる")
        first = self.mv_jobs("mv.scene")[0]
        view = await self.director.pause(pid)
        self.assertEqual(view["state"], "paused")
        self.assertIn("取り下げています", view["message"])
        self.assertTrue(first.cancel_requested)
        self.ensure_hold.set()                                                 # 準備が終わると、取り下げたジョブは走らずに終わる
        await self.until(lambda: first.status == "cancelled", "取り下げたジョブが終わる")
        await self.until(lambda: self.scene_status(pid)[0] == "pending", "シーンが未生成に戻る")
        await self.settle()
        self.assertEqual(self.log.count(("scene", 1)), 0)
        self.assertEqual(self.ledger.get_mv_project(pid)["message"], "一時停止しました")
        self.director.resume(pid)
        await self.until_state(pid, "completed")
        self.assertEqual(self.log.count(("scene", 1)), 1)

    async def test_resume_right_after_pausing_a_preparing_job_submits_a_fresh_job(self):
        """準備中で取り下げたジョブがまだ終わりきっていなくても、再開すれば、新しいジョブを入れて続ける(取り下げたジョブの終わりは無視する)。"""
        self.ensure_hold = asyncio.Event()
        self.start()
        pid = self.new_project()["id"]
        await self.until(lambda: any(j.kind == "mv.scene" and j.status == "preparing" for j in self.mv_jobs()), "シーン 1 が準備中になる")
        first = self.mv_jobs("mv.scene")[0]
        await self.director.pause(pid)
        view = self.director.resume(pid)
        self.assertEqual(view["state"], "generating")
        second = self.mv_jobs("mv.scene")[1]
        self.assertNotEqual(first.id, second.id)
        self.ensure_hold.set()
        await self.until_state(pid, "completed")
        self.assertEqual((first.status, second.status), ("cancelled", "completed"))
        self.assertEqual([e for e in self.log if e[0] == "scene"], [("scene", 1), ("scene", 2), ("scene", 3)])        # シーン 1 は 1 回だけ
        self.assertEqual(self.scene_status(pid), ["done"] * 3)

    async def test_pause_and_resume_validation(self):
        self.start()
        pid = self.new_project()["id"]
        await self.until_state(pid, "completed")
        with self.assertRaises(ValueError) as cm:
            await self.director.pause(pid)
        self.assertIn("一時停止できるのは", str(cm.exception))
        with self.assertRaises(ValueError) as cm:
            self.director.resume(pid)
        self.assertIn("再開できるのは", str(cm.exception))
        with self.assertRaises(LookupError):
            await self.director.pause("nope")

    async def test_cancel_stops_the_running_job_and_resume_picks_up_the_scene_again(self):
        self.start()
        self.hold["scene2"] = asyncio.Event()
        pid = self.new_project()["id"]
        await self.until(lambda: ("scene", 2) in self.log and self.waiting, "シーン 2 が走る")
        view = await self.director.cancel(pid)
        self.assertEqual((view["state"], view["message"]), ("cancelled", "中止しました"))
        job = self.mv_jobs("mv.scene")[1]
        await self.until(lambda: job.status == "cancelled", "ジョブが中止される")
        await self.until(lambda: self.scene_status(pid)[1] == "pending", "シーンが未生成に戻る")
        await self.settle()
        self.assertEqual((self.state(pid), self.scene_status(pid)), ("cancelled", ["done", "pending", "pending"]))
        self.assertEqual(self.log.count(("scene", 3)), 0)
        self.assertEqual((await self.director.cancel(pid))["state"], "cancelled")       # 二重に押しても同じ
        self.hold["scene2"].clear()
        self.hold["scene2"].set()
        self.director.resume(pid)                             # 中止した制作も、続きから再開できる
        await self.until_state(pid, "completed")
        self.assertEqual(self.log.count(("scene", 1)), 1)
        self.assertEqual(self.log.count(("scene", 2)), 2)

    async def test_cancel_validation(self):
        self.start()
        pid = self.new_project()["id"]
        await self.until_state(pid, "completed")
        with self.assertRaises(ValueError) as cm:
            await self.director.cancel(pid)
        self.assertIn("終わった制作は中止できません", str(cm.exception))

    async def test_cancel_while_paused_with_nothing_running(self):
        self.start()
        self.hold["scene1"] = asyncio.Event()
        pid = self.new_project()["id"]
        await self.until(lambda: ("scene", 1) in self.log, "シーン 1 が始まる")
        await self.director.pause(pid)
        self.hold["scene1"].set()
        await self.until(lambda: self.scene_status(pid)[0] == "done", "シーン 1 が終わる")
        await self.settle()
        self.assertEqual((await self.director.cancel(pid))["state"], "cancelled")


class RetryTest(MvBase):
    async def completed(self) -> str:
        self.start()
        pid = self.new_project()["id"]
        await self.until_state(pid, "completed")
        self.log.clear()
        self.seen.clear()
        return pid

    async def test_retry_with_edited_prompt_and_seed_then_reassembles(self):
        pid = await self.completed()
        old_output = self.ledger.get_mv_project(pid)["output_id"]
        view = self.director.retry_scene(pid, 2, prompt="  an edited prompt  ", seed=4242)
        self.assertEqual((view["state"], view["assembled"]), ("generating", False))
        self.assertEqual([(s["index"], s["status"]) for s in view["scenes"]][1], (2, "running"))
        self.assertEqual(self.scene_status(pid), ["done", "running", "done"])
        await self.until_state(pid, "completed")
        self.assertEqual(self.log, [("scene", 2), ("assemble", None)])           # そのシーンだけ作り直し、自動で結合し直す
        self.assertEqual(self.seen, [(2, "an edited prompt", 4242)])
        proj = self.ledger.get_mv_project(pid)
        self.assertEqual((proj["scenes"][1]["prompt"], proj["scenes"][1]["seed"]), ("an edited prompt", 4242))
        self.assertEqual((proj["scenes"][0]["prompt"], proj["scenes"][0]["seed"]), ("prompt 1", 1001))      # 他のシーンはそのまま
        self.assertNotEqual(proj["output_id"], old_output)
        self.assertIsNotNone(self.ledger.get_output(old_output))                  # 前の MV はギャラリーに残る
        self.assertTrue(self.ledger.abs_path(self.ledger.get_output(old_output)["path"]).is_file())
        self.assertEqual(len(self.ledger.list_outputs(kind="video", tab="mv")), 2)
        self.assertNotEqual(self.ledger.get_output(old_output)["path"], self.ledger.get_output(proj["output_id"])["path"])

    async def test_plain_retry_takes_a_new_seed_and_keeps_the_prompt(self):
        pid = await self.completed()
        self.director.retry_scene(pid, 1)
        await self.until_state(pid, "completed")
        (i, prompt, seed) = self.seen[0]
        self.assertEqual((i, prompt), (1, "prompt 1"))
        self.assertNotEqual(seed, 1001)
        self.director.retry_scene(pid, 3, prompt="only the prompt changed")             # プロンプトだけ変えたときは、seed はそのまま
        await self.until(lambda: len(self.seen) == 2, "シーン 3 の再生成")
        self.assertEqual(self.seen[1], (3, "only the prompt changed", 1003))
        await self.until_state(pid, "completed")

    async def test_retry_validation(self):
        pid = await self.completed()
        for kwargs, word in (({"index": 9}, "シーン 9 はありません"), ({"index": "x"}, "整数"), ({"index": 1, "prompt": "   "}, "プロンプトは"),
                             ({"index": 1, "prompt": "x" * 8001}, "プロンプトは"), ({"index": 1, "seed": "abc"}, "seed は整数")):
            with self.assertRaises(ValueError, msg=str(kwargs)) as cm:
                self.director.retry_scene(pid, **kwargs)
            self.assertIn(word, str(cm.exception))
        with self.assertRaises(LookupError):
            self.director.retry_scene("nope", 1)
        self.assertEqual((self.state(pid), self.scene_status(pid)), ("completed", ["done"] * 3))        # 断ったら何も変えない
        early = mv.Director(self.runner, self.ledger, self.root)
        self.ledger.create_mv_project("early", "analyzing", {"analysis": {}}, mv.make_scenes(quantize_scene_plan(plan_of([8.0]))), "")
        with self.assertRaises(ValueError) as cm:
            early.retry_scene("early", 1)                      # 演出がまだ
        self.assertIn("演出ができていない", str(cm.exception))

    async def test_retry_refused_for_the_running_scene_and_while_assembling(self):
        self.start()
        self.hold["scene2"] = asyncio.Event()
        pid = self.new_project()["id"]
        await self.until(lambda: ("scene", 2) in self.log and self.waiting, "シーン 2 が走る")
        with self.assertRaises(ValueError) as cm:
            self.director.retry_scene(pid, 2)
        self.assertIn("生成中", str(cm.exception))
        self.hold["scene2"].set()
        self.hold["assemble"] = asyncio.Event()
        await self.until(lambda: ("assemble", None) in self.log, "結合が始まる")
        with self.assertRaises(ValueError) as cm:
            self.director.retry_scene(pid, 1)
        self.assertIn("結合しています", str(cm.exception))
        with self.assertRaises(ValueError) as cm:
            self.director.reassemble(pid)
        self.assertIn("制作中", str(cm.exception))
        self.hold["assemble"].set()
        await self.until_state(pid, "completed")

    async def test_retry_of_an_earlier_scene_while_running_is_picked_up_after_the_current_one(self):
        self.start()
        self.hold["scene2"] = asyncio.Event()
        pid = self.new_project()["id"]
        await self.until(lambda: ("scene", 2) in self.log and self.waiting, "シーン 2 が走る")
        view = self.director.retry_scene(pid, 1, prompt="again")
        self.assertEqual(view["state"], "generating")
        self.assertEqual(self.scene_status(pid), ["pending", "running", "pending"])
        self.hold["scene2"].set()
        await self.until_state(pid, "completed")
        self.assertEqual([e for e in self.log if e[0] == "scene"], [("scene", 1), ("scene", 2), ("scene", 1), ("scene", 3)])
        self.assertEqual(self.seen[2][:2], (1, "again"))
        self.assertEqual(self.log[-1], ("assemble", None))
        self.assertEqual(self.log.count(("assemble", None)), 1)

    async def test_retry_resumes_a_failed_project(self):
        self.start()
        self.failing["scene3"] = 1
        pid = self.new_project()["id"]
        await self.until_state(pid, "failed")
        self.director.retry_scene(pid, 1, seed=7)               # 失敗しているプロジェクトでも、シーンをやり直せば続きから進む
        await self.until_state(pid, "completed")
        self.assertEqual([e for e in self.log if e[0] == "scene"], [("scene", 1), ("scene", 2), ("scene", 3), ("scene", 1), ("scene", 3)])

    async def test_reassemble(self):
        pid = await self.completed()
        first = self.ledger.get_mv_project(pid)["output_id"]
        view = self.director.reassemble(pid)
        self.assertEqual((view["state"], view["assembled"]), ("assembling", False))
        await self.until_state(pid, "completed")
        self.assertEqual(self.log, [("assemble", None)])
        self.assertNotEqual(self.ledger.get_mv_project(pid)["output_id"], first)
        self.ledger.update_mv_project(pid, scene_patches={2: {"status": "failed"}})
        with self.assertRaises(ValueError) as cm:
            self.director.reassemble(pid)
        self.assertIn("全部のシーンができてから", str(cm.exception))


class RestoreDeleteTest(MvBase):
    def make_rows(self):
        scenes = mv.make_scenes(quantize_scene_plan(plan_of([8.0, 6.0])))
        scenes[0].update(status="done", prompt="p", clip="scene_001.mp4")
        scenes[1].update(status="running", prompt="p")
        params = {"analysis": {"duration": 14.0}}
        for pid, state in (("gen", "generating"), ("ana", "analyzing"), ("scr", "scripting"), ("asm", "assembling"),
                           ("done", "completed"), ("pau", "paused"), ("fai", "failed"), ("can", "cancelled")):
            self.ledger.create_mv_project(pid, state, params, [dict(s) for s in scenes], "途中")

    async def test_restart_pauses_running_projects(self):
        self.make_rows()
        self.start()
        self.assertEqual(self.director.restore(), 4)
        for pid in ("gen", "ana", "scr", "asm"):
            proj = self.ledger.get_mv_project(pid)
            self.assertEqual((proj["state"], proj["message"]), ("paused", "管理役の再起動で一時停止しました"), pid)
        self.assertEqual(self.scene_status("gen"), ["done", "pending"])        # 「生成中」だったシーンは、未生成に戻る
        for pid, state in (("done", "completed"), ("pau", "paused"), ("fai", "failed"), ("can", "cancelled")):
            self.assertEqual(self.state(pid), state)                          # 走っていない制作の状態はそのまま
            self.assertEqual(self.scene_status(pid), ["done", "pending"], pid)  # 一時停止・中止の後も走っていたシーンのジョブは消えたので、未生成に戻す
        self.assertEqual(self.director.restore(), 0)
        self.assertEqual(self.mv_jobs(), [])                                  # 勝手にジョブを入れない

    async def test_restart_then_resume_continues_from_the_first_incomplete_scene(self):
        self.start()
        self.hold["scene2"] = asyncio.Event()
        pid = self.new_project()["id"]
        await self.until(lambda: ("scene", 2) in self.log and self.waiting, "シーン 2 が走る")
        # 管理役が落ちて立ち上がり直した、の代わり: 新しい JobRunner と Director で台帳から読み直す
        await self.runner.stop()
        self.log.clear()
        self.hold.clear()
        self.waiting.clear()
        self.start()
        self.assertEqual(self.director.restore(), 1)
        view = self.director.view(pid)
        self.assertEqual((view["state"], view["message"], view["job_id"]), ("paused", "管理役の再起動で一時停止しました", None))
        self.assertEqual([s["status"] for s in view["scenes"]], ["done", "pending", "pending"])
        self.director.resume(pid)
        await self.until_state(pid, "completed")
        self.assertEqual(self.log, [("scene", 2), ("scene", 3), ("assemble", None)])      # 分析・演出・済んだシーンはやり直さない

    async def test_delete_removes_the_work_dir_but_keeps_the_final_mv(self):
        self.start()
        pid = self.new_project()["id"]
        await self.until_state(pid, "completed")
        out = self.ledger.get_output(self.ledger.get_mv_project(pid)["output_id"])
        work = mv.work_dir(self.root, pid)
        self.assertTrue(any(work.iterdir()))
        await self.director.delete(pid)
        self.assertFalse(work.exists())
        self.assertIsNone(self.ledger.get_mv_project(pid))
        self.assertIsNone(self.director.view(pid))
        self.assertEqual(self.director.list(), [])
        self.assertTrue(self.ledger.abs_path(out["path"]).is_file())           # 最終の MV はギャラリーに残る
        self.assertIsNotNone(self.ledger.get_output(out["id"]))
        with self.assertRaises(LookupError):
            await self.director.delete(pid)

    async def test_delete_while_running_cancels_the_job_and_cleans_up_after_it(self):
        self.start()
        self.late_file = True            # 中止されたジョブが、フォルダを消された後にファイルを書いてしまう場合
        self.hold["scene1"] = asyncio.Event()
        pid = self.new_project()["id"]
        await self.until(lambda: ("scene", 1) in self.log and self.waiting, "シーン 1 が走る")
        job = self.mv_jobs("mv.scene")[0]
        await self.director.delete(pid)
        self.assertIsNone(self.ledger.get_mv_project(pid))
        await self.until(lambda: job.status == "cancelled", "ジョブが中止される")
        await self.settle()
        self.assertFalse(mv.work_dir(self.root, pid).exists())                  # 終わりの通知で、後から書かれたファイルも片づく
        self.assertEqual(self.log, [("analyze", None), ("script", None), ("scene", 1)])        # 次のジョブは入れない
        self.assertEqual(self.director._active, {})


class ListenFlowTest(MvBase):
    """曲の台帳に情報が無く、「曲の内容」も空の制作には、分析の後に「曲を聞く」段(LLM が音声を聞く。CPU のレーン)が入る。聞けなくても MV は作る。"""

    def external(self, name="ext.mp3") -> dict:
        return self.add("audio", name, {"uploaded": True})

    def new_external(self, **kw) -> dict:
        return self.director.create({"song_id": self.external()["id"], "image_id": self.image["id"], **kw})

    async def test_external_song_gets_a_listen_stage_between_analyze_and_script(self):
        self.start()
        self.hold["listen"] = asyncio.Event()
        view = self.new_external()
        pid = view["id"]
        self.assertEqual(self.ledger.get_mv_project(pid)["params"]["listen"], "pending")
        await self.until(lambda: ("listen", None) in self.log and self.waiting, "曲を聞く段が動き出す")
        self.assertEqual(self.log, [("analyze", None), ("listen", None)])                       # 分析のあと、演出の前
        self.assertEqual(self.state(pid), "analyzing")                                          # 状態は「分析中」のまま
        self.assertEqual(self.ledger.get_mv_project(pid)["message"], "曲を聞いて、曲の内容を考えます")
        (listen_job,) = self.mv_jobs("mv.listen")
        self.assertEqual((listen_job.lane, listen_job.params), ("cpu", {"project_id": pid}))          # GPU も ACE-Step も使わない(載せ替えなし)
        self.assertGreater(self.director.view(pid)["progress"], 0.0)
        self.hold["listen"].set()
        await self.until_state(pid, "completed")
        self.assertEqual(self.log, [("analyze", None), ("listen", None), ("script", None), ("scene", 1), ("scene", 2), ("scene", 3), ("assemble", None)])
        self.assertEqual([j.kind for j in self.mv_jobs()][:3], ["mv.analyze", "mv.listen", "mv.script"])
        v = self.director.view(pid)
        self.assertEqual((v["params"]["concept"], v["sources"]["concept"]), ("聞いて作った案", "listen"))
        self.assertEqual(v["sources"]["character"], "image")                                    # 画像のプロンプト(setUp の画像)から

    async def test_a_studio_song_has_no_listen_stage(self):
        self.start()
        pid = self.new_project()["id"]                                                          # 音楽タブの曲(テーマ・キャプション・ジャンルがある)
        params = self.ledger.get_mv_project(pid)["params"]
        self.assertEqual((params["concept"], params["concept_source"], params["listen"]), ("夜明けの駅\nA warm ballad\nBallad", "song", ""))
        await self.until_state(pid, "completed")
        self.assertEqual(self.log, [("analyze", None), ("script", None), ("scene", 1), ("scene", 2), ("scene", 3), ("assemble", None)])
        self.assertEqual(self.mv_jobs("mv.listen"), [])
        self.assertEqual(self.director.view(pid)["sources"]["concept"], "song")

    async def test_a_written_concept_is_never_replaced(self):
        self.start()
        pid = self.new_external(concept="自分で書いた世界観")["id"]
        await self.until_state(pid, "completed")
        self.assertNotIn(("listen", None), self.log)                                            # 書いてあれば、聞かない
        v = self.director.view(pid)
        self.assertEqual((v["params"]["concept"], v["sources"]["concept"]), ("自分で書いた世界観", "typed"))

    async def test_a_failed_listen_continues_without_a_concept_and_says_why(self):
        self.start()
        self.failing["listen"] = 1
        pid = self.new_external()["id"]
        await self.until_state(pid, "completed")                                                # 失敗しても、MV は作る
        self.assertEqual(self.log[:3], [("analyze", None), ("listen", None), ("script", None)])
        self.assertEqual(self.log.count(("listen", None)), 1)                                   # もう聞き直さない
        v = self.director.view(pid)
        self.assertEqual((v["params"]["concept"], v["sources"]["concept"]), ("", ""))
        self.assertEqual(v["source_notes"]["concept"], "listen が失敗した")
        self.assertEqual((v["error"], v["state"]), (None, "completed"))
        self.assertEqual(self.ledger.get_mv_project(pid)["params"]["listen"], "skipped")

    async def test_pause_during_listen_waits_for_it_and_resume_goes_on_to_the_script(self):
        self.start()
        self.hold["listen"] = asyncio.Event()
        self.failing["listen"] = 1                                                              # 一時停止の間に、失敗で終わる
        pid = self.new_external()["id"]
        await self.until(lambda: self.waiting and ("listen", None) in self.log, "曲を聞く段が動き出す")
        await self.director.pause(pid)
        self.hold["listen"].set()
        await self.until(lambda: self.mv_jobs("mv.listen")[0].status == "failed", "曲を聞く段が失敗で終わる")
        await self.settle()
        self.assertEqual(self.state(pid), "paused")                                             # 一時停止のまま(失敗にはしない)
        self.assertEqual(self.ledger.get_mv_project(pid)["params"]["listen"], "skipped")
        self.director.resume(pid)
        await self.until_state(pid, "completed")
        self.assertEqual(self.log.count(("listen", None)), 1)

    async def test_cancel_during_listen_then_resume_listens_again(self):
        self.start()
        self.hold["listen"] = asyncio.Event()
        pid = self.new_external()["id"]
        await self.until(lambda: self.waiting and ("listen", None) in self.log, "曲を聞く段が動き出す")
        await self.director.cancel(pid)
        await self.until(lambda: self.mv_jobs("mv.listen")[0].status == "cancelled", "曲を聞く段が止まる")
        self.assertEqual(self.state(pid), "cancelled")
        self.assertEqual(self.ledger.get_mv_project(pid)["params"]["listen"], "pending")        # 聞けていない: 続きから、また聞く
        self.hold["listen"].set()
        self.director.resume(pid)
        await self.until_state(pid, "completed")
        self.assertEqual(self.log.count(("listen", None)), 2)

    async def test_a_listen_that_records_nothing_fails_instead_of_looping(self):
        self.start()
        self.record_listen = False
        pid = self.new_external()["id"]
        await self.until_state(pid, "failed")
        proj = self.ledger.get_mv_project(pid)
        self.assertEqual(proj["message"], "曲を聞く処理の結果を記録できませんでした")
        await self.settle()
        self.assertEqual(self.log.count(("listen", None)), 1)

    async def test_a_listen_cancelled_from_outside_pauses_the_project(self):
        self.start()
        self.hold["listen"] = asyncio.Event()
        pid = self.new_external()["id"]
        await self.until(lambda: self.waiting and ("listen", None) in self.log, "曲を聞く段が動き出す")
        await self.runner.cancel(self.mv_jobs("mv.listen")[0].id)                               # ジョブの一覧など、制作の外からの中止
        await self.until_state(pid, "paused")
        self.assertEqual(self.ledger.get_mv_project(pid)["message"], "曲を聞く処理が中止されたので一時停止しました")


class ViewTest(MvBase):
    async def test_progress_weights_scenes_by_duration(self):
        self.start()
        scenes = mv.make_scenes(quantize_scene_plan(plan_of([6.0, 12.0])))        # 1 : 2
        pid = "p1"
        self.ledger.create_mv_project(pid, "generating", {"analysis": {"duration": 18.0}, "title": "t", "width": 768, "height": 448}, scenes, "")
        proj = lambda: self.ledger.get_mv_project(pid)
        self.assertAlmostEqual(self.director._progress(proj(), None), mv.WEIGHT_ANALYZE)                     # 分析だけ済んだ
        self.ledger.update_mv_project(pid, scene_patches={1: {"prompt": "a"}, 2: {"prompt": "b"}})
        self.assertAlmostEqual(self.director._progress(proj(), None), mv.WEIGHT_ANALYZE + mv.WEIGHT_SCRIPT)
        self.ledger.update_mv_project(pid, scene_patches={1: {"status": "done"}})
        self.assertAlmostEqual(self.director._progress(proj(), None), 0.07 + 0.90 / 3, places=3)             # 短い方が 1/3
        job = types.SimpleNamespace(kind="mv.scene", status="running", progress=0.5, params={"scene": 2}, id="j", message="")
        self.assertAlmostEqual(self.director._progress(proj(), job), 0.07 + 0.90 / 3 + 0.90 * 2 / 3 * 0.5, places=3)     # 走っているシーンの途中
        self.ledger.update_mv_project(pid, scene_patches={2: {"status": "done"}})
        self.assertAlmostEqual(self.director._progress(proj(), None), 0.97, places=3)
        self.ledger.update_mv_project(pid, params_patch={"assembled": True}, state="completed")
        self.assertEqual(self.director._progress(proj(), None), 1.0)

    def test_sizes_low_quality_landscape_5_4_and_custom(self):
        low = mv.resolve_format({"aspect": "16:9", "quality": "low"})
        self.assertEqual((low["width"], low["height"]), (512, 288))
        l54 = mv.resolve_format({"aspect": "5:4", "quality": "fast"})
        self.assertEqual((l54["width"], l54["height"]), (640, 512))
        f = mv.resolve_format({"aspect": "custom", "quality": "fast", "width": 700, "height": 300.4})      # 32 の倍数に丸める
        self.assertEqual((f["aspect"], f["width"], f["height"]), ("custom", 704, 288))
        self.assertEqual(mv.resolve_format({"aspect": "custom", "width": 1344, "height": 256})["width"], 1344)

    async def test_view_has_everything_the_screen_needs(self):
        self.start()
        self.hold["scene2"] = asyncio.Event()
        pid = self.new_project(character="a girl", concept="dawn", aspect="9:16", quality="standard", seed=5)["id"]
        await self.until(lambda: ("scene", 2) in self.log and self.waiting, "シーン 2 が走る")
        v = self.director.view(pid)
        self.assertEqual(v["params"], {"song_id": self.song["id"], "image_id": self.image["id"], "character": "a girl", "concept": "dawn",
                                       "aspect": "9:16", "quality": "standard", "width": 576, "height": 1024, "min_s": 5.0, "max_s": 10.0,
                                       "seed": 5, "background_text": "auto", "lipsync": True, "style": "none"})
        self.assertEqual((v["state"], v["error"], v["title"], v["output"], v["assembled"]), ("generating", None, "夜明けの駅", None, False))
        self.assertEqual(v["analysis"], {"duration": 21.0, "bpm": 120.0, "has_vocals": True, "scenes": 3})
        self.assertEqual(v["script"], {"title": "T"})
        self.assertEqual((v["job_id"], v["job_status"]), (self.mv_jobs("mv.scene")[1].id, "running"))
        self.assertEqual(v["message"], "シーン 2: 生成中 2/4")                    # 走っているジョブの今の文言
        self.assertGreater(v["progress"], 0.07 + 0.9 * 8 / 21)
        s1, s2, s3 = v["scenes"]
        self.assertEqual(set(s1), {"index", "start", "end", "duration", "kind", "vocal_ratio", "prompt", "emotion", "seed", "status", "elapsed_s",
                                   "peak_vram_gb", "error", "clip_url", "thumb_url", "job_status"})
        self.assertEqual((s1["status"], s1["kind"], s1["prompt"], s1["seed"], s1["elapsed_s"], s1["peak_vram_gb"]), ("done", "vocal", "prompt 1", 1001, 1.5, 8.0))
        self.assertTrue(s1["clip_url"].startswith(f"/outputs/mv/{pid}/scene_001.mp4?v="))
        self.assertEqual((s1["thumb_url"], s1["job_status"]), (None, None))
        self.assertEqual((s2["status"], s2["clip_url"], s2["job_status"], s3["status"], s3["kind"]), ("running", None, "running", "pending", "vocal"))
        self.assertEqual((s2["start"], s2["end"], s2["duration"]), (8.0, 14.0, 6.0))
        self.hold["scene2"].set()
        await self.until_state(pid, "completed")

    async def test_message_gets_the_scene_prefix_while_waiting_for_the_lane(self):
        self.start()
        self.hold["other"] = asyncio.Event()
        self.runner.submit("video", "h3.t2va", {})                # 先に H3 のレーンを塞ぐ
        await self.until(lambda: ("other", None) in self.log, "他のタブの仕事が走る")
        pid = self.new_project()["id"]
        await self.until(lambda: any(j.kind == "mv.scene" for j in self.mv_jobs()), "シーン 1 のジョブが入る")
        v = self.director.view(pid)
        self.assertEqual(v["message"], "シーン 1 / 3: 順番を待っています")
        self.hold["other"].set()
        await self.until_state(pid, "completed")

    async def test_summary_thumb_prefers_the_final_mv_then_the_image(self):
        self.start()
        pid = self.new_project()["id"]
        (item,) = self.director.list()
        self.assertEqual(item["thumb_url"], "/outputs/" + self.image["thumb_path"])         # 途中は、キャラクター画像のサムネイル
        await self.until_state(pid, "completed")
        (item,) = self.director.list()
        self.assertEqual(item["thumb_url"], "/outputs/" + self.image["thumb_path"])         # 最終の MV にサムネイルが無ければ、画像のまま
        dest = self.root / "video" / "withthumb.mp4"
        dest.parent.mkdir(exist_ok=True)
        dest.write_bytes(b"mp4")
        with mock.patch.object(media, "video_thumb", side_effect=lambda src, dst, width: (dst.write_bytes(b"jpg"), True)[1]):
            rec = self.ledger.add_output("video", dest, "mv", None, "x.mp4", {}, width=768, height=448, duration_s=5.0)
        self.ledger.update_mv_project(pid, output_id=rec["id"])
        (item,) = self.director.list()
        self.assertEqual((item["thumb_url"], item["output"]["id"]), ("/outputs/" + rec["thumb_path"], rec["id"]))         # 最終の MV のサムネイルが先


class ResolveParamsTest(MvBase):
    def resolve(self, **kw):
        return mv.resolve_params(self.ledger, {"song_id": self.song["id"], "image_id": self.image["id"], **kw})

    def test_defaults_come_from_the_ledger_params_of_the_song_and_image(self):
        p = self.resolve()
        self.assertEqual(p["character"], "a girl with silver hair, anime style")        # 画像のプロンプト
        self.assertEqual(p["concept"], "夜明けの駅\nA warm ballad\nBallad")               # 曲の theme / caption / genre
        self.assertEqual((p["aspect"], p["quality"], p["width"], p["height"], p["min_s"], p["max_s"], p["seed"]), ("16:9", "fast", 768, 448, 5.0, 10.0, None))
        self.assertEqual((p["title"], p["background_text"]), ("夜明けの駅", "auto"))
        p = self.resolve(character="  my text  ", concept="mine", seed="42", min_s="6", max_s="9")
        self.assertEqual((p["character"], p["concept"], p["seed"], p["min_s"], p["max_s"]), ("my text", "mine", 42, 6.0, 9.0))     # 画面の入力が優先

    def test_sources_say_where_each_description_came_from(self):
        p = self.resolve()
        self.assertEqual((p["character_source"], p["concept_source"], p["listen"]), ("image", "song", ""))             # 台帳の画像のプロンプト・曲のテーマなどから
        p = self.resolve(character="mine", concept="mine too")
        self.assertEqual((p["character_source"], p["concept_source"], p["listen"]), ("typed", "typed", ""))             # 書いたもの
        song, image = self.add("audio", "plain.mp3", {"uploaded": True}), self.add("image", "plain.png", {"uploaded": True})
        plain = mv.resolve_params(self.ledger, {"song_id": song["id"], "image_id": image["id"]})
        self.assertEqual((plain["character_source"], plain["concept_source"], plain["listen"]), ("", "", "pending"))   # どちらも空: 制作の中で自動で入れる
        typed = mv.resolve_params(self.ledger, {"song_id": song["id"], "image_id": image["id"], "concept": "x"})
        self.assertEqual((typed["concept_source"], typed["listen"]), ("typed", ""))                                      # 曲の内容を書けば、聞かない
        self.assertFalse(mv.song_has_info({"uploaded": True}) or mv.song_has_info({}) or mv.song_has_info(None))
        self.assertTrue(mv.song_has_info({"theme": "夜明け"}) and mv.song_has_info({"genre": "Ballad"}))

    def test_neutral_when_nothing_is_known(self):
        song = self.add("audio", "plain.mp3", {})
        image = self.add("image", "plain.png", {"uploaded": True})
        p = mv.resolve_params(self.ledger, {"song_id": song["id"], "image_id": image["id"]})
        self.assertEqual((p["character"], p["concept"], p["title"]), ("", "", "plain"))         # 空のまま(演出の作成が中立の文を使う)

    def test_long_texts_are_clipped_at_a_sentence_end(self):
        long_prompt = "A girl with silver hair. " * 40
        image = self.add("image", "long.png", {"prompt": long_prompt})
        p = mv.resolve_params(self.ledger, {"song_id": self.song["id"], "image_id": image["id"]})
        self.assertLessEqual(len(p["character"]), mv.CHARACTER_MAX)
        self.assertTrue(p["character"].endswith("hair."))
        self.assertLessEqual(len(self.resolve(concept="あ" * 5000)["concept"]), mv.CONCEPT_MAX)

    def test_sizes_for_every_aspect_and_quality(self):
        expected = {"16:9": [(768, 448), (1024, 576), (1344, 768)], "9:16": [(448, 768), (576, 1024), (768, 1344)],
                    "1:1": [(512, 512), (768, 768), (960, 960)], "4:5": [(512, 640), (640, 800), (768, 960)]}
        for aspect, sizes in expected.items():
            for quality, size in zip(("fast", "standard", "high"), sizes):
                p = self.resolve(aspect=aspect, quality=quality)
                self.assertEqual((p["width"], p["height"]), size, (aspect, quality))
                self.assertEqual((p["width"] % 32, p["height"] % 32), (0, 0))

    def test_validation_messages(self):
        gone = self.add("audio", "gone.mp3", {})
        (self.root / "uploads" / "gone.mp3").unlink()
        cases = [
            ({"song_id": ""}, "曲を選んでください"), ({"song_id": "nope"}, "曲が見つかりません"), ({"song_id": self.image["id"]}, "曲には音声を使ってください"),
            ({"song_id": gone["id"]}, "曲のファイルが見つかりません"),
            ({"image_id": ""}, "キャラクター画像を選んでください"), ({"image_id": "nope"}, "キャラクター画像が見つかりません"),
            ({"image_id": self.song["id"]}, "キャラクター画像には画像を使ってください"),
            ({"aspect": "3:2"}, "画角は 16:9 / 9:16 / 1:1 / 4:5 / 5:4 / custom のいずれかです"), ({"quality": "ultra"}, "画質は low / fast / standard / high のいずれかです"),
            ({"aspect": "custom"}, "幅を数字で"), ({"aspect": "custom", "width": 100, "height": 448}, "幅は 256〜1344"),
            ({"min_s": 4}, "シーンの長さは 5〜10 秒"), ({"max_s": 11}, "シーンの長さは 5〜10 秒"), ({"min_s": "abc"}, "シーンの最短 は数字"),
            ({"min_s": 9, "max_s": 6}, "シーンの最短は最長より長くできません"), ({"seed": "abc"}, "seed は整数"),
            ({"background_text": "huge"}, "background_text は auto / large / none"), ({"lipsync": "yes"}, "lipsync は true / false"),
        ]
        for extra, word in cases:
            with self.assertRaises(ValueError, msg=str(extra)) as cm:
                self.resolve(**extra)
            self.assertIn(word, str(cm.exception), extra)


class NextStepTest(unittest.TestCase):
    def proj(self, analysis=True, prompts=True, statuses=("pending",), assembled=False):
        scenes = [{"index": i, "status": st, "prompt": "p" if prompts else ""} for i, st in enumerate(statuses, start=1)]
        return {"params": {"analysis": {"duration": 1} if analysis else None, "assembled": assembled}, "scenes": scenes}

    def test_next_step_is_derived_from_the_data(self):
        self.assertEqual(mv.next_step(self.proj(analysis=False)), ("analyze",))
        self.assertEqual(mv.next_step({"params": {"analysis": {"d": 1}}, "scenes": []}), ("analyze",))
        self.assertEqual(mv.next_step(self.proj(prompts=False)), ("script",))
        self.assertEqual(mv.next_step(self.proj(statuses=("done", "failed", "pending"))), ("scene", 2))
        self.assertEqual(mv.next_step(self.proj(statuses=("done", "done", "running"))), ("scene", 3))
        self.assertEqual(mv.next_step(self.proj(statuses=("done", "done"))), ("assemble",))
        self.assertEqual(mv.next_step(self.proj(statuses=("done", "done"), assembled=True)), ("done",))
        self.assertEqual(mv.next_step(self.proj(statuses=("done", "pending"), assembled=True)), ("scene", 2))      # 結合済みでも、作り直すシーンがあれば先

    def test_listen_comes_after_the_analysis_and_before_the_script(self):
        def proj(listen, **kw):
            p = self.proj(**kw)
            p["params"]["listen"] = listen
            return p
        self.assertEqual(mv.next_step(proj("pending", analysis=False)), ("analyze",))            # まず分析
        self.assertEqual(mv.next_step(proj("pending", prompts=False)), ("listen",))
        self.assertEqual(mv.next_step(proj("pending")), ("listen",))                              # 演出が済んでいても、予定が残っていれば聞く
        for listen in ("done", "skipped", ""):
            self.assertEqual(mv.next_step(proj(listen, prompts=False)), ("script",), listen)
        self.assertEqual(mv.next_step(self.proj(prompts=False)), ("script",))                      # この版の前の制作(listen の項目が無い)は、聞かない
        self.assertEqual(mv.step_of(types.SimpleNamespace(kind="mv.listen", params={})), ("listen",))


if __name__ == "__main__":
    unittest.main()
