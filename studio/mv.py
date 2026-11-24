"""リップシンク MV の管理役(Director): 制作(プロジェクト)を持ち、ジョブを 1 つずつつないで 1 本の MV にする。

流れ(それぞれが 1 つのジョブ): mv.analyze(曲の分析、CPU)→ mv.script(演出を LLM が書く)→ mv.scene × シーン数(H3)→ mv.assemble(結合、CPU)。
シーンのジョブは、前のシーンが終わってから次を入れる。その間に他のタブの仕事が割り込める(順番は受け付けた順)。

制作の状態(state)とシーンは台帳の mv_projects 表に残る。次にやることは「データから導く」(next_step): 分析が無ければ分析、演出の無いシーンが
あれば演出、できていない最初のシーン、結合がまだなら結合。だから、一時停止・再開・失敗・再起動・シーンのやり直しのあとも、同じ規則で続きから進む。

  create             → analyzing  (mv.analyze を入れる)
  ジョブが完了        → 次の段階のジョブを入れる(analyzing → scripting → generating(シーン 1 … N)→ assembling → completed)
  ジョブが失敗        → failed(失敗したシーンは failed。再開・やり直しで続きから)
  pause              → paused(走っているジョブは終わるまで続け、次は入れない。まだ始まっていない(順番待ち・準備中の)ジョブは取り下げる)
  resume             → 走っているジョブがあればその段階に戻し、無ければ次の段階を入れる(paused / failed / cancelled から)
  cancel             → cancelled(走っているジョブを中止)
  retry_scene        → そのシーンを pending に戻して続ける(全シーンができたら assembling → completed)
  reassemble         → assembling
  管理役の再起動      → 走っていた制作は paused(restore)

状態の変更はイベントループのスレッドで行う(API のルートは async def、ジョブの終わりの通知もループの上で呼ばれる)。
"""
import asyncio
import logging
import random
import re
import shutil
import uuid
from pathlib import Path
from typing import Optional

from .jobs import TERMINAL

log = logging.getLogger("studio.mv")

# 投稿先 → 画質(高速 / 標準 / 高画質)→ (幅, 高さ)。docs/SPEC.md の表。どれも 32 の倍数(H3 が 32 に丸めるので、そのまま通る)
SIZES = {
    "16:9": {"fast": (768, 448), "standard": (1024, 576), "high": (1344, 768)},
    "9:16": {"fast": (448, 768), "standard": (576, 1024), "high": (768, 1344)},
    "1:1": {"fast": (512, 512), "standard": (768, 768), "high": (960, 960)},
    "4:5": {"fast": (512, 640), "standard": (640, 800), "high": (768, 960)},
}
ASPECTS, QUALITIES = tuple(SIZES), ("fast", "standard", "high")
SCENE_MIN_S, SCENE_MAX_S = 5.0, 10.0           # シーンの長さの範囲(H3 の最短が 5 秒、MV の 1 カットとして 10 秒まで)
CHARACTER_MAX, CONCEPT_MAX, PROMPT_MAX = 600, 1000, 8000
SEED_MOD = 2_147_483_647

RUNNING = ("analyzing", "scripting", "generating", "assembling")
STATES = (*RUNNING, "paused", "completed", "failed", "cancelled")
FINISHED = ("completed", "cancelled")           # 終わった制作。これ以外(一時停止・失敗も)は続きがあるので、曲と画像を使い続ける
STAGE_STATE = {"mv.analyze": "analyzing", "mv.script": "scripting", "mv.scene": "generating", "mv.assemble": "assembling"}
STAGE_LABEL = {"mv.analyze": "曲の分析", "mv.script": "演出の作成", "mv.scene": "シーンの生成", "mv.assemble": "結合"}
RESTART_MESSAGE = "管理役の再起動で一時停止しました"
# 全体の進みの重み(シーンの生成が、ほぼ全部)。シーンは尺に比例して分ける
WEIGHT_ANALYZE, WEIGHT_SCRIPT, WEIGHT_SCENES, WEIGHT_ASSEMBLE = 0.04, 0.03, 0.90, 0.03
INPUT_KEYS = ("song_id", "image_id", "character", "concept", "aspect", "quality", "width", "height", "min_s", "max_s", "seed",
              "background_text")


# ---- 作業フォルダ・ファイル名 ----

