"""編集タブのハンドラ: edit.preview / edit.export(どちらも CPU の仕事。GPU も部品も使わない)。

  preview : いまの状態を短辺 480 の軽い MP4 にして outputs/edit/<プロジェクトの ID>/preview_<N>.mp4 に置き、台帳の preview に(state の指紋と一緒に)記録する。
            これより古い番号のプレビューは消す。ギャラリーには載せない
  export  : 元の寸法の MP4 を outputs/video/edit_<ID>.mp4(2 回目以降 _2…)に作り、ギャラリー(kind = video、tab = edit)に載せて、プロジェクトの output_id にする
ジョブの params は {"project_id", "state"(頼んだ時点の状態の写し), ("seq": プレビューの番号)}。ffmpeg の引数は studio/editclip.py の render_args。
ffmpeg は子プロセスとして別スレッドで走らせ、標準出力の -progress(out_time)から進みを出す。中止(cancel)は子プロセスを止める。
"""
import asyncio
import logging
import os
import re
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

from .. import edit as edit_mod
from .. import editclip, media, mv_audio
from ..jobs import Ctx, Handler, Job, JobCancelled, JobFailed
from .mv import final_path

log = logging.getLogger("studio.edit")

LOG_TAIL_LINES = 40
STALL_S = 600                       # ffmpeg の進み(out_time)がこれだけ止まったら、止まったと見て中止する
# 走っている ffmpeg(ジョブの ID -> Popen)。cancel がここから子プロセスを止める
_procs: dict = {}


def normalize(kind: str, params: dict, ledger=None) -> dict:
    """ジョブの params を検証する: プロジェクトの ID と、作る状態(渡されなければ、いまの状態)。不正なら ValueError(日本語)。"""
    pid = str(params.get("project_id") or "").strip()
    proj = ledger.get_edit_project(pid) if ledger is not None and pid else None
    if not pid or (ledger is not None and proj is None):
        raise ValueError("編集プロジェクトが見つかりません(削除されたかもしれません)")
    state = params.get("state") if isinstance(params.get("state"), dict) else (proj["state"] if proj else None)
    if not state or not state.get("clips"):
        raise ValueError("クリップがありません。ギャラリーの動画・画像を入れてください")
    out = {"project_id": pid, "state": state}
    if kind == "edit.preview":
        try:
            out["seq"] = max(1, int(params.get("seq") or 1))
        except (TypeError, ValueError):
            raise ValueError("プレビューの番号を整数で指定してください") from None
    return out


# ---- ffmpeg の実行(進みつき) ----

def progress_seconds(line: str) -> Optional[float]:
    """-progress の 1 行から、出力の時刻(秒)を読む。out_time_us / out_time_ms(どちらもマイクロ秒)か out_time(HH:MM:SS.ffffff)。関係ない行・N/A は None。"""
    key, _, value = line.strip().partition("=")
    value = value.strip()
    if key in ("out_time_us", "out_time_ms"):
        try:
            return max(0.0, int(value) / 1_000_000)
        except ValueError:
            return None
    if key == "out_time":
        m = re.fullmatch(r"(\d+):(\d+):(\d+(?:\.\d+)?)", value)
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else None
    return None


