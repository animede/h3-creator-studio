# Ported from Minimax-H3-lipsync-mv (Apache-2.0, Copyright 2026 YUZUKI): app/services/audio.py の quantize_scene_plan、
# app/pipeline.py のシーンごとの seed の決め方、app/services/h3.py の finish_scene(クリップを尺・寸法・コーデックにそろえる ffmpeg)と
# concatenate_and_mux(つないで元の曲に差し替える ffmpeg)。modified: ffmpeg は呼ばず引数を組み立てるだけにした(実行は
# studio/handlers/mv.py)。H3 は長さを自分の格子に丸めるので、短いクリップは最後のコマを複製して足し(tpad)、-frames:v で正確な枚数に切る。
# SPDX-License-Identifier: Apache-2.0
"""MV のクリップ作りの部品: シーン計画のフレーム格子への量子化、シーンごとの seed、ffmpeg の引数(クリップの整形・結合・曲の差し替え)。

ffmpeg は呼ばない。出力先(tmp)は呼び出し側が「<目的のファイル>.part」にして走らせ、成功したら置き換える
(mv_audio._run_ffmpeg の流儀)ので、どの引数にも -f mp4 を付けてある(拡張子が .part でも形式が決まるように)。
"""
from itertools import pairwise
from pathlib import Path
from typing import Optional

FPS = 24                      # H3 の出力もこの fps
SEED_STEP, SEED_MOD = 7919, 2_147_483_647
PAD_MARGIN_S = 0.25           # 足りない分に上乗せする余白(秒)。コンテナの長さの丸めで 1 コマ足りなくならないように
PAD_UNKNOWN_S = 1.0           # H3 の返した長さが分からないときの足し代(H3 の格子は 17 コマ = 0.71 秒おきなので、これで足りる)


def quantize_scene_plan(scenes: list, fps: int = FPS) -> list:
    """シーンの境界をすべて同じコマの格子に載せる(丸めの誤差を積み上げない)。
    各シーンに start_frame / end_frame / frame_count を足し、start / end / duration をコマ数ちょうどの秒に直す。"""
    if not scenes:
        return []
    boundaries = [float(scenes[0]["start"])] + [float(s["end"]) for s in scenes]
    frames = [round(sec * fps) for sec in boundaries]
    frames[0] = 0
    out = []
    for scene, (a, b) in zip(scenes, pairwise(frames), strict=True):
        item = dict(scene)
        item["start_frame"], item["end_frame"], item["frame_count"] = a, b, b - a
        item["start"], item["end"], item["duration"] = round(a / fps, 6), round(b / fps, 6), round((b - a) / fps, 6)
        out.append(item)
    return out


def scene_seed(seed_base: int, index: int) -> int:
    """シーン index(1 始まり)の seed。同じ seed_base なら、毎回同じ並びになる。"""
    return (int(seed_base) + int(index) * SEED_STEP) % SEED_MOD


def pad_seconds(frames: int, raw_s: Optional[float], fps: int = FPS) -> float:
    """H3 のクリップ(raw_s 秒)を frames コマにするために、最後のコマを複製して足す秒数。長すぎても足す(余分は -frames:v が切る)。"""
    if raw_s is None:
        return PAD_UNKNOWN_S
    return max(0.0, frames / fps - float(raw_s)) + PAD_MARGIN_S


AUDIO_RATE = 48000            # 音声を残すクリップ(keep_audio)の標準。どのクリップも同じ設定にして、-c copy でつなげるようにする


