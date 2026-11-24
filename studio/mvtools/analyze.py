#!/usr/bin/env python
# Ported from Minimax-H3-lipsync-mv (Apache-2.0, Copyright 2026 YUZUKI): app/services/audio.py の
# analyze_boundaries / plan_scenes と docs/vocal-scene-segmentation.md の仕様。アルゴリズム・定数・コスト項は
# そのままで、modified: 単体のスクリプトにした(設定・ファイル書き出しを外し、JSON 1 行の結果にした)。
# 加えた変更(移植元との違い)は plan_scenes の docstring と、has_vocals(歌なしの曲の判定)に書いてある。
# SPDX-License-Identifier: Apache-2.0
"""曲を 5〜10 秒のシーンに分ける(リップシンク MV 用)。

歌詞や音声認識は使わず、分離したボーカル(vocals.wav)と元の曲(song.wav)の音響特徴だけで決める。
ボーカルの音量の谷・ブレス・小節・構成の変化を「境界の候補」として採点し、動的計画法で曲全体を最良の経路でつなぐ。

  python analyze.py <song.wav> <vocals.wav> [--min 5] [--max 10] [--target 8] [--dump analysis.json]

stdout に JSON を 1 行だけ出す(失敗したら理由を stderr の最後の行に書いて終了コード 1):
  {"duration": 60.024, "bpm": 123.05, "has_vocals": true,
   "scenes": [{"index": 1, "start": 0.0, "end": 8.4, "duration": 8.4, "vocal_ratio": 0.0, "kind": "instrumental",
               "boundary_kind": "bar+silence", "boundary_score": 126.0}, ...]}
  vocal_ratio = そのシーンのうちボーカルが鳴っている(RMS が曲中の最大から -35dB 以内の)窓の割合
  kind        = vocal_ratio が 0.35 以上なら "vocal"、未満なら "instrumental"
  boundary_kind / boundary_score = そのシーンの終わりに選ばれた境界の根拠と点数(分析の説明用)
  has_vocals  = 分離したボーカルに歌が入っているか。false(歌のない曲)なら、全シーンが vocal_ratio 0 の "instrumental"
曲の構成(A メロ・サビ)を判別する処理は無いので、シーンに section の項目は付かない。

重い import(numpy / librosa)は analyze_boundaries の中だけで行う。plan_scenes(動的計画法)は標準ライブラリだけで動く。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import traceback
import warnings
from dataclasses import asdict, dataclass
from pathlib import Path

SAMPLE_RATE = 16000
HOP_LENGTH = 1600                    # 100 ms
WINDOW_SECONDS = HOP_LENGTH / SAMPLE_RATE

# 境界候補の点数(docs/vocal-scene-segmentation.md の表そのまま)
SILENCE_DB, SILENCE_SCORE = -38.0, 100.0           # ボーカルの音量の局所最小がこれ以下: silence
VALLEY_DB, VALLEY_SCORE = -28.0, 68.0              #                              vocal_valley
SOFT_VALLEY_DB, SOFT_VALLEY_SCORE = -20.0, 36.0    #                              soft_valley
BREATH_SCORE = 42.0                                # 広帯域・無声のフレーム(ブレス): 補助の手がかり
BAR_SCORE, BEAT_SCORE = 26.0, 10.0                 # 小節頭(4 拍ごと)と拍
SECTION_SCORE = 46.0                               # クロマの変化の上位 10% の局所ピーク
MERGE_TOLERANCE = 0.22                             # 近い候補をまとめる幅(秒)。まとめた時刻は点数で重みを付けた平均
PLAN_MERGE_TOLERANCE = 0.08                        # シーン計画の前にもう一度まとめる幅(秒)。曲の両端は、この幅の内側の候補を吸収して動かない
FALLBACK_STEP = 0.25                               # 5〜10 秒で必ず分けられるよう、点数 0 の候補を 0.25 秒おきに置く

# シーンのコスト(動的計画法): 評価値 += 境界の点数 - |長さ - 目標| * 5 - (7 秒未満の追加) - 18(シーンを増やす費用)
LENGTH_PENALTY = 5.0
COMFORT_SECONDS, SHORT_PENALTY = 7.0, 9.0
SCENE_COST = 18.0

VOCAL_ACTIVE_DB = -35.0              # ボーカルの RMS(曲中の最大を 0dB とした値)がこれを超える窓を「歌っている」とみなす
VOCAL_SCENE_RATIO = 0.35             # 歌っている窓の割合がこれ以上のシーンは歌唱シーン(未満は器楽)

# 歌なしの曲の判定(移植元に無い追加)。移植元の dB は「ボーカル自身の最大」が基準なので、歌の無い曲を分離しても、
# 漏れ込みの小さな音が「歌っている」ことになってしまう(実測 2 例: ACE-Step の歌入りの曲のボーカルは最大 -15dBFS 前後、
# その伴奏だけを分けたときのボーカルは最大 -46dBFS)。
# ボーカルの最大の RMS が絶対値で NO_VOCAL_PEAK_DB 未満、または曲全体の最大より NO_VOCAL_REL_DB 以上小さいなら「歌なし」とする。
NO_VOCAL_PEAK_DB = -40.0
NO_VOCAL_REL_DB = -30.0
EPS = 1e-6


class AnalyzeError(Exception):
    """曲をシーンに分けられない(利用者向けの日本語の理由)。"""


@dataclass
class BoundaryCandidate:
    time: float
    score: float
    kind: str
    details: dict


def _local_minima(values, radius: int = 2):
    import numpy as np
    for index in range(radius, len(values) - radius):
        window = values[index - radius:index + radius + 1]
        if values[index] <= np.min(window):
            yield index


def _merge_candidates(candidates: list, tolerance: float = MERGE_TOLERANCE) -> list:
    if not candidates:
        return []
    merged: list = []
    for candidate in sorted(candidates, key=lambda item: item.time):
        if merged and candidate.time - merged[-1].time <= tolerance:
            previous = merged[-1]
            weight = max(previous.score, 1.0) + max(candidate.score, 1.0)
            previous.time = round(
                (previous.time * max(previous.score, 1.0) + candidate.time * max(candidate.score, 1.0))
                / weight, 3
            )
            previous.score += candidate.score
            kinds = set(previous.kind.split("+")) | set(candidate.kind.split("+"))
            previous.kind = "+".join(sorted(kinds))
            previous.details.update(candidate.details)
        else:
            merged.append(candidate)
    return merged


def analyze_boundaries(song: Path, vocals: Path) -> dict:
    """ボーカルと原曲から、説明のつく境界の候補を取り出す(移植元の analyze_boundaries。boundary_analysis.json の書き出しだけ外した)。"""
    import librosa
    import numpy as np
    sample_rate = SAMPLE_RATE
    hop_length = HOP_LENGTH
    vocal_wave, _ = librosa.load(str(vocals), sr=sample_rate, mono=True)
    song_wave, _ = librosa.load(str(song), sr=sample_rate, mono=True)
    duration = len(song_wave) / sample_rate

    rms = librosa.feature.rms(y=vocal_wave, frame_length=2048, hop_length=hop_length)[0]
    rms_db = librosa.amplitude_to_db(np.maximum(rms, 1e-8), ref=np.max)
    flatness = librosa.feature.spectral_flatness(
        y=vocal_wave, n_fft=2048, hop_length=hop_length
    )[0]
    zcr = librosa.feature.zero_crossing_rate(
        vocal_wave, frame_length=2048, hop_length=hop_length
    )[0]
    times = librosa.frames_to_time(np.arange(len(rms)), sr=sample_rate, hop_length=hop_length)

    mix_rms = librosa.feature.rms(y=song_wave, frame_length=2048, hop_length=hop_length)[0]
    vocal_peak_db, mix_peak_db = _peak_db(rms), _peak_db(mix_rms)
    has_voice = has_vocals(vocal_peak_db, mix_peak_db)

    candidates: list = []
    # 歌のない曲では、ボーカルの音量から作る候補(谷・ブレス)は漏れ込みの揺らぎでしかないので使わない(拍・小節・構成の変化だけにする)
    for index in (_local_minima(rms_db, radius=2) if has_voice else ()):
        second = float(times[index])
        if not 0.5 < second < duration - 0.5:
            continue
        db = float(rms_db[index])
        if db <= SILENCE_DB:
            score, kind = SILENCE_SCORE, "silence"
        elif db <= VALLEY_DB:
            score, kind = VALLEY_SCORE, "vocal_valley"
        elif db <= SOFT_VALLEY_DB:
            score, kind = SOFT_VALLEY_SCORE, "soft_valley"
        else:
            continue
        candidates.append(BoundaryCandidate(second, score, kind, {"vocal_db": round(db, 2)}))

    # ブレスは広帯域で無声。補助の手がかりであって、これだけで境界を決めることはない
    flat_threshold = float(np.quantile(flatness, 0.72)) if len(flatness) else 1.0
    for index in (range(1, min(len(rms_db), len(flatness), len(zcr)) - 1) if has_voice else ()):
        db = float(rms_db[index])
        if -38 < db < -15 and flatness[index] >= flat_threshold and zcr[index] >= 0.08:
            second = float(times[index])
            candidates.append(BoundaryCandidate(
                second, BREATH_SCORE, "breath",
                {"vocal_db": round(db, 2), "flatness": round(float(flatness[index]), 3)},
            ))

    tempo, beat_frames = librosa.beat.beat_track(
        y=song_wave, sr=sample_rate, hop_length=hop_length, units="frames"
    )
    beat_times = librosa.frames_to_time(beat_frames, sr=sample_rate, hop_length=hop_length)
    for beat_index, second in enumerate(beat_times):
        if 0.5 < second < duration - 0.5:
            is_bar = beat_index % 4 == 0
            candidates.append(BoundaryCandidate(
                float(second), BAR_SCORE if is_bar else BEAT_SCORE,
                "bar" if is_bar else "beat", {},
            ))

    # クロマの変化から見た構成の変わり目。ピークは候補であって、A メロ・サビのラベルではない
    chroma = librosa.feature.chroma_cqt(y=song_wave, sr=sample_rate, hop_length=hop_length)
    if chroma.shape[1] > 2:
        novelty = np.linalg.norm(np.diff(chroma, axis=1), axis=0)
        threshold = float(np.quantile(novelty, 0.9))
        for index in _local_minima(-novelty, radius=3):
            if novelty[index] >= threshold:
                second = float(times[min(index + 1, len(times) - 1)])
                candidates.append(BoundaryCandidate(
                    second, SECTION_SCORE, "section_change", {"novelty": round(float(novelty[index]), 3)}
                ))

    # 5〜10 秒の経路が必ず作れるようにする細かい格子。点数は 0 なので、自然な候補と競合したら選ばれない
    for second in np.arange(FALLBACK_STEP, duration, FALLBACK_STEP):
        candidates.append(BoundaryCandidate(float(second), 0.0, "fallback", {}))

    merged = _merge_candidates(candidates)
    return {
        "duration": round(duration, 3),
        "tempo": round(float(np.asarray(tempo).reshape(-1)[0]), 2),
        "window_seconds": hop_length / sample_rate,
        "rms_db": [round(float(value), 2) for value in rms_db],
        "vocal_peak_db": round(vocal_peak_db, 1),
        "mix_peak_db": round(mix_peak_db, 1),
        "has_vocals": has_voice,
        "candidates": [asdict(item) for item in merged],
    }


def _peak_db(rms) -> float:
    """100ms ごとの RMS の最大(絶対値の dBFS)。"""
    import numpy as np
    return float(20 * np.log10(max(float(np.max(rms)), 1e-8))) if len(rms) else -160.0


def has_vocals(vocal_peak_db: float, mix_peak_db: float) -> bool:
    """分離したボーカルに歌が入っているか(NO_VOCAL_PEAK_DB / NO_VOCAL_REL_DB の説明を参照)。"""
    return vocal_peak_db >= NO_VOCAL_PEAK_DB and vocal_peak_db - mix_peak_db >= NO_VOCAL_REL_DB


def _boundary_penalty(duration: float, target: float) -> float:
    # 7〜10 秒が無理のない長さ。5 秒でも許されるが、わざと高くつく
    penalty = abs(duration - target) * LENGTH_PENALTY
    if duration < COMFORT_SECONDS:
        penalty += (COMFORT_SECONDS - duration) * SHORT_PENALTY
    return penalty


def _vocal_ratio(rms_db: list, window: float, start: float, end: float, has_voice: bool = True) -> float:
    if not has_voice:
        return 0.0
    lo = max(0, int(start / window))
    hi = min(len(rms_db), max(lo + 1, math.ceil(end / window)))
    if hi <= lo:
        return 0.0
    return sum(float(value) > VOCAL_ACTIVE_DB for value in rms_db[lo:hi]) / (hi - lo)


def _scene(index: int, start: float, end: float, ratio: float, kind: str, score: float) -> dict:
    return {
        "index": index,
        "start": round(start, 3),
        "end": round(end, 3),
        "duration": round(end - start, 3),
        "vocal_ratio": round(ratio, 3),
        "kind": "vocal" if ratio >= VOCAL_SCENE_RATIO else "instrumental",
        "boundary_kind": kind,
        "boundary_score": round(score, 2),
    }


def plan_scenes(analysis: dict, *, min_seconds: float = 5.0, target_seconds: float = 9.0,
                max_seconds: float = 10.0) -> list:
    """境界の候補から、曲全体を [min, max] 秒のシーンに分ける最良の経路を動的計画法で求める。

    移植元との違い(アルゴリズム・定数・コスト項は同じ):
      - 曲の両端(0 秒と曲末)は動かさない。移植元は両端も近接候補と同じ回に混ぜるので、曲末から 0.08 秒以内に候補が
        あると曲末の時刻がずれ、シーンが [0, 曲の長さ] をちょうど覆わなくなる。ここでは端に近い候補は端に吸収する。
      - 時刻を 1 ミリ秒に丸めてから計画する(報告するシーンの長さが、動的計画法で確かめた長さと食い違わないように)。
      - 動的計画法の内側のループは、シーンが max を超えた時点で打ち切る(結果は同じで、速いだけ)。
      - 経路が見つからないとき(候補の併合で時刻がずれて、曲の長さが 10〜10.5 秒のような狭い範囲に入ったときなど)は、
        移植元ではエラーだが、均等割り(boundary_kind = even_split)に切り替える。均等割りでも範囲に入らなければエラー。
      - シーンに kind(vocal / instrumental)を付ける。歌なしの曲(analysis["has_vocals"] が false)は全シーンの vocal_ratio を 0 にする。
    """
    total = round(float(analysis["duration"]), 3)
    rms_db = analysis.get("rms_db", [])
    window = float(analysis.get("window_seconds", WINDOW_SECONDS))
    voiced = bool(analysis.get("has_vocals", True))
    if total <= min_seconds:
        return [_scene(1, 0.0, total, _vocal_ratio(rms_db, window, 0.0, total, voiced), "song_end", 0.0)]

    inner = [BoundaryCandidate(round(float(item["time"]), 3), float(item.get("score", 0.0)), str(item.get("kind", "")),
                               dict(item.get("details") or {}))
             for item in analysis.get("candidates", []) if 0 < float(item["time"]) < total]
    inner = [c for c in _merge_candidates(inner, tolerance=PLAN_MERGE_TOLERANCE)
             if PLAN_MERGE_TOLERANCE < c.time < total - PLAN_MERGE_TOLERANCE]
    points = [BoundaryCandidate(0.0, 0.0, "song_start", {})] + inner + [BoundaryCandidate(total, 0.0, "song_end", {})]

    best = [-math.inf] * len(points)
    previous = [-1] * len(points)
    best[0] = 0.0
    for end_index in range(1, len(points)):
        for start_index in range(end_index - 1, -1, -1):
            duration = points[end_index].time - points[start_index].time
            if duration > max_seconds + EPS:
                break
            if duration < min_seconds - EPS:
                continue
            value = best[start_index]
            if not math.isfinite(value):
                continue
            boundary_reward = 0.0 if end_index == len(points) - 1 else points[end_index].score
            value += boundary_reward - _boundary_penalty(duration, target_seconds) - SCENE_COST
            if value > best[end_index]:
                best[end_index] = value
                previous[end_index] = start_index

    if previous[-1] < 0:
        return _even_split(total, rms_db, window, voiced, min_seconds, max_seconds)

    path: list = []
    cursor = len(points) - 1
    while cursor >= 0:
        path.append(cursor)
        if cursor == 0:
            break
        cursor = previous[cursor]
    path.reverse()

    return [
        _scene(index, points[left].time, points[right].time,
               _vocal_ratio(rms_db, window, points[left].time, points[right].time, voiced),
               points[right].kind, points[right].score)
        for index, (left, right) in enumerate(zip(path, path[1:]), start=1)
    ]


def _even_split(total: float, rms_db: list, window: float, voiced: bool, min_seconds: float, max_seconds: float) -> list:
    count = max(1, math.ceil(total / max_seconds - 1e-9))
    if total / count < min_seconds - EPS:
        raise AnalyzeError(f"{min_seconds:g}〜{max_seconds:g} 秒の範囲で曲全体({total:g} 秒)を分割できませんでした")
    edges = [round(total * i / count, 3) for i in range(count)] + [total]
    return [_scene(i, a, b, _vocal_ratio(rms_db, window, a, b, voiced), "song_end" if i == count else "even_split", 0.0)
            for i, (a, b) in enumerate(zip(edges, edges[1:]), start=1)]


def analyze(song: Path, vocals: Path, min_s: float = 5.0, max_s: float = 10.0, target_s: float = 8.0,
            dump: str = "") -> dict:
    for label, path in (("曲", song), ("ボーカル", vocals)):
        if not Path(path).is_file():
            raise AnalyzeError(f"{label}の音声がありません: {path}")
    if not 0 < min_s <= max_s:
        raise AnalyzeError(f"シーンの長さの範囲が正しくありません: --min {min_s:g} --max {max_s:g}")
    try:
        analysis = analyze_boundaries(Path(song), Path(vocals))
    except ImportError as e:
        raise AnalyzeError(f"必要なパッケージがありません({e.name}): この Python には librosa が入っていません。"
                           "setup --mv を実行してください") from e
    if dump:
        Path(dump).write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")
    scenes = plan_scenes(analysis, min_seconds=min_s, target_seconds=target_s, max_seconds=max_s)
    return {"duration": analysis["duration"], "bpm": analysis["tempo"], "has_vocals": analysis["has_vocals"], "scenes": scenes}


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    p = argparse.ArgumentParser(description="曲を 5〜10 秒のシーンに分ける(結果の JSON を stdout に 1 行)")
    p.add_argument("song", help="曲の音声(44.1kHz ステレオの WAV を想定)")
    p.add_argument("vocals", help="分離したボーカルの音声(separate.py の vocals.wav)")
    p.add_argument("--min", dest="min_s", type=float, default=5.0, help="シーンの最短の秒数(既定 5)")
    p.add_argument("--max", dest="max_s", type=float, default=10.0, help="シーンの最長の秒数(既定 10)")
    p.add_argument("--target", dest="target_s", type=float, default=8.0, help="シーンの目標の秒数(既定 8)")
    p.add_argument("--dump", default="", help="境界の候補とボーカルの音量の列をこのファイルに JSON で書く(確認用)")
    args = p.parse_args(argv)
    warnings.filterwarnings("ignore")
    real_stdout, sys.stdout = sys.stdout, sys.stderr         # ライブラリの print が結果の JSON 行に混ざらないように、作業中は stdout を stderr へ向ける
    try:
        result = analyze(Path(args.song), Path(args.vocals), args.min_s, args.max_s, args.target_s, args.dump)
    except AnalyzeError as e:
        print(f"エラー: {e}", file=sys.stderr, flush=True)
        return 1
    except KeyboardInterrupt:
        print("エラー: 中断されました", file=sys.stderr, flush=True)
        return 130
    except Exception as e:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        print(f"エラー: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        return 1
    finally:
        sys.stdout = real_stdout
    print(json.dumps(result, ensure_ascii=True), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
