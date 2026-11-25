"""編集タブの部品(第 4 版): 編集プロジェクトの状態(state)の検証・時間軸・分割/削除/挿入と、ffmpeg の引数(filter_complex)の組み立て。

純粋な関数だけで、ファイルも ffmpeg も触らない(生成物の長さ・寸法は呼び出し側が sources で渡す)。ffmpeg の実行は studio/handlers/edit.py。
状態(docs/SPEC.md 第 4 版 §1):
    {"clips": [{"id", "kind": "video" | "image", "in", "out", "duration"}], "transitions": [{"after", "type", "duration"}],
     "fade_in", "fade_out", "effects": [{"type", "value", "start", "end"}], "audio": {"mode", "song_id", "song_db"}, "width", "height", "fps"}
時間の指定(効果の区間・分割・削除・挿入)は、すべて「クリップをつないだだけの時間軸」(plain。クロスフェードで縮む分を含めない)。
書き出される動画は境目のクロスフェードの分だけ短くなる(export)。効果の区間は、書き出しのときに plain → export に直して ffmpeg に渡す。
"""
import copy
import hashlib
import json
import math
from typing import Optional

from .mvclip import BGM_DB, BGM_FADE_S

CLIP_KINDS = ("video", "image")
TRANSITION_TYPES = ("cut", "crossfade", "fadeblack")
# 値の無い「よく使う見た目」(フィルタは固定。区間にも全体にも効く)
LOOKS = {
    "sepia": "colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131:0",     # セピア
    "warm": "colorbalance=rs=.15:gs=.02:bs=-.15:rm=.1:bm=-.1",                           # 暖色
    "cool": "colorbalance=rs=-.15:gs=.0:bs=.15:rm=-.1:bm=.1",                             # 寒色
    "vintage": "curves=preset=vintage",                                                   # フィルム風(退色)
    "vignette": "vignette=angle=PI/4",                                                    # 周辺を暗く
    "negate": "negate",                                                                   # 色を反転
    "blur": "gblur=sigma=6",                                                              # ぼかし
}
# 選択区間の効果(区間が必須。フェード・黒・白と、静止画に効くズーム)
RANGE_EFFECTS = ("fade_to_black", "fade_from_black", "black", "fade_to_white", "fade_from_white", "zoom_in", "zoom_out",
                 "pan_right", "pan_left", "pan_down", "pan_up", "zoom_in_right", "zoom_in_left")
PANS = ("pan_right", "pan_left", "pan_down", "pan_up", "zoom_in_right", "zoom_in_left")      # 少し拡大して、切り出す位置を動かす(静止画のケン・バーンズ)
EFFECT_TYPES = ("mono", "saturation", "contrast", "brightness", *RANGE_EFFECTS, *LOOKS)
AUDIO_MODES = ("original", "music", "mix", "none")
EFFECT_RANGE = {"saturation": (0.0, 3.0, 1.0), "contrast": (0.0, 3.0, 1.0), "brightness": (-1.0, 1.0, 0.0),   # (最小, 最大, 既定 = そのまま)
                "zoom_in": (1.05, 2.0, 1.3), "zoom_out": (1.05, 2.0, 1.3),                                        # ズームの倍率
                "pan_right": (1.05, 2.0, 1.2), "pan_left": (1.05, 2.0, 1.2), "pan_down": (1.05, 2.0, 1.2), "pan_up": (1.05, 2.0, 1.2),
                "zoom_in_right": (1.05, 2.0, 1.3), "zoom_in_left": (1.05, 2.0, 1.3)}                              # パンの拡大(動かす余白を作る)
IMAGE_DEFAULT_S, IMAGE_MAX_S = 3.0, 600.0
MIN_CLIP_S = 0.1                 # クリップの最短(これより短い切れ端は作らない)
TRANSITION_MIN_S, TRANSITION_MAX_S = 0.5, 2.0
SONG_DB_MIN, SONG_DB_MAX = -60.0, 0.0
DEFAULT_SIZE, DEFAULT_FPS = (1024, 576), 24
PREVIEW_SHORT_EDGE = 480
AUDIO_RATE = 48000
EPS = 1e-3                       # 時刻・長さの比べの許容(秒)
AFMT = f"aformat=sample_fmts=fltp:sample_rates={AUDIO_RATE}:channel_layouts=stereo"    # どのクリップの音声も同じ形にして、concat / acrossfade がつなげるようにする


# ---- 小さな道具 ----

def _num(value, name: str, default: Optional[float] = None) -> float:
    if value is None or value == "":
        if default is None:
            raise ValueError(f"{name}を数字で指定してください")
        return default
    try:
        x = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name}は数字で指定してください") from None
    if not math.isfinite(x):
        raise ValueError(f"{name}は数字で指定してください")
    return x


def _r3(x: float) -> float:
    return round(float(x), 3)


def _n(x: float) -> str:
    """ffmpeg の引数に出す数(小数 3 桁、末尾の 0 は省く)。"""
    s = f"{float(x):.3f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def _even(x: float) -> int:
    return max(2, int(x) - int(x) % 2)


