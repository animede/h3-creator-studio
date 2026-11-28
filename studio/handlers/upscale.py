"""ギャラリーの生成物(動画・画像)を 1 つ直接 AI で拡大するジョブ: upscale.output(v1.3.0。CPU のレーンの仕事で、エンジンは使わない)。

POST /api/gallery/{id}/upscale(main.py)が plan() で検証し(方式・倍率・長辺の上限は studio/upscale.py の resolve。使えなければ 400)、
このジョブを入れる。/api/jobs からは入れられない(main.py の PUBLIC_JOB_PREFIXES に入れない)。
  動画: outputs/video/<元の名前>_x<倍率>.mp4(アップロード outputs/uploads/<uuid>.mp4 の拡大も outputs/video/ に置く)。作業は outputs/upscale/<ジョブの ID>
  画像: outputs/image/<元の名前>_x<倍率>.png
どちらも前のものを上書きしない(_2、_3…)。結果はギャラリーに新しい生成物として載せ、params.upscale に方式・倍率・元の生成物の ID を残す。
params は {"output_id", "kind": "video" / "image", "method"(resolve 済みの方式), "scale", "width", "height"(拡大後), "fps"}。
走っている(順番待ちの)間は、params の output_id で元の生成物が削除から守られる(main.py の _in_use がジョブの params の文字列を見る)。
"""
import asyncio
import functools
import logging
import shutil
from pathlib import Path

from .. import media, mv_audio, upscale
from ..jobs import Ctx, Handler, Job, JobCancelled, JobFailed

log = logging.getLogger("studio.upscale")

KIND = "upscale.output"
KINDS = ("video", "image")
FPS_DEFAULT = 24.0
MSG_REGISTER = "ギャラリーに登録しています"
MSG_NOT_FOUND = "その生成物は見つかりません(ギャラリーから削除されたかもしれません)"
MSG_ONLY_MEDIA = "拡大できるのは動画と画像だけです"
MSG_SCALE = "拡大の倍率は 2 / 3 / 4 のいずれかです"
MSG_NO_FILE = "生成物のファイルが見つかりません(ギャラリーから削除されたかもしれません)"
MSG_NO_SIZE = "生成物の大きさ(幅・高さ)が分かりません"
MP4_AUDIO_OK = (".mp4", ".m4v")      # この入れ物の音声は MP4 にそのまま写せる。ほか(webm の Vorbis・avi の PCM など)は AAC にし直す
# 走っている道具・ffmpeg(ジョブの ID -> Popen)。cancel がここから子プロセスを止める
_procs: dict = {}
# 走っているジョブが書くことに決めた出力の場所(CPU のレーンは 2 つ同時に走るので、同じ生成物を 2 回拡大しても同じ名前にしない)
_taken: set = set()


def _size(value) -> int:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        raise ValueError("拡大後の幅・高さを正の整数で指定してください")
    return n


def plan(ledger, rec: dict, method=upscale.AUTO, scale=None, settings=None, st: dict = None) -> dict:
    """API の指定(方式・倍率)を検証し、ジョブの params にする。不正・使えない・大きすぎれば ValueError(日本語。API は 400 にする)。
    方式の使えるか・倍率・長辺の上限は upscale.resolve。寸法(とコマ率)は台帳の値で、無ければ動画は ffprobe で読む。止まる処理(nvidia-smi・ffprobe)。"""
    kind = rec.get("kind")
    if kind not in KINDS:
        raise ValueError(MSG_ONLY_MEDIA)
    src = ledger.abs_path(rec["path"])
    if not src.is_file():
        raise ValueError(MSG_NO_FILE)
    width, height, fps = rec.get("width"), rec.get("height"), rec.get("fps")
    if kind == "video" and (not width or not height or not fps):
        info = media.probe(src)          # ffprobe が無かった時に取り込んだ動画は、台帳に寸法が無い
        width, height, fps = width or info.get("width"), height or info.get("height"), fps or info.get("fps")
    if not width or not height:
        raise ValueError(MSG_NO_SIZE)
    method = upscale.AUTO if method in (None, "") else method
    resolved = upscale.resolve(settings, method, scale, int(width), int(height), upscale.max_edge(settings), st=st, kind=kind)
    if resolved is None:                 # 倍率 1(拡大しない)・倍率なし。ここでは拡大だけをするので、倍率の誤り
        raise ValueError(MSG_SCALE)
    try:
        fps = float(fps) if kind == "video" and fps else None
    except (TypeError, ValueError):
        fps = None
    return {"output_id": rec["id"], "kind": kind, **resolved, "fps": fps if fps and fps > 0 else (FPS_DEFAULT if kind == "video" else None)}


def normalize(kind: str, params: dict, ledger=None) -> dict:
    """ジョブの params の形を確かめる(使えるかの検証は設定の要る plan で済んでいる)。不正なら ValueError(日本語)。"""
    oid = str(params.get("output_id") or "").strip()
    rec = ledger.get_output(oid) if ledger is not None and oid else None
    if not oid or (ledger is not None and rec is None):
        raise ValueError(MSG_NOT_FOUND)
    media_kind = str(params.get("kind") or "").strip()
    if media_kind not in KINDS or (rec is not None and rec["kind"] != media_kind):
        raise ValueError(MSG_ONLY_MEDIA)
    # 方式・倍率の形は編集の書き出しと同じ検証(寸法は plan が resolve で出した拡大後の値を、そのまま受ける)
    up = upscale.check_params({"method": params.get("method"), "scale": params.get("scale")}, {"width": 0, "height": 0})
    out = {"output_id": oid, "kind": media_kind, "method": up["method"], "scale": up["scale"],
           "width": _size(params.get("width")), "height": _size(params.get("height")), "fps": None}
    if media_kind == "video":
        try:
            fps = float(params.get("fps") or FPS_DEFAULT)
        except (TypeError, ValueError):
            fps = FPS_DEFAULT
        out["fps"] = fps if 0 < fps < 1000 else FPS_DEFAULT          # 読めない・おかしなコマ率は 24 にする(拡大は止めない)
    return out


