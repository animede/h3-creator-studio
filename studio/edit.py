"""編集タブの管理役(EditManager): 編集プロジェクトを持ち、状態(state)の更新・元に戻す・やり直し・プレビュー・書き出しを受け付ける。

状態の検証・分割などの計算は studio/editclip.py、ffmpeg の実行は studio/handlers/edit.py(ジョブ edit.preview / edit.export。どちらも CPU のレーン)。
台帳は edit_projects 表(state と履歴、プレビューの記録、最後に書き出した動画)。ここは、それらをつなぎ、画面に出す view を作る。

  set_state  → 検証して履歴に積む(プレビューは自動では作らない。画面が続けて preview を呼ぶ)
  preview    → 走っている(順番待ちを含む)プレビューを中止して、いまの状態の edit.preview ジョブを入れる
  export     → edit.export ジョブを入れる。書き出しが走っていれば ExportBusy(API は 409)。
               upscale={"method", "scale"} を渡せば、書き出した動画を AI で拡大する(studio/upscale.py の resolve で検証し、params["upscale"] に入れる)
プレビューは「作ったときの state の指紋」を台帳に残し、いまの state の指紋と違えば stale(最後の操作より古い)。
ジョブの params に state の写しを入れる(順番待ちの間に操作が続いても、頼んだ時点の状態を作る。ギャラリーの削除の保護も、params に入った ID を見る)。
状態の変更はイベントループのスレッドで行う(API のルートは async def)。
"""
import asyncio
import logging
import re
import shutil
import uuid
from pathlib import Path
from typing import Optional

from . import config, editclip
from . import upscale as upscale_mod
from .jobs import TERMINAL

log = logging.getLogger("studio.edit")

NAME_MAX = 80
DEFAULT_NAME = "無題の編集"
WORK_SUB = "edit"                  # outputs/edit/<プロジェクトの ID>/preview_<N>_<state の指紋>.mp4
PREVIEW_RE = re.compile(r"^preview_(\d+)(?:_([0-9a-f]{16}))?\.mp4$")      # 指紋の無い古い名前も読む
PREVIEW_KEEP = 8                   # 残すプレビューの数(元に戻す・やり直すで、前の状態のプレビューをそのまま使う)


class ExportBusy(ValueError):
    """書き出しが走っているので、もう 1 つは受け付けない(API は 409)。"""


def work_dir(outputs_dir, pid: str) -> Path:
    return Path(outputs_dir) / WORK_SUB / pid


def preview_name(seq: int, state_hash: str = "") -> str:
    return f"preview_{int(seq)}" + (f"_{state_hash}" if state_hash else "") + ".mp4"


def cached_preview(work: Path, state_hash: str) -> Optional[Path]:
    """その指紋の状態で作ったプレビューが残っていれば、その(いちばん新しい)ファイル。"""
    found = [(int(m.group(1)), f) for f in work.glob(f"preview_*_{state_hash}.mp4") if (m := PREVIEW_RE.match(f.name)) and m.group(2) == state_hash]
    return max(found)[1] if found else None


def sources_for(ledger, state: dict) -> dict:
    """状態が使う生成物(クリップと曲)の、検証に要る情報を台帳から集める: {ID: {"kind", "length_s", "width", "height", "fps"}}。台帳に無い ID は含めない。
    fps は動画のコマ率(登録のときに ffprobe で読んだもの。それより前の行は空で、書き出しは 24 になる)。"""
    out = {}
    for oid in editclip.used_ids(state):
        rec = ledger.get_output(oid)
        if rec is not None:
            out[oid] = {"kind": rec["kind"], "length_s": None if rec["kind"] == "image" else rec.get("duration_s"),
                        "width": rec.get("width"), "height": rec.get("height"), "fps": rec.get("fps")}
    return out


