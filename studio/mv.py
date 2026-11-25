"""リップシンク MV の管理役(Director): 制作(プロジェクト)を持ち、ジョブを 1 つずつつないで 1 本の MV にする。

流れ(それぞれが 1 つのジョブ): mv.analyze(曲の分析、CPU)→ [mv.listen(曲を LLM が聞いて、曲の内容を書く、CPU)]→ mv.script(演出を LLM が書く)→
mv.scene × シーン数(H3)→ mv.assemble(結合、CPU)。[ ] は、曲の台帳に情報が無く、「曲の内容」も空のときだけ(listen = "pending")。聞けなくても MV は作る。
シーンのジョブは、前のシーンが終わってから次を入れる。その間に他のタブの仕事が割り込める(順番は受け付けた順)。

制作の状態(state)とシーンは台帳の mv_projects 表に残る。次にやることは「データから導く」(next_step): 分析が無ければ分析、曲を聞く予定が残っていれば
聞く、演出の無いシーンがあれば演出、できていない最初のシーン、結合がまだなら結合。だから、一時停止・再開・失敗・再起動・シーンのやり直しのあとも、同じ規則で続きから進む。

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
import math
import random
import re
import shutil
import uuid
from pathlib import Path
from typing import Optional

from .jobs import TERMINAL

log = logging.getLogger("studio.mv")

# 画角 → 画質(低 VRAM / 高速 / 標準 / 高画質)→ (幅, 高さ)。docs/SPEC.md の表。どれも 32 の倍数(H3 が 32 に丸めるので、そのまま通る)
SIZES = {
    "16:9": {"low": (512, 288), "fast": (768, 448), "standard": (1024, 576), "high": (1344, 768)},
    "9:16": {"low": (288, 512), "fast": (448, 768), "standard": (576, 1024), "high": (768, 1344)},
    "1:1": {"low": (384, 384), "fast": (512, 512), "standard": (768, 768), "high": (960, 960)},
    "4:5": {"low": (416, 512), "fast": (512, 640), "standard": (640, 800), "high": (768, 960)},
    "5:4": {"low": (512, 416), "fast": (640, 512), "standard": (800, 640), "high": (960, 768)},
}
CUSTOM = "custom"                              # 画角「手動入力」: 幅と高さを直接指定(32 の倍数に丸める)
CUSTOM_MIN, CUSTOM_MAX, CUSTOM_STEP = 256, 1344, 32
ASPECTS, QUALITIES = (*SIZES, CUSTOM), ("low", "fast", "standard", "high")
SCENE_MIN_S, SCENE_MAX_S = 5.0, 10.0           # シーンの長さの範囲(H3 の最短が 5 秒、MV の 1 カットとして 10 秒まで)
CHARACTER_MAX, CONCEPT_MAX, PROMPT_MAX = 600, 1000, 8000
SEED_MOD = 2_147_483_647

RUNNING = ("analyzing", "scripting", "generating", "assembling")
STATES = (*RUNNING, "paused", "completed", "failed", "cancelled")
# 段階の名前(ジョブの種類は "<接頭辞>.<段階>": MV は mv.analyze…、ショート動画は short.analyze…)→ 制作の状態・画面に出す名前。
# plan(物語からシーンに割る)はショート動画だけ
STAGE_STATE = {"analyze": "analyzing", "listen": "analyzing", "plan": "scripting", "script": "scripting", "scene": "generating", "assemble": "assembling"}
STAGE_LABEL = {"analyze": "曲の分析", "listen": "曲を聞く処理", "plan": "シーン割り", "script": "演出の作成", "scene": "シーンの生成", "assemble": "結合"}
RESTART_MESSAGE = "管理役の再起動で一時停止しました"
# 全体の進みの重み(シーンの生成が、ほぼ全部)。シーンは尺に比例して分ける
WEIGHT_ANALYZE, WEIGHT_SCRIPT, WEIGHT_SCENES, WEIGHT_ASSEMBLE = 0.04, 0.03, 0.90, 0.03
INPUT_KEYS = ("song_id", "image_id", "character", "concept", "aspect", "quality", "width", "height", "min_s", "max_s", "seed",
              "background_text", "lipsync")


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
    """数(空なら default)。NaN・無限大は数として扱わない(JSON の NaN / Infinity も float として届き、範囲の比べをすり抜けるため)。"""
    if value in (None, ""):
        return default
    try:
        x = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} は数字で指定してください") from None
    if not math.isfinite(x):
        raise ValueError(f"{name} は数字で指定してください")
    return x


def concept_from_song(params: dict) -> str:
    """曲の台帳の params(音楽タブの曲なら theme / caption / genre がある)から、曲の内容・世界観の文を作る。無ければ空。
    画面が曲を落としたときに入れる文と同じ組み立て(web/mv.js の conceptFrom)。"""
    parts = [str((params or {}).get(k) or "").strip() for k in ("theme", "caption", "genre")]
    return _clip("\n".join(p for p in parts if p), CONCEPT_MAX)


def song_has_info(params: dict) -> bool:
    """曲の台帳の params に、曲の内容の手がかり(テーマ・キャプション・ジャンル)があるか。音楽タブの曲は有り、外から持ち込んだ曲(アップロード)は無い。"""
    return bool(concept_from_song(params))


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


def pick_song(ledger, oid) -> dict:
    """曲(音声の生成物)を台帳から取る。無い・音声でない・ファイルが無いときは ValueError(日本語)。"""
    return _output(ledger, oid, "audio", "曲", "音声")


def pick_image(ledger, oid) -> dict:
    return _output(ledger, oid, "image", "キャラクター画像", "画像")


def _custom_side(value, label: str) -> int:
    """手動入力の幅・高さ: 整数、256〜1344、32 の倍数に丸める。"""
    try:
        n = int(float(value))
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"画角が手動入力のときは、{label}を数字で指定してください(256〜{CUSTOM_MAX}、32 の倍数)") from None
    if not CUSTOM_MIN <= n <= CUSTOM_MAX:
        raise ValueError(f"{label}は {CUSTOM_MIN}〜{CUSTOM_MAX} の範囲で指定してください(32 の倍数に丸めます)")
    return max(CUSTOM_MIN, round(n / CUSTOM_STEP) * CUSTOM_STEP)


def resolve_format(params: dict) -> dict:
    """画角・画質・シーンの長さ・seed・背景の文字を検証して、サイズまで解決する(MV もショート動画も同じ規則)。不正なら ValueError(日本語)。
    画角が custom(手動入力)なら width / height をそのまま使う(32 の倍数に丸め、256〜1344)。"""
    aspect = str(params.get("aspect") or "16:9")
    if aspect not in ASPECTS:
        raise ValueError("画角は " + " / ".join(ASPECTS) + " のいずれかです")
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
        except (TypeError, ValueError, OverflowError):        # OverflowError: 無限大(int(float("inf")))
            raise ValueError("seed は整数で指定してください") from None
    background_text = str(params.get("background_text") or "auto")
    if background_text not in ("auto", "large", "none"):
        raise ValueError("background_text は auto / large / none のいずれかです")
    if aspect == CUSTOM:
        width, height = (_custom_side(params.get(k), label) for k, label in (("width", "幅"), ("height", "高さ")))
    else:
        width, height = SIZES[aspect][quality]
    return {"aspect": aspect, "quality": quality, "width": width, "height": height, "min_s": min_s, "max_s": max_s, "seed": seed,
            "background_text": background_text}


def resolve_params(ledger, params: dict) -> dict:
    """画面・API から来た入力を検証し、曲・画像の生成物の中身で既定を埋めて、制作の params にする。不正なら ValueError(日本語)。
    人物の説明・曲の内容の出どころは character_source(typed / image)・concept_source(typed / song)に残す(空は ""。制作の中で、画像を見る・曲を聞いて
    埋めたら、vision / listen になる)。曲の内容が空のまま(台帳にも手がかりが無い)のときは listen = "pending" で、曲を聞く段が入る。"""
    song = pick_song(ledger, params.get("song_id"))
    image = pick_image(ledger, params.get("image_id"))
    fmt = resolve_format(params)
    # 歌わせる(リップシンク。ボーカルを H3 に渡して口を合わせる)か、歌わせない(イメージ MV。音声を渡さず、画像とプロンプトだけで作る)か
    lipsync = params.get("lipsync", True)
    if not isinstance(lipsync, bool):
        raise ValueError("lipsync は true / false で指定してください")
    typed_character, image_prompt = _clip(str(params.get("character") or ""), CHARACTER_MAX), _clip(str(image["params"].get("prompt") or ""), CHARACTER_MAX)
    typed_concept, song_concept = _clip(str(params.get("concept") or ""), CONCEPT_MAX), concept_from_song(song["params"])
    character, concept = typed_character or image_prompt, typed_concept or song_concept
    title = _clip(str(song["params"].get("theme") or Path(song.get("filename") or "").stem or "MV"), 60) or "MV"
    return {"song_id": song["id"], "image_id": image["id"], "character": character, "concept": concept, **fmt, "lipsync": lipsync, "title": title,
            "character_source": "typed" if typed_character else "image" if image_prompt else "",
            "concept_source": "typed" if typed_concept else "song" if song_concept else "", "listen": "" if concept else "pending"}


# ---- シーン・次にやること ----

def make_scenes(plan_scenes: list) -> list:
    """分析(量子化済み)のシーンを、制作が持つシーンの形にする。演出(prompt・seed)はまだ空。"""
    return [{"index": int(s["index"]), "start": s["start"], "end": s["end"], "duration": s["duration"],
             "start_frame": s.get("start_frame"), "end_frame": s.get("end_frame"), "frame_count": s.get("frame_count"),
             "kind": s.get("kind") or "instrumental", "vocal_ratio": s.get("vocal_ratio") or 0.0,
             "boundary_kind": s.get("boundary_kind"), "prompt": "", "emotion": "", "seed": None, "status": "pending",
             "clip": None, "thumb": None, "elapsed_s": None, "peak_vram_gb": None, "error": None} for s in plan_scenes]


def next_step(proj: dict) -> tuple:
    """制作のデータから、次にやること: ("analyze",) / ("listen",) / ("script",) / ("scene", 番号) / ("assemble",) / ("done",)。"""
    params, scenes = proj["params"], proj["scenes"]
    if not params.get("analysis") or not scenes:
        return ("analyze",)
    if params.get("listen") == "pending":
        return ("listen",)
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
    stage = job.kind.split(".", 1)[1]
    return ("scene", job.params.get("scene")) if stage == "scene" else (stage,)


class Director:
    """制作ごとに、ジョブを 1 つずつ入れて進める。runner は JobRunner、ledger と outputs_dir は管理役のもの。

    MV(kind = "mv")とショート動画(kind = "short"。studio/short.py の ShortDirector)は同じ規則で動く。違いは下のクラス属性と、
    入力の検証(_resolve)・次にやること(_next_step)・画面に出す項目だけで、一時停止・再開・中止・やり直し・結合し直し・削除・進みは共通。
    """
    KIND = "mv"                    # 台帳の kind 列。一覧・復元はこの種類だけを見る
    PREFIX = "mv"                  # ジョブの種類の接頭辞(mv.analyze …)。ジョブの tab にも使う(生成物の tab になる)
    NOUN = "MV"
    WORK_SUB = "mv"                # outputs/<WORK_SUB>/<制作の ID>/ が作業フォルダ
    STAGE_LABEL = STAGE_LABEL
    TEXT = {"analyze": "曲を分析します", "listen": "曲を聞いて、曲の内容を考えます", "plan": "物語からシーンに分けます", "script": "演出を書きます",
            "assemble": "シーンを結合します", "done": "MV ができました"}
    PARAM_KEYS = INPUT_KEYS        # 画面(view)に出す制作の入力
    SCENE_KEYS = ("index", "start", "end", "duration", "kind", "vocal_ratio", "prompt", "emotion", "seed", "status", "elapsed_s",
                  "peak_vram_gb", "error")

    def __init__(self, runner, ledger, outputs_dir):
        self.runner, self.ledger, self.outputs_dir = runner, ledger, Path(outputs_dir)
        self.stage_state = {f"{self.PREFIX}.{k}": v for k, v in STAGE_STATE.items()}      # ジョブの種類 -> 制作の状態
        self._active: dict = {}        # 制作の ID -> 入れてあるジョブの ID(制作ごとに高々 1 つ。ジョブが終わった通知で外す)
        runner.on_finished(self._on_finished)

    # ---- 種類ごとに違うところ(ShortDirector が置き換える) ----

    def _resolve(self, params: dict) -> dict:
        return resolve_params(self.ledger, params)

    def _next_step(self, proj: dict) -> tuple:
        return next_step(proj)

    def work(self, pid: str) -> Path:
        return Path(self.outputs_dir) / self.WORK_SUB / pid

    def _params_view(self, params: dict) -> dict:
        return {k: params.get(k) for k in self.PARAM_KEYS}

    def _summary_extra(self, params: dict) -> dict:
        """一覧に足す、種類ごとの項目。"""
        return {"lipsync": params.get("lipsync", True)}

    def _cover_id(self, params: dict):
        """一覧のサムネイルに使う画像(最終の動画ができるまでの代わり)の生成物の ID。"""
        return params.get("image_id")

    def _stage(self, job):
        """このジョブがこの種類の制作の段階なら、その名前(analyze / scene …)、そうでなければ None。"""
        prefix, _, stage = job.kind.partition(".")
        return stage if prefix == self.PREFIX and job.kind in self.stage_state else None

    # ---- 入口 ----

    def create(self, params: dict) -> dict:
        """入力を検証して制作を作り、最初のジョブ(分析)を入れる。不正なら ValueError(日本語)。制作の画面用の辞書を返す。"""
        resolved = self._resolve(params)
        pid = uuid.uuid4().hex[:12]
        self.work(pid).mkdir(parents=True, exist_ok=True)
        self.ledger.create_mv_project(pid, "analyzing", resolved, [], self.TEXT["analyze"], kind=self.KIND)
        self._advance(pid)
        return self.view(pid)

    def restore(self) -> int:
        """管理役の起動時: 走っていた制作(ジョブは再起動で消えた)を一時停止にし、「生成中」のシーンを未生成に戻す。一時停止にした数を返す。
        一時停止・中止の後も走り続けていたシーンのジョブ(終わるまで続ける)が再起動で消えた制作は、状態はそのままで、シーンだけを未生成に戻す。"""
        n = 0
        for proj in self.ledger.list_mv_projects(limit=100000, kind=self.KIND):
            patches = {s["index"]: {"status": "pending"} for s in proj["scenes"] if s["status"] == "running"}
            if proj["state"] in RUNNING:
                self.ledger.update_mv_project(proj["id"], state="paused", message=RESTART_MESSAGE, scene_patches=patches)
                n += 1
            elif patches:
                self.ledger.update_mv_project(proj["id"], scene_patches=patches)
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
        stage = self._stage(job) if job is not None else None
        if stage == "assemble":
            raise ValueError("結合しています。終わってからやり直してください")
        if stage == "scene" and job.params.get("scene") == scene["index"]:
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

    def _check_reassemble(self, pid: str) -> None:
        proj = self._get(pid)
        if not proj["scenes"] or any(s["status"] != "done" for s in proj["scenes"]):
            raise ValueError("全部のシーンができてから結合できます")
        if self._inflight(pid) is not None:
            raise ValueError("制作中です。終わってから結合し直してください")

    def reassemble(self, pid: str) -> dict:
        self._check_reassemble(pid)
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
        await asyncio.to_thread(shutil.rmtree, self.work(pid), True)

    def output_ids(self, pid: str) -> list:
        """この制作の動画の生成物の ID(新しい順)。結合し直すたびに残る前の版も含む(生成物の params.project_id がこの制作のもの)。制作が無ければ LookupError。"""
        self._get(pid)
        return [o["id"] for o in self.ledger.list_outputs(limit=100000) if o["params"].get("project_id") == pid]

    # ---- 見せる ----

    def view(self, pid: str) -> Optional[dict]:
        proj = self.ledger.get_mv_project(pid)
        return self._view(proj) if proj and proj.get("kind", "mv") == self.KIND else None

    def list(self, limit: int = 100) -> list:
        """新しい順の一覧(シーンの中身は含めない)。"""
        return [self._summary(p) for p in self.ledger.list_mv_projects(limit, kind=self.KIND)]

    def _get(self, pid: str) -> dict:
        proj = self.ledger.get_mv_project(pid)
        if proj is None or proj.get("kind", "mv") != self.KIND:       # 別の種類の制作の ID も「無い」ことにする(MV の API でショート動画を触らせない)
            raise LookupError(f"その{self.NOUN}の制作は見つかりません(削除されたかもしれません)")
        return proj

    def _view(self, proj: dict) -> dict:
        pid, params = proj["id"], proj["params"]
        job = self._inflight(pid)
        out = self.ledger.get_output(proj["output_id"]) if proj["output_id"] else None
        return {"id": pid, "title": params.get("title") or self.NOUN, "state": proj["state"], "message": self._message(proj, job),
                "error": proj["error"], "created_at": proj["created_at"], "updated_at": proj["updated_at"],
                "params": self._params_view(params), "analysis": params.get("analysis"), "script": params.get("script"),
                "sources": {"character": params.get("character_source") or "", "concept": params.get("concept_source") or ""},
                "source_notes": {"character": params.get("character_note") or "", "concept": params.get("listen_error") or ""},
                "assembled": bool(params.get("assembled")), "scenes": [self._scene_view(pid, s, job) for s in proj["scenes"]],
                "job_id": job.id if job else None, "job_status": job.status if job else None,
                "progress": self._progress(proj, job), "output": out}

    def _summary(self, proj: dict) -> dict:
        params, job = proj["params"], self._inflight(proj["id"])
        out = self.ledger.get_output(proj["output_id"]) if proj["output_id"] else None
        thumb = (out or {}).get("thumb_path") or (self.ledger.get_output(self._cover_id(params) or "") or {}).get("thumb_path")
        return {"id": proj["id"], "title": params.get("title") or self.NOUN, "state": proj["state"], "message": self._message(proj, job),
                "error": proj["error"], "created_at": proj["created_at"], "updated_at": proj["updated_at"],
                "aspect": params.get("aspect"), "quality": params.get("quality"), "width": params.get("width"),
                "height": params.get("height"), **self._summary_extra(params), "scenes": len(proj["scenes"]),
                "done": sum(1 for s in proj["scenes"] if s["status"] == "done"), "progress": self._progress(proj, job),
                "job_id": job.id if job else None, "output": out, "thumb_url": "/outputs/" + thumb if thumb else None}

    def _scene_view(self, pid: str, s: dict, job) -> dict:
        d = self.work(pid)

        def url(name):
            f = d / name if name else None
            return f"/outputs/{self.WORK_SUB}/{pid}/{name}?v={int(f.stat().st_mtime)}" if f is not None and f.is_file() else None
        running = job is not None and self._stage(job) == "scene" and job.params.get("scene") == s["index"]
        return {**{k: s.get(k) for k in self.SCENE_KEYS},
                "clip_url": url(s.get("clip")) if s["status"] == "done" else None, "thumb_url": url(s.get("thumb")),
                "job_status": job.status if running else None}

    def _message(self, proj: dict, job) -> str:
        """画面に出す一文。走っているジョブがあれば、そのジョブの今(順番待ち・準備中・「生成中 2/4」など)。"""
        if job is None or proj["state"] not in RUNNING:
            return proj["message"] or ""
        msg = job.message or proj["message"] or ""
        if self._stage(job) == "scene" and not msg.startswith("シーン"):
            msg = f"シーン {job.params.get('scene')} / {len(proj['scenes'])}: {msg}"
        return msg

    def _progress(self, proj: dict, job) -> float:
        """0〜1。段階の重みで足し、シーンは尺に比例して分ける。走っているジョブの途中の進みも入れる。"""
        if proj["state"] == "completed":
            return 1.0
        params, scenes = proj["params"], proj["scenes"]
        live = max(0.0, min(1.0, job.progress if job is not None and job.status == "running" else 0.0))
        kind = (self._stage(job) or "") if job is not None else ""
        total = WEIGHT_ANALYZE if params.get("analysis") else (WEIGHT_ANALYZE * live if kind == "analyze" else 0.0)
        total += WEIGHT_SCRIPT if scenes and all(s.get("prompt") for s in scenes) else (WEIGHT_SCRIPT * live if kind == "script" else 0.0)
        length = sum(s["duration"] for s in scenes) or 1.0
        for s in scenes:
            w = WEIGHT_SCENES * s["duration"] / length
            if s["status"] == "done":
                total += w
            elif kind == "scene" and job.params.get("scene") == s["index"]:
                total += w * live
        total += WEIGHT_ASSEMBLE if params.get("assembled") else (WEIGHT_ASSEMBLE * live if kind == "assemble" else 0.0)
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
        job = self.runner.submit(self.PREFIX, f"{self.PREFIX}.{kind}", {"project_id": pid, **params})
        self._active[pid] = job.id
        return job

    def _start(self, pid: str) -> dict:
        """走っているジョブがあればその段階として扱い(終わったら続きを入れる)、無ければ次の段階のジョブを入れる。"""
        job = self._inflight(pid, live=True)
        if job is not None:
            self._set(pid, self.stage_state[job.kind], "再開しました")
        else:
            self._advance(pid)
        return self.view(pid)

    def _advance(self, pid: str) -> None:
        """次の段階のジョブを入れる(全部できていれば完了にする)。"""
        proj = self.ledger.get_mv_project(pid)
        if proj is None:
            return
        step, n = self._next_step(proj), len(proj["scenes"])
        try:
            if step[0] == "scene":
                self._set(pid, "generating", f"シーン {step[1]} / {n} を生成します", scene_patches={step[1]: {"status": "running", "error": None}})
                self._submit(pid, "scene", scene=step[1])
            elif step[0] in STAGE_STATE:
                self._set(pid, STAGE_STATE[step[0]], self.TEXT[step[0]])
                self._submit(pid, step[0])
            else:
                self._set(pid, "completed", self.TEXT["done"])
        except ValueError as e:       # ジョブを受け付けられない(制作のデータが壊れている、など)
            log.warning("制作 %s のジョブを入れられません: %s", pid, e)
            self._set(pid, "failed", "ジョブを始められませんでした", error=str(e))

    def _on_finished(self, job) -> None:
        """JobRunner から、ジョブが終わるたびに呼ばれる。この制作のジョブなら、結果に応じて次へ進む・止める。"""
        stage = self._stage(job)
        pid = job.params.get("project_id") if stage else None
        if not pid or self._active.get(pid) != job.id:
            return
        del self._active[pid]
        proj = self.ledger.get_mv_project(pid)
        if proj is None:       # 走っている間に消された制作: ジョブが後から書いたファイルを片づける
            shutil.rmtree(self.work(pid), ignore_errors=True)
            return
        state, scene = proj["state"], job.params.get("scene")
        label = self.STAGE_LABEL[stage]
        if job.status == "completed":
            if state in RUNNING:
                if self._next_step(proj) == step_of(job):       # 完了したのに結果が記録されていない(同じことを繰り返さない)
                    self._set(pid, "failed", f"{label}の結果を記録できませんでした", error="ジョブは完了しましたが、結果が制作に書かれていません")
                else:
                    self._advance(pid)
            elif state == "paused":
                self._set(pid, "paused", "一時停止しました")
            return
        if stage == "scene":       # 失敗・中止したシーンは、完成していない(中止なら、また最初から)
            patch = {"status": "failed", "error": job.error} if job.status == "failed" and state != "cancelled" else {"status": "pending"}
            self.ledger.update_mv_project(pid, scene_patches={scene: patch})
        if state == "cancelled":
            return
        if job.status == "failed" and stage == "listen":      # 曲を聞けなくても MV は作れる: 理由を残して、曲の内容なしで続ける
            self.ledger.update_mv_project(pid, params_patch={"listen": "skipped", "listen_error": job.error or "原因不明"})
            if state in RUNNING:
                self._advance(pid)
            else:
                self._set(pid, "paused", "一時停止しました")
        elif job.status == "failed":
            self._set(pid, "failed", f"{label}に失敗しました", error=job.error or "原因不明")
        elif state == "paused":       # 一時停止で取り下げた(まだ始まっていなかった)ジョブ
            self._set(pid, "paused", "一時停止しました")
        elif state in RUNNING:       # 管理役の画面の外(ジョブの一覧など)から、このジョブだけ中止された
            self._set(pid, "paused", f"{label}が中止されたので一時停止しました")


def _int(value, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} は整数で指定してください")