# ---- 実行 ----

async def cancel(job: Job, ctx: Ctx) -> None:
    """走っている道具・ffmpeg を止める(upscale._run が、止まったことを見て JobCancelled にする)。"""
    proc = _procs.get(job.id)
    if proc is not None and proc.poll() is None:
        proc.kill()


async def _in_thread(job: Job, fn) -> None:
    """止まる処理 fn()(_procs に子プロセスを登録する)を別スレッドで走らせて待つ。待っている側が取り消された(管理役の停止)ら、子プロセスも止める。"""
    try:
        await asyncio.to_thread(fn)
    except asyncio.CancelledError:
        proc = _procs.get(job.id)
        if proc is not None:
            proc.kill()
        raise


def work_dir(outputs_dir, job_id: str) -> Path:
    return Path(outputs_dir) / upscale.WORK_DIRNAME / job_id


def _runner(job: Job, kind: str, method: str, src: Path, dst: Path, scale: int, fps, work: Path):
    """方式の実行(止まる処理)を、引数を束ねた関数にする。道具の場所は設定(studio.config.settings)から。"""
    if kind == "image":
        def run_image() -> Path:
            inp = upscale.prepare_image_input(src, work)          # GIF・BMP や向きの付いた JPEG は、向きを直した PNG にしてから
            if method == "realesrgan":
                return upscale.run_realesrgan_image(job, upscale.realesrgan_exe(), inp, dst, scale, _procs)
            return upscale.run_rtxvsr_image(job, upscale.rtxvsr_python(), inp, dst, scale, _procs, upscale.rtxvsr_quality())
        return run_image
    try:
        ffmpeg = mv_audio._ffmpeg()
    except mv_audio.MvAudioError as e:
        raise JobFailed(e.message, e.log_tail) from e
    audio = upscale.AUDIO_COPY if src.suffix.lower() in MP4_AUDIO_OK else upscale.AUDIO_AAC
    if method == "realesrgan":
        return functools.partial(upscale.run_realesrgan, job, upscale.realesrgan_exe(), src, dst, scale, fps, work, ffmpeg, _procs, audio=audio)
    return functools.partial(upscale.run_rtxvsr, job, upscale.rtxvsr_python(), src, dst, scale, fps, work, ffmpeg, _procs, upscale.rtxvsr_quality(),
                             audio=audio)


def _filename(rec: dict, src: Path, scale: int, ext: str) -> str:
    stem = Path(str(rec.get("filename") or "")).stem or src.stem
    return f"{stem}_x{scale}.{ext}"


async def run(job: Job, ctx: Ctx) -> None:
    if job.cancel_requested:
        raise JobCancelled()
    p = job.params
    kind, method, scale = p["kind"], p["method"], int(p["scale"])
    rec = ctx.ledger.get_output(p["output_id"])
    if rec is None:
        raise JobFailed(MSG_NOT_FOUND)
    src = ctx.ledger.abs_path(rec["path"])
    if not src.is_file():
        raise JobFailed(MSG_NO_FILE)
    await upscale.wait_gpu_idle(job, ctx)
    ext = "mp4" if kind == "video" else "png"
    dst = upscale.upscaled_path(Path(ctx.outputs_dir) / kind / f"{src.stem}.{ext}", scale, _taken)     # 名前は走り出す時に決める(待つ間に増えるので)
    _taken.add(dst)
    work, done = work_dir(ctx.outputs_dir, job.id), False
    try:
        await _in_thread(job, _runner(job, kind, method, src, dst, scale, p.get("fps"), work))
        if job.cancel_requested:
            raise JobCancelled()
        job.progress, job.message = 0.99, MSG_REGISTER
        params = {"upscale": {"method": method, "scale": scale, "source_output_id": rec["id"]}, "source_filename": rec.get("filename")}
        rec2 = await asyncio.to_thread(ctx.ledger.add_output, kind, dst, job.tab, job.id, _filename(rec, src, scale, ext), params,
                                       width=p["width"], height=p["height"], duration_s=rec.get("duration_s") if kind == "video" else None,
                                       fps=p.get("fps") if kind == "video" else None)
        done = True
    finally:
        _taken.discard(dst)
        if not done:
            dst.unlink(missing_ok=True)          # 中止・失敗・登録の失敗で、ギャラリーに無いファイルを残さない
        await asyncio.to_thread(shutil.rmtree, work, True)
    job.outputs.append(rec2["id"])
    log.info("ギャラリーの拡大: %s を %s ×%d で拡大しました → %s(%d×%d)", rec.get("filename"), upscale.METHODS[method]["ja"], scale, dst.name,
             p["width"], p["height"])


def record(params: dict) -> dict:
    """台帳(jobs 表)に残す params: 元の生成物・方式・倍率だけ。"""
    return {"output_id": params.get("output_id"), "method": params.get("method"), "scale": params.get("scale")}


HANDLERS = {
    KIND: Handler(component=None, normalize=normalize, run=run, cancel=cancel, gpu=False, record=record),
}