class EditManager:
    """runner は JobRunner、ledger と outputs_dir は管理役のもの。settings は書き出しの拡大の道具の場所と長辺の上限([upscale])を読む設定
    (無ければ studio.config.settings)。"""

    def __init__(self, runner, ledger, outputs_dir, settings=None):
        self.runner, self.ledger, self.outputs_dir = runner, ledger, Path(outputs_dir)
        self.settings = settings if settings is not None else getattr(config, "settings", None)
        self._jobs: dict = {}      # プロジェクトの ID -> {"preview": ジョブの ID, "export": ジョブの ID, "last": 最後に入れたジョブの ID}
        self._seq: dict = {}       # プロジェクトの ID -> 最後に割り当てたプレビューの番号

    # ---- 入口 ----

    def create(self, name: str = "", state: dict = None) -> dict:
        """新しい編集プロジェクト(state を渡せば最初の状態に。渡さなければ空)。不正なら ValueError(日本語)。"""
        name = self._name(name, allow_empty=True)
        first = editclip.validate_state(state, sources_for(self.ledger, state)) if state else editclip.empty_state()
        pid = uuid.uuid4().hex[:12]
        self.ledger.create_edit_project(pid, name or DEFAULT_NAME, first)
        return self.view(pid)

    def set_state(self, pid: str, state: dict) -> dict:
        """state 全体を置き換える(検証して、履歴に 1 段積む)。"""
        self._get(pid)
        if not isinstance(state, dict):
            raise ValueError("state は辞書で指定してください")
        checked = editclip.validate_state(state, sources_for(self.ledger, state))
        self.ledger.push_edit_state(pid, checked)
        return self.view(pid)

    def undo(self, pid: str) -> dict:
        self._get(pid)
        self.ledger.move_edit_cursor(pid, -1)
        return self.view(pid)

    def redo(self, pid: str) -> dict:
        self._get(pid)
        self.ledger.move_edit_cursor(pid, +1)
        return self.view(pid)

    def rename(self, pid: str, name: str) -> dict:
        self._get(pid)
        self.ledger.update_edit_project(pid, name=self._name(name))
        return self.view(pid)

    async def preview(self, pid: str) -> dict:
        """いまの状態のプレビューを作るジョブを入れる。走っている(順番待ちを含む)プレビューは中止する。
        その状態のプレビューが残っていれば(元に戻した・やり直した)、作らずにそれを台帳の記録にする。"""
        proj = self._get(pid)
        self._check_ready(proj["state"])
        old = self._live(pid, "preview")
        if old is not None:
            await self.runner.cancel(old.id)
        h = editclip.state_hash(proj["state"])
        cached = cached_preview(work_dir(self.outputs_dir, pid), h)
        if cached is not None:
            self.ledger.update_edit_project(pid, preview={"n": int(PREVIEW_RE.match(cached.name).group(1)), "hash": h, "path": self.ledger.rel_path(cached)})
            return self.view(pid)
        job = self.runner.submit("edit", "edit.preview", {"project_id": pid, "state": proj["state"], "seq": self._next_seq(pid)})
        self._track(pid, "preview", job)
        return self.view(pid)

    def export(self, pid: str, upscale: dict = None) -> dict:
        """いまの状態を書き出すジョブを入れる。書き出しが走っていれば ExportBusy。
        upscale = {"method": "auto" / 方式の id, "scale": 1〜4} なら、書き出した動画を AI で拡大する(倍率 1 は拡大しない)。使えない・大きすぎれば ValueError。"""
        proj = self._get(pid)
        if self._live(pid, "export") is not None:
            raise ExportBusy("書き出しの途中です。終わってからもう一度押してください")
        self._check_ready(proj["state"])
        params = {"project_id": pid, "state": proj["state"]}
        if upscale:
            if not isinstance(upscale, dict):
                raise ValueError("upscale は {method, scale} の辞書で指定してください")
            state = proj["state"]
            resolved = upscale_mod.resolve(self.settings, upscale.get("method", upscale_mod.AUTO), upscale.get("scale"), state["width"], state["height"],
                                           upscale_mod.max_edge(self.settings))
            if resolved is not None:
                params["upscale"] = resolved
        job = self.runner.submit("edit", "edit.export", params)
        self._track(pid, "export", job)
        return self.view(pid)

    async def delete(self, pid: str) -> None:
        """プロジェクトとプレビューを消す(走っているジョブは中止する)。書き出した動画はギャラリーに残る。"""
        self._get(pid)
        live = [j for j in (self._live(pid, "preview"), self._live(pid, "export")) if j is not None]
        self.ledger.delete_edit_project(pid)
        self._jobs.pop(pid, None)
        self._seq.pop(pid, None)
        for job in live:
            await self.runner.cancel(job.id)
        await asyncio.to_thread(shutil.rmtree, work_dir(self.outputs_dir, pid), True)

    def probe(self, oid: str) -> dict:
        """生成物の種類・長さ・寸法(画面がドロップした素材の長さを知る用)。無ければ LookupError。"""
        rec = self.ledger.get_output(str(oid or "").strip())
        if rec is None:
            raise LookupError("その生成物は見つかりません(ギャラリーから削除されたかもしれません)")
        return self._source_view(rec)

    # ---- 見せる ----

    def view(self, pid: str) -> Optional[dict]:
        proj = self.ledger.get_edit_project(pid)
        return self._view(proj) if proj else None

    def list(self, limit: int = 200) -> list:
        """更新が新しい順の一覧(state の中身は含めない)。"""
        out = []
        for proj in self.ledger.list_edit_projects(limit):
            state = proj["state"]
            output = self._output(proj)
            thumb = (output or {}).get("thumb_path")
            if not thumb:
                first = self.ledger.get_output(state["clips"][0]["id"]) if state.get("clips") else None
                thumb = (first or {}).get("thumb_path")
            out.append({"id": proj["id"], "name": proj["name"], "updated_at": proj["updated_at"], "duration": editclip.export_duration(state),
                        "clips": len(state.get("clips") or []), "thumb_url": "/outputs/" + thumb if thumb else None, "output": output})
        return out

    def extract_frame(self, pid: str, t: float) -> dict:
        """プレビューの再生位置 t(書き出しの秒)に映っているコマを、元の動画から元の解像度で PNG に抜き出してギャラリー(画像)に載せ、その台帳の行を返す。
        色調などの効果は掛けない(元のコマ)。静止画のクリップなら、その画像の行をそのまま返す。同期(1 コマなので一瞬)。"""
        from . import mv_audio
        proj = self._get(pid)
        src = editclip.source_at(proj["state"], t)
        if src is None:
            raise ValueError("クリップがありません")
        rec = self.ledger.get_output(src["clip"]["id"])
        path = self.ledger.abs_path(rec["path"]) if rec is not None else None
        if path is None or not path.is_file():
            raise ValueError(f"クリップ {src['index'] + 1} のファイルが見つかりません(ギャラリーから削除されたかもしれません)")
        if rec["kind"] == "image":
            return rec
        # クリップの最後の 1 コマの中(再生位置が動画の終わり・「1 コマ ▶」で端まで進めたとき)を -ss で指すと、そこから始まるコマが無く
        # 何も抜けない(ffmpeg は終了コード 0 で、ファイルを書かない)。最後のコマの頭(out - 1 コマ。in より前には戻さない)までに収める
        clip, fps = src["clip"], float(proj["state"].get("fps") or editclip.DEFAULT_FPS)
        at = round(min(float(src["source_s"]), max(float(clip["in"]), float(clip["out"]) - 1.0 / fps)), 3)
        dst = Path(self.outputs_dir) / "image" / f"frame_{pid}_{uuid.uuid4().hex[:8]}.png"
        try:
            exe = mv_audio._ffmpeg()
            mv_audio._run_ffmpeg([exe, "-y", "-v", "error", "-ss", f"{at:.3f}", "-i", str(path), "-frames:v", "1", "-f", "image2",
                                  str(mv_audio._part(dst))], dst, "コマの抜き出し")
        except mv_audio.MvAudioError as e:
            raise ValueError(e.message) from e
        name = f"{Path(rec.get('filename') or 'clip').stem}_{at:.2f}s.png"
        return self.ledger.add_output("image", dst, "edit", None, name, {"source_id": rec["id"], "source_s": at, "project_id": pid, "frame": True})

    def _get(self, pid: str) -> dict:
        proj = self.ledger.get_edit_project(pid)
        if proj is None:
            raise LookupError("その編集プロジェクトは見つかりません(削除されたかもしれません)")
        return proj

    def _source_view(self, rec: dict) -> dict:
        return {"kind": rec["kind"], "length_s": None if rec["kind"] == "image" else rec.get("duration_s"), "width": rec.get("width"),
                "height": rec.get("height"), "filename": rec.get("filename"), "thumb_url": "/outputs/" + rec["thumb_path"] if rec.get("thumb_path") else None}

    def _clip_view(self, clip: dict) -> dict:
        rec = self.ledger.get_output(clip["id"])
        if rec is None:
            return {**clip, "filename": None, "thumb_url": None, "length_s": None, "width": None, "height": None, "missing": True}
        try:
            missing = not self.ledger.abs_path(rec["path"]).is_file()
        except ValueError:
            missing = True
        src = self._source_view(rec)
        return {**clip, "filename": src["filename"], "thumb_url": src["thumb_url"], "length_s": src["length_s"], "width": src["width"],
                "height": src["height"], "missing": missing}

    def _output(self, proj: dict) -> Optional[dict]:
        """最後に書き出した動画(台帳の行。ギャラリーから消されていれば None)。公開の形にするのは API 側(_public)。"""
        return self.ledger.get_output(proj["output_id"]) if proj.get("output_id") else None

    def _preview_view(self, proj: dict) -> Optional[dict]:
        """いまの状態のプレビューが残っていればそれ(stale でない)。無ければ台帳に記録した最後のプレビュー(いまの状態と違えば stale)。"""
        h = editclip.state_hash(proj["state"])
        cached = cached_preview(work_dir(self.outputs_dir, proj["id"]), h)
        if cached is not None:
            return {"url": "/outputs/" + self.ledger.rel_path(cached), "stale": False}
        p = proj.get("preview")
        if not p or not p.get("path"):
            return None
        try:
            exists = self.ledger.abs_path(p["path"]).is_file()
        except ValueError:
            exists = False
        return {"url": "/outputs/" + p["path"], "stale": p.get("hash") != h} if exists else None

    def _job_view(self, pid: str) -> Optional[dict]:
        job = self._live(pid, "export") or self._live(pid, "preview")
        if job is None:
            return None
        return {"id": job.id, "kind": job.kind, "status": job.status, "progress": job.progress, "message": job.message, "error": job.error}

    def _error(self, pid: str) -> Optional[str]:
        """最後に入れたジョブが失敗していれば、その理由(画面が「プレビューが作れない」と出すため。次のジョブを入れると消える)。"""
        job = self.runner.get(self._jobs.get(pid, {}).get("last") or "")
        return job.error if job is not None and job.status == "failed" else None

    def _view(self, proj: dict) -> dict:
        state = proj["state"]
        return {"id": proj["id"], "name": proj["name"], "created_at": proj["created_at"], "updated_at": proj["updated_at"], "state": state,
                "duration": editclip.export_duration(state), "timeline": editclip.timeline(state),
                "clips": [self._clip_view(c) for c in state.get("clips") or []],
                "can_undo": proj["can_undo"], "can_redo": proj["can_redo"], "preview": self._preview_view(proj),
                "job": self._job_view(proj["id"]), "output": self._output(proj), "error": self._error(proj["id"])}

    # ---- ジョブ ----

    def _live(self, pid: str, kind: str):
        """プロジェクトの preview / export のうち、終わっていない(順番待ち・準備中・実行中)ジョブ。"""
        job = self.runner.get(self._jobs.get(pid, {}).get(kind) or "")
        return job if job is not None and job.status not in TERMINAL else None

    def _track(self, pid: str, kind: str, job) -> None:
        self._jobs.setdefault(pid, {}).update({kind: job.id, "last": job.id})

    def _next_seq(self, pid: str) -> int:
        """次のプレビューの番号(ブラウザのキャッシュを避けるため、毎回変える)。再起動後は、台帳の記録とフォルダの中身から続ける。"""
        if pid not in self._seq:
            found = [int(m.group(1)) for f in work_dir(self.outputs_dir, pid).glob("preview_*.mp4") if (m := PREVIEW_RE.match(f.name))]
            stored = (self.ledger.get_edit_project(pid) or {}).get("preview") or {}
            self._seq[pid] = max([0, int(stored.get("n") or 0), *found])
        self._seq[pid] += 1
        return self._seq[pid]

    def _check_ready(self, state: dict) -> None:
        """作れる状態か(クリップがあり、ファイルが残っているか)。"""
        if not state.get("clips"):
            raise ValueError("クリップがありません。ギャラリーの動画・画像を入れてください")
        for n, c in enumerate(state["clips"], start=1):
            rec = self.ledger.get_output(c["id"])
            if rec is None or not self.ledger.abs_path(rec["path"]).is_file():
                raise ValueError(f"クリップ {n} のファイルが見つかりません(削除されたかもしれません)。外してください")

    @staticmethod
    def _name(name, allow_empty: bool = False) -> str:
        name = str(name or "").strip()
        if not name and not allow_empty:
            raise ValueError("名前を入力してください")
        if len(name) > NAME_MAX:
            raise ValueError(f"名前は {NAME_MAX} 文字までです")
        return name