def work_dir(outputs_dir, pid: str) -> Path:
    return Path(outputs_dir) / "mv" / pid


def scene_clip_name(index: int) -> str:
    return f"scene_{index:03d}.mp4"


def scene_thumb_name(index: int) -> str:
    return f"scene_{index:03d}.jpg"


def scene_vocals_name(index: int) -> str:
    return f"scene_{index:03d}_vocals.wav"


# ---- 入力の検証と解決 ----

def _clip(text: str, limit: int) -> str:
    """limit 文字までに収める。後半に文の切れ目があれば、そこで切る。"""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    ends = [m.end() for m in re.finditer(r"[。．.!?！？\n]", cut)]
    return (cut[:ends[-1]] if ends and ends[-1] > limit * 0.6 else cut).strip()


def _number(value, name: str, default: float) -> float:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} は数字で指定してください")


def concept_from_song(params: dict) -> str:
    """曲の台帳の params(音楽タブの曲なら theme / caption / genre がある)から、曲の内容・世界観の文を作る。無ければ空。
    画面が曲を落としたときに入れる文と同じ組み立て(web/mv.js の conceptFrom)。"""
    parts = [str((params or {}).get(k) or "").strip() for k in ("theme", "caption", "genre")]
    return _clip("\n".join(p for p in parts if p), CONCEPT_MAX)


def _output(ledger, oid, kind: str, label: str, what: str) -> dict:
    if not str(oid or "").strip():
        raise ValueError(f"{label}を選んでください")
    rec = ledger.get_output(str(oid).strip())
    if rec is None:
        raise ValueError(f"{label}が見つかりません(ギャラリーから削除されたかもしれません)")
    if rec["kind"] != kind:
        raise ValueError(f"{label}には{what}を使ってください")
    if not ledger.abs_path(rec["path"]).is_file():
        raise ValueError(f"{label}のファイルが見つかりません(削除されたかもしれません)")
    return rec


def resolve_params(ledger, params: dict) -> dict:
    """画面・API から来た入力を検証し、曲・画像の生成物の中身で既定を埋めて、制作の params にする。不正なら ValueError(日本語)。"""
    song = _output(ledger, params.get("song_id"), "audio", "曲", "音声")
    image = _output(ledger, params.get("image_id"), "image", "キャラクター画像", "画像")
    aspect = str(params.get("aspect") or "16:9")
    if aspect not in SIZES:
        raise ValueError("投稿先は " + " / ".join(ASPECTS) + " のいずれかです")
    quality = str(params.get("quality") or "fast")
    if quality not in QUALITIES:
        raise ValueError("画質は " + " / ".join(QUALITIES) + " のいずれかです")
    min_s, max_s = _number(params.get("min_s"), "シーンの最短", SCENE_MIN_S), _number(params.get("max_s"), "シーンの最長", SCENE_MAX_S)
    if not (SCENE_MIN_S <= min_s <= SCENE_MAX_S and SCENE_MIN_S <= max_s <= SCENE_MAX_S):
        raise ValueError(f"シーンの長さは {SCENE_MIN_S:g}〜{SCENE_MAX_S:g} 秒で指定してください")
    if min_s > max_s:
        raise ValueError("シーンの最短は最長より長くできません")
    seed = params.get("seed")
    if seed in (None, ""):
        seed = None
    else:
        try:
            seed = int(seed)
        except (TypeError, ValueError):
            raise ValueError("seed は整数で指定してください")
    background_text = str(params.get("background_text") or "auto")
    if background_text not in ("auto", "large", "none"):
        raise ValueError("background_text は auto / large / none のいずれかです")
    character = _clip(str(params.get("character") or ""), CHARACTER_MAX) or _clip(str(image["params"].get("prompt") or ""), CHARACTER_MAX)
    concept = _clip(str(params.get("concept") or ""), CONCEPT_MAX) or concept_from_song(song["params"])
    width, height = SIZES[aspect][quality]
    title = _clip(str(song["params"].get("theme") or Path(song.get("filename") or "").stem or "MV"), 60) or "MV"
    return {"song_id": song["id"], "image_id": image["id"], "character": character, "concept": concept, "aspect": aspect,
            "quality": quality, "width": width, "height": height, "min_s": min_s, "max_s": max_s, "seed": seed,
            "background_text": background_text, "title": title}