def run_ffmpeg(job: Job, argv: list, dst: Path, total_s: float, label: str) -> None:
    """ffmpeg(argv の出力先は mv_audio._part(dst))を走らせ、進みを job に書き、成功したら dst に置き換える(失敗しても壊れたファイルを残さない)。
    別スレッドから呼ぶ(止まる処理)。中止されたら JobCancelled、失敗したら JobFailed。"""
    tmp = mv_audio._part(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                                creationflags=mv_audio._no_window())
    except OSError as e:
        raise JobFailed(f"{label}に失敗しました: ffmpeg を起動できません({e})") from e
    _procs[job.id] = proc
    tail, last, stalled = deque(maxlen=LOG_TAIL_LINES), [time.monotonic()], []

    def drain() -> None:
        for line in proc.stderr:
            tail.append(line.rstrip())

    def watch() -> None:
        while proc.poll() is None:
            time.sleep(1.0)
            if time.monotonic() - last[0] > STALL_S:
                stalled.append(True)
                proc.kill()
                return

    threads = [threading.Thread(target=drain, daemon=True), threading.Thread(target=watch, daemon=True)]
    for t in threads:
        t.start()
    try:
        if job.cancel_requested:         # 走り出す前に中止が来ていた
            proc.kill()
        for line in proc.stdout:
            sec = progress_seconds(line)
            if sec is not None:
                last[0] = time.monotonic()
                frac = min(0.99, sec / total_s) if total_s > 0 else 0.0
                job.progress, job.message = frac, f"{label} {round(frac * 100)}%"
        code = proc.wait()
    finally:
        _procs.pop(job.id, None)
        if proc.poll() is None:
            proc.kill()
        threads[0].join(timeout=5)         # stderr を読み切ってから、パイプを閉じる
        for pipe in (proc.stdout, proc.stderr):
            pipe.close()
    if job.cancel_requested:
        tmp.unlink(missing_ok=True)
        raise JobCancelled()
    if stalled:
        tmp.unlink(missing_ok=True)
        raise JobFailed(f"{label}が {STALL_S // 60} 分以上進まないので止めました(ffmpeg)", "\n".join(tail))
    if code != 0 or not tmp.is_file() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        last_line = tail[-1] if tail else f"終了コード {code}"
        raise JobFailed(f"{label}に失敗しました(ffmpeg): {last_line}", "\n".join(tail))
    os.replace(tmp, dst)


async def cancel_ffmpeg(job: Job, ctx: Ctx) -> None:
    """走っている ffmpeg を止める(run_ffmpeg が、止まったことを見て JobCancelled にする)。"""
    proc = _procs.get(job.id)
    if proc is not None and proc.poll() is None:
        proc.kill()


async def _encode(job: Job, argv: list, dst: Path, total_s: float, label: str) -> None:
    """run_ffmpeg を別スレッドで走らせて待つ。待っている側が取り消された(管理役の停止)ら、ffmpeg も止める。"""
    try:
        await asyncio.to_thread(run_ffmpeg, job, argv, dst, total_s, label)
    except asyncio.CancelledError:
        proc = _procs.get(job.id)
        if proc is not None:
            proc.kill()
        raise


# ---- 入力の用意 ----

def source_paths(ctx: Ctx, state: dict) -> tuple:
    """状態が使うファイルの {ID: {"path", "has_audio"}} と、曲の長さ(秒。曲を使わなければ None)。無ければ JobFailed。"""
    paths, song_len = {}, None
    for n, c in enumerate(state["clips"], start=1):
        rec = ctx.ledger.get_output(c["id"])
        path = ctx.ledger.abs_path(rec["path"]) if rec is not None else None
        if path is None or not path.is_file():
            raise JobFailed(f"クリップ {n} のファイルが見つかりません(ギャラリーから削除されたかもしれません)")
        # 音声の無い動画(MV の元のシーンなど)は、無音で補う。ffprobe が使えなければ「ある」として試す
        has_audio = False if c["kind"] == "image" else media.has_audio(path) is not False
        paths[c["id"]] = {"path": str(path), "has_audio": has_audio}
    audio = state.get("audio") or {}
    if audio.get("mode") in ("music", "mix"):
        rec = ctx.ledger.get_output(audio.get("song_id") or "")
        path = ctx.ledger.abs_path(rec["path"]) if rec is not None else None
        if path is None or not path.is_file():
            raise JobFailed("曲のファイルが見つかりません(ギャラリーから削除されたかもしれません)")
        paths[audio["song_id"]] = {"path": str(path), "has_audio": True}
        song_len = rec.get("duration_s")
    return paths, song_len