def clip_length(clip: dict) -> float:
    """クリップの長さ(秒)。out - in(画像は in = 0、out = 秒数)。"""
    if clip.get("out") is not None and clip.get("in") is not None:
        return float(clip["out"]) - float(clip["in"])
    return float(clip.get("duration") or 0.0)


def state_hash(state: dict) -> str:
    """状態の中身の指紋(プレビューが、いまの状態で作ったものか = stale でないかの判定に使う)。"""
    return hashlib.sha1(json.dumps(state, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]


def used_ids(state: dict) -> list:
    """状態が使っている生成物の ID(クリップと曲。重複なし、使う順)。"""
    clips, audio = state.get("clips"), state.get("audio")
    ids = [c.get("id") for c in clips if isinstance(c, dict)] if isinstance(clips, list) else []
    ids.append(audio.get("song_id") if isinstance(audio, dict) else None)
    return list(dict.fromkeys(i for i in ids if i and isinstance(i, str)))


def empty_state() -> dict:
    return {"clips": [], "transitions": [], "fade_in": 0.0, "fade_out": 0.0, "effects": [],
            "audio": {"mode": "original", "song_id": None, "song_db": BGM_DB},
            "width": DEFAULT_SIZE[0], "height": DEFAULT_SIZE[1], "fps": DEFAULT_FPS}


# ---- 時間軸 ----

def _transitions(state: dict) -> dict:
    """{after: (type, 秒)}。cut は含めない。"""
    return {int(t["after"]): (t["type"], float(t["duration"])) for t in state.get("transitions") or [] if t.get("type") != "cut"}


def _overlaps(state: dict) -> list:
    """境目ごとの縮み(クロスフェード・黒へのフェードの秒数。cut は 0)。長さはクリップ数 - 1。"""
    tr = _transitions(state)
    return [tr[j][1] if j in tr else 0.0 for j in range(max(0, len(state.get("clips") or []) - 1))]


def timeline(state: dict) -> list:
    """クリップをつないだだけの時間軸(plain)での各クリップの [start, end]。効果の区間の指定に使う。"""
    out, pos = [], 0.0
    for i, c in enumerate(state.get("clips") or []):
        length = clip_length(c)
        out.append({"index": i, "start": _r3(pos), "end": _r3(pos + length)})
        pos += length
    return out


def plain_duration(state: dict) -> float:
    return _r3(sum(clip_length(c) for c in state.get("clips") or []))


def export_duration(state: dict) -> float:
    """書き出される動画の長さ(plain の長さから、境目のクロスフェードで重なる分を引いたもの)。"""
    return _r3(sum(clip_length(c) for c in state.get("clips") or []) - sum(_overlaps(state)))


def export_timeline(state: dict) -> list:
    """書き出される動画の時間軸(export)での各クリップの [start, end](クロスフェードで次のクリップと重なる)。"""
    over, out, pos = _overlaps(state), [], 0.0
    for i, c in enumerate(state.get("clips") or []):
        length = clip_length(c)
        out.append({"index": i, "start": _r3(pos), "end": _r3(pos + length)})
        pos += length - (over[i] if i < len(over) else 0.0)
    return out


def source_at(state: dict, t: float) -> Optional[dict]:
    """書き出し(export)の時刻 t に映っているクリップと、その元の動画の中の秒: {"index", "clip", "source_s"}。クリップが無ければ None。
    クロスフェードで重なる時間は、後ろのクリップとして数える(プレビューの再生位置から静止画を抜く用)。"""
    tl = export_timeline(state)
    if not tl:
        return None
    t = min(max(float(t), 0.0), tl[-1]["end"])
    k = max(it["index"] for it in tl if it["start"] <= t + EPS)
    clip = state["clips"][k]
    return {"index": k, "clip": clip, "source_s": _r3(float(clip["in"]) + max(0.0, t - tl[k]["start"]))}


def plain_to_export(state: dict, t: float, side: str = "start") -> float:
    """plain の時刻 t を export の時刻にする(その時刻より前の境目の縮みを引く)。境目ちょうどの時刻は、側によって属するクリップが違う:
    side="start" は後ろのクリップ(区間の始まり)、"end" は前のクリップ(区間の終わり)として数える。"""
    over, tl = _overlaps(state), timeline(state)
    if not tl:
        return float(t)
    t = min(max(float(t), 0.0), tl[-1]["end"])
    if side == "end":
        k = next((it["index"] for it in tl if it["end"] >= t - EPS), len(tl) - 1)
    else:
        k = max(it["index"] for it in tl if it["start"] <= t + EPS)
    return t - sum(over[:k])


# ---- 検証 ----

def derive_format(clips: list, sources: dict) -> tuple:
    """書き出しの寸法と fps: 先頭の動画のもの(画像だけなら 1024×576 / 24)。寸法は偶数にする。fps は sources に無ければ 24。"""
    for c in clips:
        src = sources.get(c["id"]) or {}
        if c["kind"] == "video" and src.get("width") and src.get("height"):
            return _even(src["width"]), _even(src["height"]), int(round(float(src.get("fps") or DEFAULT_FPS)))
    return DEFAULT_SIZE[0], DEFAULT_SIZE[1], DEFAULT_FPS


def _check_clip(i: int, raw: dict, sources: dict) -> dict:
    no = f"クリップ {i + 1}"
    if not isinstance(raw, dict):
        raise ValueError(f"{no}の指定が正しくありません")
    oid = str(raw.get("id") or "").strip()
    src = sources.get(oid)
    if not oid or src is None:
        raise ValueError(f"{no}の生成物が見つかりません(ギャラリーから削除されたかもしれません)")
    if src["kind"] not in CLIP_KINDS:
        raise ValueError(f"{no}には動画か画像を使ってください(曲は音声の欄に入れます)")
    if src["kind"] == "image":
        seconds = _num(raw.get("duration"), f"{no}の秒数", None) if raw.get("duration") not in (None, "") else (
            _num(raw.get("out"), f"{no}の終了", IMAGE_DEFAULT_S) - _num(raw.get("in"), f"{no}の開始", 0.0) if raw.get("out") not in (None, "") else IMAGE_DEFAULT_S)
        if not MIN_CLIP_S - EPS <= seconds <= IMAGE_MAX_S:          # 最短は動画と同じ許容(分割・削除が残す切れ端は MIN_CLIP_S - EPS まで)
            raise ValueError(f"{no}(画像)の秒数は {MIN_CLIP_S:g}〜{IMAGE_MAX_S:g} 秒で指定してください")
        return {"id": oid, "kind": "image", "in": 0.0, "out": _r3(seconds), "duration": _r3(seconds)}
    length = src.get("length_s")
    start, end = _num(raw.get("in"), f"{no}の開始", 0.0), _num(raw.get("out"), f"{no}の終了", length if length else None)
    if start < 0:
        raise ValueError(f"{no}の開始は 0 秒以上にしてください")
    if end <= start:
        raise ValueError(f"{no}の終了は開始より後にしてください")
    if length and end > length + 0.05:
        raise ValueError(f"{no}の終了が元の動画の長さ({length:.2f} 秒)を超えています")
    if length:
        end = min(end, float(length))
    if end - start < MIN_CLIP_S - EPS:
        raise ValueError(f"{no}は短すぎます({MIN_CLIP_S:g} 秒以上にしてください)")
    return {"id": oid, "kind": "video", "in": _r3(start), "out": _r3(end), "duration": _r3(end - start)}


def _check_transitions(raw, clips: list) -> list:
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise ValueError("境目の指定が正しくありません")
    seen, out = set(), []
    for k, t in enumerate(raw):
        if not isinstance(t, dict):
            raise ValueError(f"境目 {k + 1} の指定が正しくありません")
        try:
            after = int(t.get("after"))
        except (TypeError, ValueError):
            raise ValueError(f"境目 {k + 1} の位置(after)を整数で指定してください") from None
        if not 0 <= after <= len(clips) - 2:
            raise ValueError(f"境目 {k + 1} の位置が正しくありません(クリップ {after + 1} の後ろには境目がありません)")
        if after in seen:
            raise ValueError(f"クリップ {after + 1} の後ろの境目が重複しています")
        seen.add(after)
        kind = str(t.get("type") or "cut")
        if kind not in TRANSITION_TYPES:
            raise ValueError("境目の効果は cut / crossfade / fadeblack のいずれかです")
        if kind == "cut":
            continue
        d = _num(t.get("duration"), f"境目 {after + 1} の長さ")
        if not TRANSITION_MIN_S - EPS <= d <= TRANSITION_MAX_S + EPS:
            raise ValueError(f"クリップ {after + 1} の後ろの境目の長さは {TRANSITION_MIN_S:g}〜{TRANSITION_MAX_S:g} 秒で指定してください")
        half = min(clip_length(clips[after]), clip_length(clips[after + 1])) / 2
        if d > half + EPS:
            raise ValueError(f"クリップ {after + 1} の後ろの境目の長さ({d:g} 秒)が、前後のクリップの半分({half:.2f} 秒)を超えています")
        out.append({"after": after, "type": kind, "duration": _r3(d)})
    return sorted(out, key=lambda t: t["after"])


def _check_effects(raw, plain: float) -> list:
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise ValueError("効果の指定が正しくありません")
    out = []
    for k, e in enumerate(raw):
        no = f"効果 {k + 1}"
        if not isinstance(e, dict):
            raise ValueError(f"{no}の指定が正しくありません")
        kind = str(e.get("type") or "")
        if kind not in EFFECT_TYPES:
            raise ValueError(f"{no}の種類は " + " / ".join(EFFECT_TYPES) + " のいずれかです")
        value = None
        if kind in EFFECT_RANGE:
            lo, hi, default = EFFECT_RANGE[kind]
            value = _num(e.get("value"), f"{no}の値", default)
            if not lo <= value <= hi:
                raise ValueError(f"{no}の値({value:g})は {lo:g}〜{hi:g} の範囲で指定してください")
            value = _r3(value)
        start = None if e.get("start") in (None, "") else _num(e.get("start"), f"{no}の開始")
        end = None if e.get("end") in (None, "") else _num(e.get("end"), f"{no}の終了")
        if kind in RANGE_EFFECTS and (start is None or end is None):
            raise ValueError(f"{no}({kind})は区間(開始と終了)を指定してください")
        if start is not None and start < 0:
            raise ValueError(f"{no}の開始は 0 秒以上にしてください")
        if (end is not None and end > plain + EPS) or (start is not None and start >= plain - EPS):
            raise ValueError(f"{no}の区間がプロジェクトの長さ({plain:.2f} 秒)の外です")
        if (start if start is not None else 0.0) >= (end if end is not None else plain) - EPS and (start is not None or end is not None):
            raise ValueError(f"{no}の終了は開始より後にしてください")
        out.append({"type": kind, "value": value, "start": None if start is None else _r3(start),
                    "end": None if end is None else _r3(min(end, plain))})
    return out


def _check_audio(raw, sources: dict) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    mode = str(raw.get("mode") or "original")
    if mode not in AUDIO_MODES:
        raise ValueError("音声は original / music / mix / none のいずれかです")
    song_id = str(raw.get("song_id") or "").strip() or None
    song_db = _num(raw.get("song_db"), "曲の音量", BGM_DB)
    if not SONG_DB_MIN <= song_db <= SONG_DB_MAX:
        raise ValueError(f"曲の音量は {SONG_DB_MIN:g}〜{SONG_DB_MAX:g} dB で指定してください")
    if mode in ("music", "mix"):
        if song_id is None:
            raise ValueError("音声に曲を使うときは、曲を選んでください")
        src = sources.get(song_id)
        if src is None:
            raise ValueError("曲が見つかりません(ギャラリーから削除されたかもしれません)")
        if src["kind"] != "audio":
            raise ValueError("曲には音声を使ってください")
    return {"mode": mode, "song_id": song_id, "song_db": _r3(song_db)}


def validate_state(state: dict, sources: dict) -> dict:
    """状態を検証して、正規化したものを返す(不正なら ValueError。理由は日本語)。
    sources は {生成物の ID: {"kind", "length_s", "width", "height"(, "fps")}}(呼び出し側が台帳から作る)。
    正規化: 数は小数 3 桁、画像は in = 0・out = 秒数、cut の境目は省く、width / height / fps は先頭の動画から決め直す(渡された値は使わない)。"""
    if not isinstance(state, dict):
        raise ValueError("state は辞書で指定してください")
    raw_clips = state.get("clips") or []
    if not isinstance(raw_clips, list):
        raise ValueError("clips は配列で指定してください")
    clips = [_check_clip(i, c, sources) for i, c in enumerate(raw_clips)]
    out = {"clips": clips, "transitions": _check_transitions(state.get("transitions"), clips)}
    plain = _r3(sum(clip_length(c) for c in clips))
    out["fade_in"], out["fade_out"] = (_r3(_num(state.get(k), "先頭のフェード" if k == "fade_in" else "末尾のフェード", 0.0)) for k in ("fade_in", "fade_out"))
    if out["fade_in"] < 0 or out["fade_out"] < 0:
        raise ValueError("フェードの秒数は 0 以上にしてください")
    out["effects"] = _check_effects(state.get("effects"), plain)
    out["audio"] = _check_audio(state.get("audio"), sources)
    out["width"], out["height"], out["fps"] = derive_format(clips, sources)
    if clips and out["fade_in"] + out["fade_out"] > export_duration(out) + EPS:
        raise ValueError(f"先頭と末尾のフェードの合計が、動画の長さ({export_duration(out):.2f} 秒)を超えています")
    return out


# ---- 分割・削除・挿入(plain の時間軸。どれも新しい state を返し、元は変えない) ----

def _cut_clip(clip: dict, a: float, b: float) -> dict:
    """クリップの局所の a〜b 秒だけを残したコピー(動画は in/out を動かし、画像は秒数を変える)。"""
    c = dict(clip)
    c["in"], c["out"] = (0.0, _r3(b - a)) if clip.get("kind") == "image" else (_r3(float(clip["in"]) + a), _r3(float(clip["in"]) + b))
    c["duration"] = _r3(b - a)
    return c


def _with_items(state: dict, items: list) -> dict:
    """items([{"clip", "tr"(その後ろの境目 or None)}])から state を作り直す(境目の位置は並びから決まる)。長さに合わない境目は縮める・外す。"""
    new = copy.deepcopy(state)
    new["clips"] = [it["clip"] for it in items]
    new["transitions"] = [{"after": i, **it["tr"]} for i, it in enumerate(items[:-1]) if it["tr"]]
    return _fit_transitions(new)


def _items(state: dict) -> list:
    tr = {t["after"]: {"type": t["type"], "duration": t["duration"]} for t in state.get("transitions") or [] if t.get("type") != "cut"}
    return [{"clip": copy.deepcopy(c), "tr": tr.get(i)} for i, c in enumerate(state.get("clips") or [])]


def _fit_transitions(state: dict) -> dict:
    """境目の長さを、前後のクリップの半分に収める(切り分けたクリップが短くなったとき)。0.5 秒に届かなければ cut にする。"""
    clips, out = state["clips"], []
    for t in state.get("transitions") or []:
        a = t["after"]
        if t.get("type") == "cut" or not 0 <= a < len(clips) - 1:
            continue
        d = math.floor(min(float(t["duration"]), TRANSITION_MAX_S, min(clip_length(clips[a]), clip_length(clips[a + 1])) / 2) * 1000) / 1000
        if d >= TRANSITION_MIN_S - EPS:
            out.append({**t, "duration": d})
    state["transitions"] = out
    return state


def split_at(state: dict, t: float) -> dict:
    """plain の時刻 t を含むクリップを、そこで 2 つに分ける(同じ生成物の in/out を変えた 2 つ)。分けたところの境目は cut。
    t が境目(から MIN_CLIP_S 以内)・範囲の外なら何もしない。"""
    t = float(t)
    items, pos, out, done = _items(state), 0.0, [], False
    for it in items:
        c, length = it["clip"], clip_length(it["clip"])
        if not done and pos + MIN_CLIP_S - EPS <= t <= pos + length - MIN_CLIP_S + EPS:
            out += [{"clip": _cut_clip(c, 0.0, t - pos), "tr": None}, {"clip": _cut_clip(c, t - pos, length), "tr": it["tr"]}]
            done = True
        else:
            out.append(it)
        pos += length
    return _with_items(state, out) if done else copy.deepcopy(state)


def _map_effects(state: dict, fn_start, fn_end) -> list:
    out = []
    for e in state.get("effects") or []:
        e = dict(e)
        if e.get("start") is not None:
            e["start"] = _r3(fn_start(float(e["start"])))
        if e.get("end") is not None:
            e["end"] = _r3(fn_end(float(e["end"])))
        if e.get("start") is not None and e.get("end") is not None and e["end"] - e["start"] < EPS:
            continue
        out.append(e)
    return out


def delete_range(state: dict, start: float, end: float) -> dict:
    """plain の start〜end 秒を削除する。またがるクリップは、その位置で切って残りをつなぐ(同じ生成物の in/out を変えた 2 つになることがある。
    MIN_CLIP_S に満たない切れ端は残さない)。削った箇所に付く境目は cut(両側が元のままつながるものだけ元の境目を残す)。
    効果の区間は、削った分だけ詰める(区間が丸ごと消えた効果は外す)。"""
    total = plain_duration(state)
    a, b = max(0.0, float(start)), min(total, float(end))
    if b - a < EPS:
        return copy.deepcopy(state)
    pieces, pos = [], 0.0       # (クリップ, 境目, 元の添字, 先頭が元のまま, 末尾が元のまま)
    for idx, it in enumerate(_items(state)):
        c, length = it["clip"], clip_length(it["clip"])
        s, e = pos, pos + length
        pos = e
        if min(e, b) - max(s, a) < EPS:
            pieces.append((c, it["tr"], idx, True, True))
            continue
        if a - s >= MIN_CLIP_S - EPS:
            pieces.append((_cut_clip(c, 0.0, a - s), None, idx, True, False))
        if e - b >= MIN_CLIP_S - EPS:
            pieces.append((_cut_clip(c, b - s, length), it["tr"], idx, False, True))
    items = []
    for k, (c, tr, idx, _, right_ok) in enumerate(pieces):
        nxt = pieces[k + 1] if k + 1 < len(pieces) else None
        items.append({"clip": c, "tr": tr if tr and right_ok and nxt and nxt[2] == idx + 1 and nxt[3] else None})
    new = _with_items(state, items)
    gap = b - a
    new["effects"] = _map_effects(new, lambda p: p if p <= a else a if p < b else p - gap, lambda p: p if p <= a else a if p < b else p - gap)
    return new


def insert_at(state: dict, t: float, clip: dict) -> dict:
    """plain の時刻 t にクリップを入れる。t がクリップの途中なら、そこで分けてから入れる(境目の近く MIN_CLIP_S 以内なら、その境目に入れる)。
    入れたクリップの前後は cut。効果の区間は、入れた位置より後ろを、入れた長さだけずらす。
    clip は {"id", "kind", "in", "out"}(動画。in/out は必須)か {"id", "kind": "image", "duration"}(画像。既定 3 秒)。"""
    kind = clip.get("kind")
    if kind not in CLIP_KINDS or not clip.get("id"):
        raise ValueError("挿入するのは、動画か画像の生成物です")
    if kind == "image":
        seconds = _num(clip.get("duration"), "画像の秒数", IMAGE_DEFAULT_S)
        new_clip = {"id": clip["id"], "kind": "image", "in": 0.0, "out": _r3(seconds), "duration": _r3(seconds)}
    else:
        a, b = _num(clip.get("in"), "挿入する動画の開始", 0.0), _num(clip.get("out"), "挿入する動画の終了")
        new_clip = {"id": clip["id"], "kind": "video", "in": _r3(a), "out": _r3(b), "duration": _r3(b - a)}
    length = clip_length(new_clip)
    if length < MIN_CLIP_S - EPS:
        raise ValueError(f"挿入するクリップは短すぎます({MIN_CLIP_S:g} 秒以上にしてください)")
    bounds = [item["start"] for item in timeline(state)] + [plain_duration(state)]
    near = [i for i, x in enumerate(bounds) if abs(float(t) - x) < MIN_CLIP_S - EPS]
    base = state
    if not near:
        base = split_at(state, t)
        bounds = [item["start"] for item in timeline(base)] + [plain_duration(base)]
        near = [min(range(len(bounds)), key=lambda i: abs(bounds[i] - float(t)))]
    idx = near[0] if abs(float(t) - bounds[near[0]]) <= abs(float(t) - bounds[near[-1]]) else near[-1]
    at = bounds[idx]
    items = _items(base)
    if idx > 0:
        items[idx - 1]["tr"] = None
    items.insert(idx, {"clip": new_clip, "tr": None})
    new = _with_items(base, items)
    new["effects"] = _map_effects(new, lambda p: p + length if p >= at - EPS else p, lambda p: p + length if p > at + EPS else p)
    return new


# ---- ffmpeg の引数 ----

def render_size(state: dict, preview: bool = False) -> tuple:
    """書き出す寸法。プレビューは短辺 480(元がそれより小さければ、そのまま)。どちらの辺も偶数。"""
    w, h = int(state["width"]), int(state["height"])
    if preview and min(w, h) > PREVIEW_SHORT_EDGE:
        k = PREVIEW_SHORT_EDGE / min(w, h)
        w, h = max(2, round(w * k / 2) * 2), max(2, round(h * k / 2) * 2)      # 長いほうの辺は、いちばん近い偶数に
    return w, h


def _source(paths: dict, oid: str) -> tuple:
    """(ファイルのパス, 音声ストリームがあるか)。paths の値はパスか {"path", "has_audio"}(音声の有無が分からなければ有るとして扱う)。"""
    entry = paths.get(oid)
    if entry is None:
        raise ValueError(f"生成物 {oid} のファイルが分かりません")
    if isinstance(entry, dict):
        return str(entry["path"]), bool(entry.get("has_audio", True))
    return str(entry), True


def _input_args(state: dict, paths: dict) -> list:
    """クリップごとの入力。動画は -ss / -t で必要な範囲だけ読み(長い元ファイルの後ろのほうを使うとき、頭から読み捨てない)、
    画像は -loop 1 で秒数ぶんのコマを作る(コマ率は書き出しの fps)。"""
    argv = []
    for c in state["clips"]:
        path, _ = _source(paths, c["id"])
        if c["kind"] == "image":
            argv += ["-loop", "1", "-framerate", str(state["fps"]), "-t", _n(clip_length(c)), "-i", path]
        else:
            argv += ["-ss", _n(c["in"]), "-t", _n(clip_length(c)), "-i", path]
    return argv


def _video_chain(i: int, c: dict, w: int, h: int, fps: int) -> str:
    """クリップ i の映像: trim / setpts → fps → 寸法に収めて黒縁(pad)→ yuv420p。どのクリップも同じ形・同じ時間の刻みになり、xfade / concat がつなげる。"""
    fit = (f"scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
           f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black")
    return (f"[{i}:v]trim=duration={_n(clip_length(c))},setpts=PTS-STARTPTS,fps={fps},{fit},setsar=1,format=yuv420p[v{i}]")


def _audio_chain(i: int, c: dict, has_audio: bool) -> str:
    """クリップ i の音声: 元の音声を切って長さをクリップに合わせる(足りなければ無音で延ばす)。画像・音声の無い動画は無音(anullsrc)。"""
    d = _n(clip_length(c))
    if c["kind"] == "video" and has_audio:
        return (f"[{i}:a]atrim=duration={d},asetpts=PTS-STARTPTS,{AFMT},apad=whole_dur={d},atrim=duration={d},asetpts=PTS-STARTPTS[a{i}]")
    return f"anullsrc=channel_layout=stereo:sample_rate={AUDIO_RATE}:duration={d},{AFMT}[a{i}]"


def _label(prefix: str, first: int, last: int) -> str:
    return f"{prefix}{first}" if first == last else f"{prefix}{first}_{last}"


def _join_stages(state: dict, with_audio: bool) -> tuple:
    """クリップをつなぐ。cut でつながる連なりは concat 1 つにまとめ、その連なりを左から xfade / acrossfade で畳む(offset は、ここまでの長さ - 境目の秒数)。
    (フィルタの文の並び, 最終の映像のラベル, 音声のラベル or None)。"""
    clips, tr = state["clips"], _transitions(state)
    runs, run = [], [0]
    for j in range(len(clips) - 1):
        if j in tr:
            runs.append(run)
            run = []
        run.append(j + 1)
    runs.append(run)
    stages, labels = [], []         # labels: (映像, 音声, 長さ, 先頭, 末尾)
    for r in runs:
        first, last = r[0], r[-1]
        v, a = _label("v", first, last), _label("a", first, last)
        length = sum(clip_length(clips[k]) for k in r)
        if len(r) > 1:
            ins = "".join(f"[v{k}]" + (f"[a{k}]" if with_audio else "") for k in r)
            outs = f"[{v}_raw]" + (f"[{a}]" if with_audio else "")
            stages.append(f"{ins}concat=n={len(r)}:v=1:a={1 if with_audio else 0}{outs}")
            stages.append(f"[{v}_raw]fps={state['fps']}[{v}]")          # concat の出力は時間の刻みが 1/1000000 になり、xfade が刻みの違いを断る。fps で 1/fps に戻す
        labels.append((v, a, length, first, last))
    acc_v, acc_a, acc_len, first, _ = labels[0]
    for v, a, length, start, last in labels[1:]:
        kind, d = tr[start - 1]          # この連なりの直前の境目(前のクリップの後ろ)
        name = "fade" if kind == "crossfade" else "fadeblack"
        nv, na = _label("v", first, last), _label("a", first, last)
        stages.append(f"[{acc_v}][{v}]xfade=transition={name}:duration={_n(d)}:offset={_n(acc_len - d)}[{nv}]")
        if with_audio:
            stages.append(f"[{acc_a}][{a}]acrossfade=d={_n(d)}[{na}]")
        acc_v, acc_a, acc_len = nv, na, acc_len + length - d
    return stages, acc_v, acc_a if with_audio else None


def _gated(text: str, enable: str) -> str:
    """フィルタ text に enable(区間)を付ける。オプションの無いフィルタ(negate など)は「名前=enable=…」、あれば「…:enable=…」。"""
    if not enable:
        return text
    return text + (":" if "=" in text else "=") + enable


def _effect_filters(state: dict, size: Optional[tuple] = None) -> list:
    """効果をフィルタの並びにする。区間(plain)は export の時刻に直して enable='between(t,開始,終了)' を付ける(全体なら付けない)。
    size は書き出す寸法(幅, 高さ)。ズーム・パンの crop はこの寸法で切り出す(プレビューは短辺 480 に縮めてあるので、state の寸法では切れない)。"""
    out = []
    plain = plain_duration(state)
    fw, fh = (int(size[0]), int(size[1])) if size else (int(state["width"]), int(state["height"]))
    for e in state.get("effects") or []:
        whole = e.get("start") is None and e.get("end") is None
        a = plain_to_export(state, 0.0 if e.get("start") is None else e["start"], "start")
        b = plain_to_export(state, plain if e.get("end") is None else e["end"], "end")
        if b - a < EPS:
            continue
        gate = "" if whole else f"enable='between(t,{_n(a)},{_n(b)})'"
        kind = e["type"]
        if kind in ("fade_to_black", "fade_from_black", "fade_to_white", "fade_from_white"):
            # 区間の中だけ(enable)で、黒 / 白へ・から。区間の外は元のまま(黒を続けたいときは「黒にする」を重ねる)
            direction = "out" if kind.endswith("_to_black") or kind.endswith("_to_white") else "in"
            color = ":c=white" if kind.endswith("white") else ""
            out.append(_gated(f"fade=t={direction}:st={_n(a)}:d={_n(b - a)}{color}", gate))
        elif kind == "black":
            out.append(_gated("drawbox=c=black:t=fill", gate))
        elif kind in ("zoom_in", "zoom_out"):
            # 中央へ寄るズーム(挿入した静止画に効く)。区間の進み p = 0→1 で倍率を 1→Z(ズームイン)/ Z→1(ズームアウト)にして、コマごとに拡大(scale の
            # eval=frame。crop の w/h は起動時に 1 回しか計算されず t を使えない)してから、中央を元の寸法で切り出す。区間の外は倍率 1(何もしない)
            z, w, h = _n(e["value"]), fw, fh
            p = f"clip((t-{_n(a)})/{_n(b - a)},0,1)"
            zexpr = f"if(between(t,{_n(a)},{_n(b)}),{f'1+({z}-1)*{p}' if kind == 'zoom_in' else f'{z}-({z}-1)*{p}'},1)"
            out.append(f"scale=w='trunc(iw*({zexpr})/2)*2':h='trunc(ih*({zexpr})/2)*2':eval=frame,crop={w}:{h}:(iw-{w})/2:(ih-{h})/2")
        elif kind in PANS:
            # パン: 区間の間だけ Z 倍に拡大し(ズームしながらのパンは 1→Z)、切り出す窓を区間の進み p = 0→1 で端から端へ動かす(crop の x / y はコマごとに計算される)。
            # 区間の外は拡大しない(iw = 幅なので窓は動かない)
            z, w, h = _n(e["value"]), fw, fh
            p = f"clip((t-{_n(a)})/{_n(b - a)},0,1)"
            zexpr = f"if(between(t,{_n(a)},{_n(b)}),{f'1+({z}-1)*{p}' if kind.startswith('zoom_in_') else z},1)"
            x = {"pan_right": f"(iw-{w})*{p}", "pan_left": f"(iw-{w})*(1-{p})", "pan_down": f"(iw-{w})/2", "pan_up": f"(iw-{w})/2",
                 "zoom_in_right": f"(iw-{w})*{p}", "zoom_in_left": f"(iw-{w})*(1-{p})"}[kind]
            y = {"pan_down": f"(ih-{h})*{p}", "pan_up": f"(ih-{h})*(1-{p})"}.get(kind, f"(ih-{h})/2")
            out.append(f"scale=w='trunc(iw*({zexpr})/2)*2':h='trunc(ih*({zexpr})/2)*2':eval=frame,crop={w}:{h}:x='{x}':y='{y}'")
        elif kind == "mono":
            out.append(_gated("hue=s=0", gate))
        elif e["type"] in LOOKS:
            out.append(_gated(LOOKS[e["type"]], gate))
        else:
            out.append(_gated(f"eq={kind}={_n(e['value'])}", gate))
    return out


def _music_chain(state: dict, idx: int, total: float, mix: bool, sources_len: Optional[float]) -> str:
    """曲([idx:a])を BGM にする: 先頭から total(曲が短ければ曲の長さ)で切り、終わりの 2 秒をフェードアウト。mix は -14 dB(song_db)に下げる。
    曲が短いときは、無音で total まで延ばす(繰り返さない)。"""
    bgm_s = min(total, float(sources_len)) if sources_len else total
    fade_s = min(BGM_FADE_S, bgm_s)
    gain = f",volume={_n(state['audio']['song_db'])}dB" if mix else ""
    return (f"[{idx}:a]{AFMT},atrim=duration={_n(bgm_s)},asetpts=PTS-STARTPTS{gain},"
            f"afade=t=out:st={_n(bgm_s - fade_s)}:d={_n(fade_s)},apad=whole_dur={_n(total)}[bgm]")


def build_graph(state: dict, paths: dict, *, preview: bool = False, song_len: Optional[float] = None) -> tuple:
    """-filter_complex の文字列と、映像・音声の出力ラベル(音声なしなら None)。段ごと: クリップ → つなぎ → 効果 → 先頭・末尾のフェード → 音声の合成。"""
    clips = state["clips"]
    if not clips:
        raise ValueError("クリップがありません")
    w, h = render_size(state, preview)
    mode = state["audio"]["mode"]
    clip_audio = mode in ("original", "mix")          # クリップの音声を使うモードだけ作る(使わない音声のラベルを残すと ffmpeg が失敗する)
    total = export_duration(state)
    stages = [_video_chain(i, c, w, h, state["fps"]) for i, c in enumerate(clips)]
    if clip_audio:
        stages += [_audio_chain(i, c, _source(paths, c["id"])[1]) for i, c in enumerate(clips)]
    joined, vall, aall = _join_stages(state, clip_audio)
    stages += joined
    vfx = _effect_filters(state, (w, h))
    if state["fade_in"] > EPS:
        vfx.append(f"fade=t=in:st=0:d={_n(state['fade_in'])}")
    if state["fade_out"] > EPS:
        vfx.append(f"fade=t=out:st={_n(total - state['fade_out'])}:d={_n(state['fade_out'])}")
    stages.append(f"[{vall}]" + ",".join([*vfx, "format=yuv420p"]) + "[vout]")
    if mode == "none":
        return ";".join(stages), "vout", None
    afx = []
    if state["fade_in"] > EPS:
        afx.append(f"afade=t=in:st=0:d={_n(state['fade_in'])}")
    if state["fade_out"] > EPS:
        afx.append(f"afade=t=out:st={_n(total - state['fade_out'])}:d={_n(state['fade_out'])}")
    if mode == "original":
        stages.append(f"[{aall}]" + ",".join([*afx, "anull"]) + "[aout]")
    else:
        stages.append(_music_chain(state, len(clips), total, mode == "mix", song_len))
        if mode == "mix":
            stages.append(f"[{aall}][bgm]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[amix]")
            stages.append("[amix]" + ",".join([*afx, "anull"]) + "[aout]")
        else:
            stages.append("[bgm]" + ",".join([*afx, "anull"]) + "[aout]")
    return ";".join(stages), "vout", "aout"


def render_args(exe: str, state: dict, sources_paths: dict, dst, *, preview: bool = False, progress: bool = False, song_len: Optional[float] = None) -> list:
    """状態を 1 本の MP4 にする ffmpeg の引数。sources_paths は {生成物の ID: パス or {"path", "has_audio"}}(クリップと、曲を使うなら曲)。
    プレビューは短辺 480・crf 28・AAC 96k、書き出しは元の寸法・crf 18・AAC 256k。どちらも先頭にメタデータ(+faststart)。
    progress なら -progress pipe:1 -nostats(標準出力に out_time=… が流れる)。dst は呼び出し側が「<目的のファイル>.part」にする(-f mp4 を付けてある)。
    song_len は曲の長さ(秒。BGM を曲の終わりでフェードアウトさせる。分からなければ省く)。"""
    graph, vlabel, alabel = build_graph(state, sources_paths, preview=preview, song_len=song_len)
    argv = [exe, "-y", "-v", "error"]
    if progress:
        argv += ["-progress", "pipe:1", "-nostats"]
    argv += _input_args(state, sources_paths)
    mode = state["audio"]["mode"]
    if mode in ("music", "mix"):
        argv += ["-i", _source(sources_paths, state["audio"]["song_id"])[0]]
    argv += ["-filter_complex", graph, "-map", f"[{vlabel}]"]
    argv += ["-map", f"[{alabel}]"] if alabel else ["-an"]
    argv += ["-t", _n(export_duration(state)), "-c:v", "libx264", "-preset", "veryfast" if preview else "medium",
             "-crf", "28" if preview else "18", "-pix_fmt", "yuv420p"]
    if alabel:
        argv += ["-c:a", "aac", "-b:a", "96k" if preview else "256k", "-ar", str(AUDIO_RATE), "-ac", "2"]
    return argv + ["-movflags", "+faststart", "-f", "mp4", str(dst)]