# ---- シーン・次にやること ----

def make_scenes(plan_scenes: list) -> list:
    """分析(量子化済み)のシーンを、制作が持つシーンの形にする。演出(prompt・seed)はまだ空。"""
    return [{"index": int(s["index"]), "start": s["start"], "end": s["end"], "duration": s["duration"],
             "start_frame": s.get("start_frame"), "end_frame": s.get("end_frame"), "frame_count": s.get("frame_count"),
             "kind": s.get("kind") or "instrumental", "vocal_ratio": s.get("vocal_ratio") or 0.0,
             "boundary_kind": s.get("boundary_kind"), "prompt": "", "emotion": "", "seed": None, "status": "pending",
             "clip": None, "thumb": None, "elapsed_s": None, "peak_vram_gb": None, "error": None} for s in plan_scenes]


def next_step(proj: dict) -> tuple:
    """制作のデータから、次にやること: ("analyze",) / ("script",) / ("scene", 番号) / ("assemble",) / ("done",)。"""
    params, scenes = proj["params"], proj["scenes"]
    if not params.get("analysis") or not scenes:
        return ("analyze",)
    if any(not s.get("prompt") for s in scenes):
        return ("script",)
    for s in sorted(scenes, key=lambda s: s["index"]):
        if s["status"] != "done":
            return ("scene", s["index"])
    if not params.get("assembled"):
        return ("assemble",)
    return ("done",)


def step_of(job) -> tuple:
    """ジョブが受け持っていた段階(next_step と同じ形)。"""
    if job.kind == "mv.scene":
        return ("scene", job.params.get("scene"))
    return (job.kind.split(".", 1)[1],)