def _args(ctx: Ctx, job: Job, dst: Path, preview: bool) -> tuple:
    state = job.params["state"]
    paths, song_len = source_paths(ctx, state)
    try:
        exe = mv_audio._ffmpeg()
        argv = editclip.render_args(exe, state, paths, mv_audio._part(dst), preview=preview, progress=True, song_len=song_len)
    except mv_audio.MvAudioError as e:
        raise JobFailed(e.message, e.log_tail) from e
    except ValueError as e:
        raise JobFailed(str(e)) from e
    return state, argv


# ---- edit.preview ----

async def run_preview(job: Job, ctx: Ctx) -> None:
    if job.cancel_requested:
        raise JobCancelled()
    pid, seq = job.params["project_id"], job.params["seq"]
    if ctx.ledger.get_edit_project(pid) is None:
        raise JobFailed("編集プロジェクトが見つかりません(削除されたかもしれません)")
    work = edit_mod.work_dir(ctx.outputs_dir, pid)
    dst = work / edit_mod.preview_name(seq, editclip.state_hash(job.params["state"]))       # 名前に状態の指紋(params の state は正規化済み)
    state, argv = await asyncio.to_thread(_args, ctx, job, dst, True)
    await _encode(job, argv, dst, editclip.export_duration(state), "プレビューを作っています")
    if job.cancel_requested:
        dst.unlink(missing_ok=True)
        raise JobCancelled()
    cur = ctx.ledger.get_edit_project(pid)
    if cur is None or int((cur.get("preview") or {}).get("n") or 0) > seq:
        dst.unlink(missing_ok=True)         # プロジェクトが消えた・もっと新しいプレビューが先にできた
        return
    # 古いプレビューは、新しい順に PREVIEW_KEEP 本だけ残す(元に戻す・やり直すで、前の状態のものをそのまま使うため)
    older = sorted(((int(m.group(1)), f) for f in work.glob("preview_*.mp4") if (m := edit_mod.PREVIEW_RE.match(f.name)) and int(m.group(1)) < seq), reverse=True)
    for _, f in older[edit_mod.PREVIEW_KEEP - 1:]:
        f.unlink(missing_ok=True)
    ctx.ledger.update_edit_project(pid, preview={"n": seq, "hash": editclip.state_hash(state), "path": ctx.ledger.rel_path(dst)})
    log.info("編集 %s: プレビュー %d ができました(%.1f 秒)", pid, seq, editclip.export_duration(state))


# ---- edit.export ----

async def run_export(job: Job, ctx: Ctx) -> None:
    if job.cancel_requested:
        raise JobCancelled()
    pid = job.params["project_id"]
    proj = ctx.ledger.get_edit_project(pid)
    if proj is None:
        raise JobFailed("編集プロジェクトが見つかりません(削除されたかもしれません)")
    dest = final_path(ctx.outputs_dir, pid, "edit")
    state, argv = await asyncio.to_thread(_args, ctx, job, dest, False)
    duration = editclip.export_duration(state)
    await _encode(job, argv, dest, duration, "書き出しています")
    job.progress, job.message = 0.99, "ギャラリーに登録しています"
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", proj["name"])[:40] + "_edit.mp4"
    rec = await asyncio.to_thread(ctx.ledger.add_output, "video", dest, job.tab, job.id, name,
                                  {"project_id": pid, "state": state, "name": proj["name"]},
                                  width=state["width"], height=state["height"], duration_s=duration)
    job.outputs.append(rec["id"])
    ctx.ledger.update_edit_project(pid, output_id=rec["id"])
    log.info("編集 %s: 書き出しました → %s(%.1f 秒)", pid, dest.name, duration)


HANDLERS = {
    "edit.preview": Handler(component=None, normalize=normalize, run=run_preview, cancel=cancel_ffmpeg, gpu=False),
    "edit.export": Handler(component=None, normalize=normalize, run=run_export, cancel=cancel_ffmpeg, gpu=False),
}