def finish_args(exe: str, raw, tmp, frames: int, width: int, height: int, fps: int = FPS, pad_s: float = 0.0, keep_audio: bool = False) -> list:
    """H3 のクリップ raw を、ちょうど frames コマ・width×height・yuv420p の H.264(crf 18)にする。音声は既定では捨てる(-an)。
    pad_s > 0 なら、最後のコマを pad_s 秒ぶん複製して足してから切る(H3 が丸めて短く返したとき)。
    keep_audio なら、H3 の音声を AAC(48kHz ステレオ)で残し、映像と同じ長さ(frames / fps 秒)にそろえる(短ければ無音で足し、長ければ切る)。
    ショート動画の「どちらも無し」(H3 が環境音・効果音を作る)が使う。"""
    vf = f"scale={width}:{height}:flags=lanczos"
    if pad_s > 0:
        vf += f",tpad=stop_mode=clone:stop_duration={pad_s:.3f}"
    argv = [exe, "-y", "-v", "error", "-i", str(raw)]
    if keep_audio:
        length = max(1, int(frames)) / fps
        argv += ["-af", f"apad=whole_dur={length:.3f}", "-t", f"{length:.3f}", "-c:a", "aac", "-b:a", "192k", "-ar", str(AUDIO_RATE), "-ac", "2"]
    else:
        argv += ["-an"]
    return argv + ["-frames:v", str(max(1, int(frames))), "-r", str(fps), "-vf", vf + ",format=yuv420p", "-c:v", "libx264", "-preset", "medium",
                   "-crf", "18", "-f", "mp4", str(tmp)]


def concat_list(paths: list) -> str:
    """ffmpeg の concat demuxer に渡す一覧ファイルの中身。パスの ' は '\\'' にする。"""
    return "".join("file '%s'\n" % Path(p).as_posix().replace("'", "'\\''") for p in paths)


def concat_args(exe: str, listing, tmp, drop_audio: bool = False, faststart: bool = False) -> list:
    """一覧のクリップを、再エンコードせずにつなぐ(クリップはどれも同じ設定で書き出してあるので -c copy でつながる)。
    クリップの音声(ショート動画の「どちらも無し」)はそのまま残す。drop_audio なら映像だけにする。faststart は、これが最終のファイルのとき(先頭にメタデータ)。"""
    argv = [exe, "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy"]
    if drop_audio:
        argv.append("-an")
    if faststart:
        argv += ["-movflags", "+faststart"]
    return argv + ["-f", "mp4", str(tmp)]


def mux_args(exe: str, video, song, tmp, duration: float) -> list:
    """映像(つないだもの)に、元の曲を差し替えて載せる。長さは曲の長さ(duration 秒)まで、音声は AAC 256k、先頭にメタデータ(+faststart)。"""
    return [exe, "-y", "-v", "error", "-i", str(video), "-i", str(song), "-map", "0:v:0", "-map", "1:a:0",
            "-t", f"{duration:.3f}", "-c:v", "copy", "-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart", "-f", "mp4", str(tmp)]


BGM_DB = -14                  # 読み上げの下に敷く曲の音量(dB)
BGM_FADE_S = 2.0              # 曲の終わりのフェードアウト(秒)


def mix_args(exe: str, video, narration, song, tmp, duration: float, song_s: Optional[float] = None) -> list:
    """映像(つないだもの)に、読み上げを主、曲を BGM(-14 dB)にして混ぜて載せる。長さは読み上げの長さ(duration 秒)まで。
    曲は先頭から使い、duration(曲が短ければ song_s)で切って、終わりの 2 秒をフェードアウトする。曲が読み上げより短くても繰り返さない
    (そこで BGM が終わる)。読み上げの音量は変えない(amix の normalize=0)。音声は AAC 256k、先頭にメタデータ(+faststart)。"""
    bgm_s = min(float(duration), float(song_s)) if song_s else float(duration)
    fade_s = min(BGM_FADE_S, bgm_s)
    graph = (f"[1:a]aformat=channel_layouts=stereo[nar];"
             f"[2:a]aformat=channel_layouts=stereo,atrim=duration={bgm_s:.3f},asetpts=PTS-STARTPTS,volume={BGM_DB}dB,"
             f"afade=t=out:st={bgm_s - fade_s:.3f}:d={fade_s:.3f}[bgm];"
             f"[nar][bgm]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]")
    return [exe, "-y", "-v", "error", "-i", str(video), "-i", str(narration), "-i", str(song), "-filter_complex", graph,
            "-map", "0:v:0", "-map", "[a]", "-t", f"{duration:.3f}", "-c:v", "copy", "-c:a", "aac", "-b:a", "256k",
            "-movflags", "+faststart", "-f", "mp4", str(tmp)]