class Director:
    """制作ごとに、ジョブを 1 つずつ入れて進める。runner は JobRunner、ledger と outputs_dir は管理役のもの。"""

    def __init__(self, runner, ledger, outputs_dir):
        self.runner, self.ledger, self.outputs_dir = runner, ledger, Path(outputs_dir)
        self._active: dict = {}        # 制作の ID -> 入れてあるジョブの ID(制作ごとに高々 1 つ。ジョブが終わった通知で外す)
        runner.on_finished(self._on_finished)

    # ---- 入口 ----

    def create(self, params: dict) -> dict:
        """入力を検証して制作を作り、最初のジョブ(分析)を入れる。不正なら ValueError(日本語)。制作の画面用の辞書を返す。"""
        resolved = resolve_params(self.ledger, params)
        pid = uuid.uuid4().hex[:12]
        work_dir(self.outputs_dir, pid).mkdir(parents=True, exist_ok=True)
        self.ledger.create_mv_project(pid, "analyzing", resolved, [], "曲を分析します")
        self._advance(pid)
        return self.view(pid)

    def restore(self) -> int:
        """管理役の起動時: 走っていた制作(ジョブは再起動で消えた)を一時停止にし、「生成中」のシーンを未生成に戻す。戻す数を返す。"""
        n = 0
        for proj in self.ledger.list_mv_projects(limit=100000):
            if proj["state"] not in RUNNING:
                continue
            patches = {s["index"]: {"status": "pending"} for s in proj["scenes"] if s["status"] == "running"}
            self.ledger.update_mv_project(proj["id"], state="paused", message=RESTART_MESSAGE, scene_patches=patches)
            n += 1
        return n

    async def pause(self, pid: str) -> dict:
        """走っているジョブは終わるまで続け、次は入れない。まだ始まっていないジョブ(順番待ち・準備中)は、順番が来てから勝手に始まらないよう取り下げる。"""
        proj = self._get(pid)
        if proj["state"] == "paused":
            return self._view(proj)
        if proj["state"] not in RUNNING:
            raise ValueError("一時停止できるのは、制作の途中だけです")
        job = self._inflight(pid)
        if job is None:
            self._set(pid, "paused", "一時停止しました")
        elif job.status == "running":
            self._set(pid, "paused", "一時停止します(いまの仕事が終わると止まります)")
        else:
            self._set(pid, "paused", "一時停止します(始まっていない仕事を取り下げています)")
            await self.runner.cancel(job.id)
        return self.view(pid)

    def resume(self, pid: str) -> dict:
        proj = self._get(pid)
        if proj["state"] not in ("paused", "failed", "cancelled"):
            raise ValueError("再開できるのは、一時停止・失敗・中止した制作だけです")
        return self._start(pid)

    async def cancel(self, pid: str) -> dict:
        proj = self._get(pid)
        if proj["state"] == "cancelled":
            return self._view(proj)
        if proj["state"] in ("completed", "failed"):
            raise ValueError("終わった制作は中止できません(削除はできます)")
        self._set(pid, "cancelled", "中止しました")
        job = self._inflight(pid)
        if job is not None:
            await self.runner.cancel(job.id)
        return self.view(pid)

    def retry_scene(self, pid: str, index: int, prompt: str = None, seed: int = None) -> dict:
        """シーン 1 つを作り直す。prompt を渡せば演出を書き換え、seed を渡せばその seed にする。
        どちらも渡さなければ、別の絵になるよう seed を取り直す(prompt だけ渡したときは、seed はそのまま)。
        全シーンができたら、自動で結合し直す。"""
        proj = self._get(pid)
        scenes = proj["scenes"]
        if not scenes or any(not s.get("prompt") for s in scenes):
            raise ValueError("まだ演出ができていないので、シーンをやり直せません")
        number = _int(index, "シーンの番号")
        scene = next((s for s in scenes if s["index"] == number), None)
        if scene is None:
            raise ValueError(f"シーン {number} はありません")
        job = self._inflight(pid)
        if job is not None and job.kind == "mv.assemble":
            raise ValueError("結合しています。終わってからやり直してください")
        if job is not None and job.kind == "mv.scene" and job.params.get("scene") == scene["index"]:
            raise ValueError("そのシーンは生成中です")
        patch = {"status": "pending", "error": None, "clip": None, "thumb": None, "elapsed_s": None, "peak_vram_gb": None}
        if prompt is not None:
            prompt = str(prompt).strip()
            if not 1 <= len(prompt) <= PROMPT_MAX:
                raise ValueError(f"プロンプトは 1〜{PROMPT_MAX} 文字で入力してください")
            patch["prompt"] = prompt
        if seed not in (None, ""):
            patch["seed"] = _int(seed, "seed")
        elif prompt is None or scene.get("seed") is None:
            patch["seed"] = random.randrange(SEED_MOD)
        self.ledger.update_mv_project(pid, params_patch={"assembled": False}, scene_patches={scene["index"]: patch})
        return self._start(pid)

    def reassemble(self, pid: str) -> dict:
        proj = self._get(pid)
        if not proj["scenes"] or any(s["status"] != "done" for s in proj["scenes"]):
            raise ValueError("全部のシーンができてから結合できます")
        if self._inflight(pid) is not None:
            raise ValueError("制作中です。終わってから結合し直してください")
        self.ledger.update_mv_project(pid, params_patch={"assembled": False})
        return self._start(pid)

    async def delete(self, pid: str) -> None:
        """制作を消す(作業フォルダごと)。ギャラリーの最終の MV は残す。走っているジョブは中止し、その終わりの通知で、後から書かれたファイルも消す。"""
        self._get(pid)
        job = self._inflight(pid)
        self.ledger.delete_mv_project(pid)
        if job is not None:
            await self.runner.cancel(job.id)
        else:
            self._active.pop(pid, None)
        await asyncio.to_thread(shutil.rmtree, work_dir(self.outputs_dir, pid), True)

    def output_ids(self, pid: str) -> list:
        """この制作の MV(動画)の生成物の ID(新しい順)。結合し直すたびに残る前の版も含む(生成物の params.project_id がこの制作のもの)。制作が無ければ LookupError。"""
        self._get(pid)
        return [o["id"] for o in self.ledger.list_outputs(limit=100000) if o["params"].get("project_id") == pid]

    # ---- 見せる ----

    def view(self, pid: str) -> Optional[dict]:
        proj = self.ledger.get_mv_project(pid)
        return self._view(proj) if proj else None

    def list(self, limit: int = 100) -> list:
        """新しい順の一覧(シーンの中身は含めない)。"""
        return [self._summary(p) for p in self.ledger.list_mv_projects(limit)]

    def _get(self, pid: str) -> dict:
        proj = self.ledger.get_mv_project(pid)
        if proj is None:
            raise LookupError("その MV の制作は見つかりません(削除されたかもしれません)")
        return proj

    def _view(self, proj: dict) -> dict:
        pid, params = proj["id"], proj["params"]
        job = self._inflight(pid)
        out = self.ledger.get_output(proj["output_id"]) if proj["output_id"] else None
        return {"id": pid, "title": params.get("title") or "MV", "state": proj["state"], "message": self._message(proj, job),
                "error": proj["error"], "created_at": proj["created_at"], "updated_at": proj["updated_at"],
                "params": {k: params.get(k) for k in INPUT_KEYS}, "analysis": params.get("analysis"), "script": params.get("script"),
                "assembled": bool(params.get("assembled")), "scenes": [self._scene_view(pid, s, job) for s in proj["scenes"]],
                "job_id": job.id if job else None, "job_status": job.status if job else None,
                "progress": self._progress(proj, job), "output": out}

    def _summary(self, proj: dict) -> dict:
        params, job = proj["params"], self._inflight(proj["id"])
        out = self.ledger.get_output(proj["output_id"]) if proj["output_id"] else None
        thumb = (out or {}).get("thumb_path") or (self.ledger.get_output(params.get("image_id") or "") or {}).get("thumb_path")
        return {"id": proj["id"], "title": params.get("title") or "MV", "state": proj["state"], "message": self._message(proj, job),
                "error": proj["error"], "created_at": proj["created_at"], "updated_at": proj["updated_at"],
                "aspect": params.get("aspect"), "quality": params.get("quality"), "width": params.get("width"),
                "height": params.get("height"), "scenes": len(proj["scenes"]),
                "done": sum(1 for s in proj["scenes"] if s["status"] == "done"), "progress": self._progress(proj, job),
                "job_id": job.id if job else None, "output": out, "thumb_url": "/outputs/" + thumb if thumb else None}

    def _scene_view(self, pid: str, s: dict, job) -> dict:
        d = work_dir(self.outputs_dir, pid)

        def url(name):
            f = d / name if name else None
            return f"/outputs/mv/{pid}/{name}?v={int(f.stat().st_mtime)}" if f is not None and f.is_file() else None
        running = job is not None and job.kind == "mv.scene" and job.params.get("scene") == s["index"]
        return {**{k: s.get(k) for k in ("index", "start", "end", "duration", "kind", "vocal_ratio", "prompt", "emotion", "seed",
                                         "status", "elapsed_s", "peak_vram_gb", "error")},
                "clip_url": url(s.get("clip")) if s["status"] == "done" else None, "thumb_url": url(s.get("thumb")),
                "job_status": job.status if running else None}

    def _message(self, proj: dict, job) -> str:
        """画面に出す一文。走っているジョブがあれば、そのジョブの今(順番待ち・準備中・「生成中 2/4」など)。"""
        if job is None or proj["state"] not in RUNNING:
            return proj["message"] or ""
        msg = job.message or proj["message"] or ""
        if job.kind == "mv.scene" and not msg.startswith("シーン"):
            msg = f"シーン {job.params.get('scene')} / {len(proj['scenes'])}: {msg}"
        return msg

    def _progress(self, proj: dict, job) -> float:
        """0〜1。段階の重みで足し、シーンは尺に比例して分ける。走っているジョブの途中の進みも入れる。"""
        if proj["state"] == "completed":
            return 1.0
        params, scenes = proj["params"], proj["scenes"]
        live = max(0.0, min(1.0, job.progress if job is not None and job.status == "running" else 0.0))
        kind = job.kind if job is not None else ""
        total = WEIGHT_ANALYZE if params.get("analysis") else (WEIGHT_ANALYZE * live if kind == "mv.analyze" else 0.0)
        total += WEIGHT_SCRIPT if scenes and all(s.get("prompt") for s in scenes) else (WEIGHT_SCRIPT * live if kind == "mv.script" else 0.0)
        length = sum(s["duration"] for s in scenes) or 1.0
        for s in scenes:
            w = WEIGHT_SCENES * s["duration"] / length
            if s["status"] == "done":
                total += w
            elif kind == "mv.scene" and job.params.get("scene") == s["index"]:
                total += w * live
        total += WEIGHT_ASSEMBLE if params.get("assembled") else (WEIGHT_ASSEMBLE * live if kind == "mv.assemble" else 0.0)
        return round(min(total, 0.999), 4)

    # ---- ジョブをつなぐ ----

    def _inflight(self, pid: str, live: bool = False):
        """入れてあって、まだ終わっていないジョブ(無ければ None)。live なら、中止を頼んだジョブ(終わりかけ)は数えない。"""
        jid = self._active.get(pid)
        job = self.runner.get(jid) if jid else None
        if job is None or job.status in TERMINAL or (live and job.cancel_requested):
            return None
        return job

    def _set(self, pid: str, state: str, message: str, error: str = None, **fields) -> None:
        self.ledger.update_mv_project(pid, state=state, message=message, error=error, **fields)

    def _submit(self, pid: str, kind: str, **params):
        job = self.runner.submit("mv", kind, {"project_id": pid, **params})
        self._active[pid] = job.id
        return job

    def _start(self, pid: str) -> dict:
        """走っているジョブがあればその段階として扱い(終わったら続きを入れる)、無ければ次の段階のジョブを入れる。"""
        job = self._inflight(pid, live=True)
        if job is not None:
            self._set(pid, STAGE_STATE[job.kind], "再開しました")
        else:
            self._advance(pid)
        return self.view(pid)

    def _advance(self, pid: str) -> None:
        """次の段階のジョブを入れる(全部できていれば完了にする)。"""
        proj = self.ledger.get_mv_project(pid)
        if proj is None:
            return
        step, n = next_step(proj), len(proj["scenes"])
        try:
            if step[0] == "analyze":
                self._set(pid, "analyzing", "曲を分析します")
                self._submit(pid, "mv.analyze")
            elif step[0] == "script":
                self._set(pid, "scripting", "演出を書きます")
                self._submit(pid, "mv.script")
            elif step[0] == "scene":
                self._set(pid, "generating", f"シーン {step[1]} / {n} を生成します", scene_patches={step[1]: {"status": "running", "error": None}})
                self._submit(pid, "mv.scene", scene=step[1])
            elif step[0] == "assemble":
                self._set(pid, "assembling", "シーンを結合します")
                self._submit(pid, "mv.assemble")
            else:
                self._set(pid, "completed", "MV ができました")
        except ValueError as e:       # ジョブを受け付けられない(制作のデータが壊れている、など)
            log.warning("制作 %s のジョブを入れられません: %s", pid, e)
            self._set(pid, "failed", "ジョブを始められませんでした", error=str(e))

    def _on_finished(self, job) -> None:
        """JobRunner から、ジョブが終わるたびに呼ばれる。この制作のジョブなら、結果に応じて次へ進む・止める。"""
        pid = job.params.get("project_id") if job.kind in STAGE_STATE else None
        if not pid or self._active.get(pid) != job.id:
            return
        del self._active[pid]
        proj = self.ledger.get_mv_project(pid)
        if proj is None:       # 走っている間に消された制作: ジョブが後から書いたファイルを片づける
            shutil.rmtree(work_dir(self.outputs_dir, pid), ignore_errors=True)
            return
        state, scene = proj["state"], job.params.get("scene")
        if job.status == "completed":
            if state in RUNNING:
                if next_step(proj) == step_of(job):       # 完了したのに結果が記録されていない(同じことを繰り返さない)
                    self._set(pid, "failed", f"{STAGE_LABEL[job.kind]}の結果を記録できませんでした", error="ジョブは完了しましたが、結果が制作に書かれていません")
                else:
                    self._advance(pid)
            elif state == "paused":
                self._set(pid, "paused", "一時停止しました")
            return
        if job.kind == "mv.scene":       # 失敗・中止したシーンは、完成していない(中止なら、また最初から)
            patch = {"status": "failed", "error": job.error} if job.status == "failed" and state != "cancelled" else {"status": "pending"}
            self.ledger.update_mv_project(pid, scene_patches={scene: patch})
        if state == "cancelled":
            return
        if job.status == "failed":
            self._set(pid, "failed", f"{STAGE_LABEL[job.kind]}に失敗しました", error=job.error or "原因不明")
        elif state == "paused":       # 一時停止で取り下げた(まだ始まっていなかった)ジョブ
            self._set(pid, "paused", "一時停止しました")
        elif state in RUNNING:       # 管理役の画面の外(ジョブの一覧など)から、このジョブだけ中止された
            self._set(pid, "paused", f"{STAGE_LABEL[job.kind]}が中止されたので一時停止しました")


def _int(value, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} は整数で指定してください")
