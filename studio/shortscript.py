# SPDX-License-Identifier: Apache-2.0
"""ショート動画の演出(筋・シーン割り・シーンごとの演出)を、管理役の LLM に書かせる。mvscript.py と同じ流儀(chat を受け取る純粋な非同期関数。
GPU・台帳・ファイルには触らない)で、JSON の取り出し・人物の定義・構図・字幕を出さない規則・歌唱の口の規則は mvscript から import して使う(複製しない)。

  outline = await story_outline(chat, story, narration=..., character=person_facts, sceneries=[...])      # 筋(1 回)
  plan    = await plan_scenes(chat, outline, total_s, min_s, max_s)                                         # 秒数と場面(timeline == "free" だけ)
  result  = await generate_scenario(chat, scenes, outline, character, descriptions, roles=roles,
                                    timeline="speech", frame="portrait", background_text="auto")           # シーンごとの演出
  result["scenes"]    # [{"index", "emotion", "place", "refs": [画像の添字], "shot", "camera", "prompt"(最終のプロンプト全文)}, ...]

参照の添字(refs)は params.images の添字と同じ。sceneries[i] と roles[i]("person" | "scenery")が params.images[i] を説明する。
H3 に渡す画像は refs の順(person があれば先頭)なので、最終のプロンプトの <Picture k> の k は refs の中の 1 始まりの位置。

H3 公式のプロンプトの形(backends/minimax-h3/skills_cache/h3-prompt-writing の base-en.txt / ref-en.txt、docs/FEATURES.en.md が出所を説明):
  - 参照なし(t2va): base-en.txt の 3 項目(integrated_multimodal_description / overall_soundscape / non_diegetic_music)。契約は
    「subject_definitions の無い 6 セクション、summary は [text generation]」としていたが、公式の t2va は subject_definitions も summary も
    持たない 3 項目で、[text generation] という task type も ref-en.txt には無いので、公式の形に従う。
  - 参照あり(ref2va): ref-en.txt の 6 セクション。景色・物は公式の §2.1 のとおり <Subject k>(「scenes, backgrounds, or environments」)として
    <Picture k> を引いて定義し(§2.2: 画像が場所の定義にだけ使われるときは独立の <Picture> 項目を作らない)、保持は partially_preserved。
"""
import json
import logging
import math
from typing import Any, Callable, Optional

from studio import mvscript as M
from studio.styles import style_phrase
from studio.mvscript import BATCH_SIZE, ScenarioError, _index_of, _items, _single_line, scenario_request

log = logging.getLogger("studio.shortscript")

Chat = M.Chat

TIMELINES = ("speech", "music", "free")
ROLE_PERSON, ROLE_SCENERY = "person", "scenery"
DESC_MAX = 400                       # LLM に見せる・プロンプトに入れる画像の説明の長さ(画像のプロンプトは長いことがある)

# 参照(人物)のいないシーンの構図文。mvscript の FRAME_SHOT_PREFIX は「<Subject 1> の頭と肩」の文なので、景色・物だけのカットには使えない
FRAME_SHOT_PREFIX_NO_PERSON = {
    "portrait": ("Vertical frame composition: the main subject of the scene is centered horizontally, with the "
                 "background arranged above and behind it. "),
    "square": "Square frame composition: the main subject of the scene is centered in the frame. ",
    "landscape": "",
}


def _clip(value: Any, limit: int = DESC_MAX) -> str:
    text = _single_line(value)
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "..."


def _lines(value: Any) -> list:
    return [t for t in (_single_line(v) for v in value) if t] if isinstance(value, list) else []


def _has_character(character: Any) -> bool:
    return isinstance(character, dict) and any(_single_line(v) for v in character.values())


# ---- 筋(1 回) ----

OUTLINE_KEYS = ("title", "setting", "characters", "arc")


async def story_outline(chat: Chat, story: str, *, song_concept: str = "", narration: str = "", character: Optional[dict] = None,
                        sceneries: Optional[list] = None) -> dict:
    """物語の文章(+ 曲の内容・セリフ)から、{"title", "setting", "characters", "arc", "places": [...], "continuity_rules": [...]} を 1 回で書く。
    読めない答え(ScenarioError・オブジェクトでない)は、警告を残して {} を返す(止めない。以降は筋なしで演出する)。
    character は人物の見た目の辞書(無ければ {})。あればその見た目は変えない(mvscript の概要と同じ言い回し)。
    sceneries は、物語が使えるように用意された景色・物の画像の説明(person は含めない)。"""
    scenery_list = [{"n": i + 1, "description": _clip(d) or "(no description)"} for i, d in enumerate(sceneries or [])]
    parts = ["Create a coherent treatment for a short narrative video (a few minutes at most, made of separate 5-10 second shots).\n",
             f"STORY: {_single_line(story) or '(derive from the narration and the references)'}\n"]
    if _single_line(song_concept):
        parts.append(f"SONG_CONCEPT (background music): {_single_line(song_concept)}\n")
    if _single_line(narration):
        parts.append(f"NARRATION (spoken over the whole video; the shots illustrate it in this order): {_single_line(narration)}\n")
    if _has_character(character):
        parts.append(f"CHARACTER_REFERENCE_FACTS: {json.dumps(character, ensure_ascii=False)}\n"
                     "The reference character is the protagonist. Their identity, hair, face, outfit and style are immutable. "
                     "Do not invent a named alternate character or redesign their appearance.\n")
    else:
        parts.append("There is no reference character. Describe any people only as needed, without fixed facial details.\n")
    if scenery_list:
        parts.append(f"SCENERY_REFERENCES (places or objects the story may use; each has a reference image): "
                     f"{json.dumps(scenery_list, ensure_ascii=False)}\n"
                     "Where it suits the story, set scenes in these places or around these objects, and list them in places.\n")
    parts.append("title: a short title in the language of STORY (Japanese if STORY is Japanese), at most 16 characters, with no "
                 "translation or romanization in parentheses. places: the distinct locations of the story in the order they appear. "
                 "continuity_rules: short rules that keep consecutive shots consistent (same place, time of day, clothing, props).\n"
                 'Return: {"title":"","setting":"","characters":"","arc":"","places":[""],"continuity_rules":[""]}.')
    try:
        data = await scenario_request(chat, "".join(parts))
    except ScenarioError as e:
        log.warning("筋を読めませんでした。筋なしで進めます: %s", e.message)
        return {}
    if not isinstance(data, dict):
        log.warning("筋がオブジェクトではありません。筋なしで進めます")
        return {}
    outline = {k: _single_line(data.get(k)) for k in OUTLINE_KEYS}
    outline["places"] = _lines(data.get("places"))
    outline["continuity_rules"] = _lines(data.get("continuity_rules"))
    return outline if any(outline.values()) else {}


# ---- シーン割り(timeline == "free" だけ) ----

def _scene_count(total_s: float, min_s: float, max_s: float) -> int:
    """秒数が [min_s, max_s] のシーンの合計が total_s になりうる個数。真ん中の長さに近い個数を選ぶ(区間が空なら最小の個数)。"""
    lo = max(1, math.ceil(total_s / max_s - 1e-9))
    hi = math.floor(total_s / min_s + 1e-9)
    if hi < lo:
        return lo
    return min(hi, max(lo, round(total_s / ((min_s + max_s) / 2))))


def _fit_durations(raw: list, total_s: float, min_s: float, max_s: float) -> list:
    """各秒数を [min_s, max_s] に収め、合計が total_s になるよう、動かせる(まだ上下限に当たっていない)シーンへ比例して配り直す。
    min_s * N <= total_s <= max_s * N なら合計は ほぼ一致する(3 桁に丸める分の 0.001 秒 × N 以下の誤差。呼ぶ側が
    コマの格子に丸めるときに吸収する)。その範囲を外れる total_s では、上下限を守って合計がずれる。"""
    d = [min(max_s, max(min_s, float(x))) for x in raw]
    for _ in range(len(d) + 1):
        diff = total_s - sum(d)
        if abs(diff) < 1e-6:
            break
        free = [i for i in range(len(d)) if (diff > 0 and d[i] < max_s - 1e-9) or (diff < 0 and d[i] > min_s + 1e-9)]
        if not free:
            break
        base = sum(d[i] for i in free)
        for i in free:
            d[i] = min(max_s, max(min_s, d[i] * (base + diff) / base))
    return [round(x, 3) for x in d]


async def plan_scenes(chat: Chat, outline: dict, total_s: float, min_s: float, max_s: float) -> list:
    """筋と長さから、シーン割り [{"duration": 秒(min_s〜max_s), "place": str, "summary": str}, ...] を作る(曲もセリフも無いときだけ)。
    個数 N は合計が total_s になりうる値で、LLM には正確な N と目標の合計を伝える。個数が違えば 1 回だけ頼み直し、それでもだめなら ScenarioError。
    秒数は [min_s, max_s] に収めて合計が total_s に合うよう配り直す(合計の誤差は 0.001 秒 × N 以下。格子への丸めは呼ぶ側)。"""
    total_s, min_s, max_s = float(total_s), float(min_s), float(max_s)
    if total_s <= 0 or min_s <= 0 or max_s < min_s:
        raise ScenarioError("シーンの長さの指定が正しくありません")
    n = _scene_count(total_s, min_s, max_s)
    template = [{"duration": 0, "place": "", "summary": ""} for _ in range(n)]
    prompt = (
        "Split the story into the shots of a short narrative video. "
        f"Write exactly {n} scenes whose durations in seconds are each between {min_s:g} and {max_s:g} and add up to {total_s:g}. "
        "The scenes play in order and together tell the whole arc (beginning, development, turn, ending). "
        "place is the location (and time of day) of the scene, one short English phrase; use the places of the treatment where they fit "
        "and keep consecutive scenes consistent with continuity_rules. summary is one English sentence about what visibly happens.\n"
        f"TREATMENT: {json.dumps(outline, ensure_ascii=False)}\n"
        f"Return exactly {n} array items. Use this exact JSON structure and replace only the zero and empty string values: "
        f"{json.dumps(template, ensure_ascii=False)}")

    def valid(result):
        return result is not None and len(result) == n and all(isinstance(it, dict) for it in result)

    result = _items(await scenario_request(chat, prompt))
    if not valid(result):
        result = _items(await scenario_request(
            chat, prompt + f"\nCORRECTION: The previous answer did not have exactly {n} scene objects. "
            f"Return exactly {n} items in order. JSON only."))
    if not valid(result):
        raise ScenarioError(f"シーン割りの数が合いません: 期待 {n} 個、LLM の答え {len(result) if result is not None else 0} 個")
    raw = []
    for it in result:
        try:
            raw.append(float(it.get("duration")))
        except (TypeError, ValueError):
            raw.append(total_s / n)                 # 読めない秒数は、平均に
    durations = _fit_durations(raw, total_s, min_s, max_s)
    return [{"duration": dur, "place": _single_line(it.get("place")), "summary": _single_line(it.get("summary"))}
            for dur, it in zip(durations, result, strict=True)]


# ---- 最終の 6 セクション(t2va は公式の 3 項目)形式 ----

# 人物が口を動かさないシーンの頭書き(mvscript の IMAGE_MV_PREFIX は「曲は後で足す」と書くので、free には使えない)
CLOSED_MOUTH_PREFIX = ("The character does not speak or mouth words in this shot and keeps a naturally closed, relaxed mouth; "
                       "the performance is in the eyes, posture, movement and the scene itself. ")
NO_PEOPLE_TEXT = "No people, faces or human figures appear in this shot; it shows only the place, objects, nature and atmosphere."
PARTIAL_RETENTION = ("the layout, main objects and atmosphere are kept; the camera angle, time of day and weather may change")


def _entries(scene: dict, sceneries: list, roles: list) -> list:
    """scene["refs"](params.images の添字)から [(k, "person" | "scenery", 説明)] を作る。k は H3 に渡す画像の 1 始まりの位置
    (ハンドラは refs の順に画像を送る)。範囲外の添字は読み飛ばす。人物は最初の 1 つだけ(複数の出演者は持たない)。"""
    out, have_person = [], False
    for idx in scene.get("refs") or []:
        if not isinstance(idx, int) or isinstance(idx, bool) or not 0 <= idx < len(roles):
            continue
        role = ROLE_PERSON if roles[idx] == ROLE_PERSON else ROLE_SCENERY
        if role == ROLE_PERSON:
            if have_person:
                continue
            have_person = True
        out.append((len(out) + 1, role, _clip(sceneries[idx]) if idx < len(sceneries) else ""))
    return out


def _soundscape(speech: bool, timeline: str) -> str:
    if speech:
        return ("No additional dialogue, ambience, crowd voices, or sound effects are introduced; "
                "the spoken narration comes only from <Audio 1>.")
    if timeline == "free":
        return "Natural ambience and sound effects matching the scene; no dialogue and no music."
    return "No dialogue, singing, or vocals; only quiet natural ambience of the location."      # music: 音は結合で曲に差し替える


def format_h3_prompt(scene: dict, direction: str, character: dict, sceneries: list, roles: list, *, frame: str = "landscape",
                     timeline: str = "free", style: str = "none", with_reference: Optional[bool] = None) -> str:
    """1 つのクリップを H3 公式の形に包む。scene["refs"] が空なら t2va、あれば ref2va(人物は <Subject k>、景色・物も <Subject k>)。
    speech(timeline == "speech" で人物が refs にある)のシーンだけ <Audio 1>(読み上げの切り出し)を持つ。字幕を出さない文は必ず入れる。"""
    entries = _entries(scene, sceneries, list(roles or []))
    person = next((e for e in entries if e[1] == ROLE_PERSON), None)
    speech = timeline == "speech" and person is not None
    direction = M.scrub_overlay_text(_single_line(direction))
    direction = f"{direction.rstrip('.')}. " if direction else ""          # _single_line が落とす文末の「.」を戻す(次の文と続かないように)
    if person is not None:
        shot_prefix = M.FRAME_SHOT_PREFIX.get(frame, "").replace("<Subject 1>", f"<Subject {person[0]}>")
    else:
        shot_prefix = FRAME_SHOT_PREFIX_NO_PERSON.get(frame, "")
    soundscape = _soundscape(speech, timeline)
    # 画風の文(none なら "")。参照画像を 1 枚でも持つ制作は、このシーンが参照を使わなくても、参照の見た目を保つ文にする
    styled = style_phrase(style, bool(entries) if with_reference is None else with_reference)

    if not entries:
        # t2va: 公式 base-en.txt の 3 項目。[Shot 1] はタイムスタンプなしで、文頭にスタイルと構図
        look = f"{styled} One continuous shot" if styled else "Cinematic, coherent short-film style in one continuous shot"
        return (f"integrated_multimodal_description: [Shot 1] {shot_prefix}{look} "
                f"with no internal cut. {NO_PEOPLE_TEXT} {direction}{M.NO_OVERLAY_TEXT}\n\n"
                f"overall_soundscape: {soundscape}\n\n"
                "non_diegetic_music: N/A")

    defs, retention, labels = [], [], []
    for k, role, desc in entries:
        if role == ROLE_PERSON:
            defs.append(f"<Subject {k}> is the exact character shown in <Picture {k}>: {M.subject_definition(character or {})}. "
                        "The same identity, facial structure, hair, clothing, and visual style must remain unchanged.")
            retention.append(f"<Subject {k}> (appears in [Shot 1]): fully_preserved - the referenced character's identity, "
                             f"face, hair, clothing, and visual style are retained exactly: "
                             f"{_single_line(M.identity_instruction(character or {}))}.")
        else:
            labels.append(f"<Subject {k}>")
            defs.append(f"<Subject {k}> is the location or object shown in <Picture {k}>" + (f": {desc}." if desc else "."))
            retention.append(f"<Subject {k}> (appears in [Shot 1]): partially_preserved - {PARTIAL_RETENTION}.")
    if speech:
        defs.append(f"<Audio 1> is the supplied narration (spoken voice signal) for this entire target clip and is reused as the "
                    f"clip's synchronized audio track for <Subject {person[0]}> (S1).")
        retention.append("<Audio 1>: fully_copy - <Audio 1> is reused 1:1 as the target clip's complete synchronized audio track.")

    # summary
    if person is None:
        action = "shows the place and objects of the referenced locations with no people in the frame"
        who = "the shot"
    elif speech:
        action = "quietly speaks the supplied narration in the scene of the story (not singing, no microphone), with clearly visible synchronized lip movement"
        who = f"<Subject {person[0]}>"
    else:
        action = "appears in the scene without speaking, with a closed, relaxed mouth"
        who = f"<Subject {person[0]}>"
    place_sentence = f" The shot is set in or around {', '.join(labels)}." if labels else ""
    summary = (f"[reference generation{' + audio reuse' if speech else ''}] The target video is a single continuous short-film shot in "
               f"which {who} {action}" + (", while <Audio 1> is reused for synchronization." if speech else ".") + place_sentence)

    # detailed_description
    if styled:
        style = f"{styled} It contains one continuous shot with no internal cut."
    else:
        style = "The target video uses a coherent cinematic short-film style"
        if person is not None:
            style += f" while fully preserving the reference character <Subject {person[0]}>"
        style += ". It contains one continuous shot with no internal cut."
    if labels:
        style += (f" The setting follows {', '.join(labels)}: {PARTIAL_RETENTION}.")
    if person is None:
        performance = NO_PEOPLE_TEXT
    elif speech:
        performance = (f"<Subject {person[0]}> (S1) speaks the supplied narration naturally, as quiet talk or inner monologue within the "
                       "scene: not singing, no microphone, no stage. The full face and unobstructed mouth remain visible, and the lips, "
                       "jaw, and facial muscles articulate every audible phoneme in precise synchronization with <Audio 1>. "
                       "The spoken content comes only from <Audio 1>.")
    else:
        performance = (f"<Subject {person[0]}> does not speak; the mouth stays naturally closed and relaxed.")
    return (
        "subject_definitions:\n" + "\n".join(defs) + "\n\n"
        f"summary:\n{summary}\n\n"
        "retention_analysis:\n" + "\n".join(retention) + "\n\n"
        "detailed_description:\n"
        f"{style} {M.NO_OVERLAY_TEXT}\n"
        f"[Shot 1] {shot_prefix}{direction}{performance} No text overlay.\n\n"
        f"overall_soundscape:\n{soundscape}\n\n"
        "non_diegetic_music:\n"
        "N/A"
    )


# ---- シーンごとの演出 ----

# 口の規則(speech: 全シーンが読み上げの演技 / 人物はいるが読み上げなし / 人物なし)
# 読み上げのシーンの頭書き(mvscript の歌唱の頭書きに当たる。「演奏・パフォーマンス」の語を使わない: H3 がマイクを持たせて歌わせた、2026-11-24 の実機)
SPEAKING_PREFIX = (
    "Medium close-up, front-facing or three-quarter face view. Use the exact same person from the reference image; preserve facial "
    "identity, hair, outfit, and visual style unchanged: {identity}. The full face and mouth remain clearly visible and unobstructed "
    "throughout the shot. The character quietly speaks the supplied narration as natural talk or inner monologue - not singing, no "
    "microphone, no stage; the lips and jaw articulate every phoneme in precise synchronization with the reference voice. Subtle "
    "natural breathing and facial motion, a calm, present expression. ")

MOUTH_RULE_SPEECH = (
    "In every scene the character quietly speaks the narration as natural talk or inner monologue while living the scene of the story "
    "(walking, pausing, looking): this is NOT a performance - never a stage, concert, microphone, singing or audience. Use a medium "
    "close-up, keep the entire face and mouth visible, and state that the lips move naturally with the spoken words (<Audio 1>). "
    "Never use a wide shot, rear view, eye-only shot or obscured mouth. SPOKEN (when present) is what is said during that scene: use it only to choose the "
    "expression, gesture and setting that match its meaning; never quote it, write it out or show it as text. ")
MOUTH_RULE_PERSON = (
    "The character never speaks, sings or mouths words in any scene; explicitly describe a closed relaxed mouth whenever the face is "
    "visible. Any shot size is fine, including wide shots and views from behind. Set no_person to true for a scene that should show "
    "no person at all (a pure place, object or atmosphere shot), otherwise keep it false. ")
RULE_NO_PERSON = (
    "There is no character in this video: describe only places, objects, nature, weather and atmosphere. No people, faces, hands or "
    "human figures appear in any scene. ")

RULE_VOICEOVER = (
    "The narration is an off-screen voice-over laid over the finished video: nobody on screen speaks, sings or mouths words, and "
    "there is no lip-sync. Each scene illustrates what SPOKEN says at that moment (its places, objects, weather, light and mood) "
    "without quoting it or showing it as text. There is no character in this video: describe only places, objects, nature, "
    "weather and atmosphere; no people, faces, hands or human figures appear in any scene. ")


def _brief(scene: dict, spoken: Optional[list]) -> dict:
    """LLM に見せるシーンの項目(番号・時刻・尺・あれば場面と要約。読み上げの文)。"""
    out = {"index": int(scene["index"])}
    if scene.get("start") is not None and scene.get("end") is not None:
        out["start"], out["end"] = round(float(scene["start"]), 3), round(float(scene["end"]), 3)
    duration = scene.get("duration") if scene.get("duration") is not None else (
        float(scene["end"]) - float(scene["start"]) if "start" in out else None)
    if duration is not None:
        out["duration"] = round(float(duration), 3)
    for key in ("place", "summary"):
        if _single_line(scene.get(key)):
            out[key] = _single_line(scene[key])
    if spoken:
        out["spoken"] = spoken
    return out


def sentences_in_scene(sentences: Optional[list], start: Any, end: Any, *, last: bool = False) -> list:
    """読み上げの文 [{"text", "start", "end"}, ...] のうち、中点が [start, end) に入るもの(最後のシーンは end を含む)の text を、順に返す。"""
    if not sentences or start is None or end is None:
        return []
    out = []
    for s in sentences:
        try:
            text, mid = _single_line(s.get("text")), (float(s["start"]) + float(s["end"])) / 2
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
        if text and (float(start) <= mid < float(end) or (last and mid == float(end))):
            out.append(text)
    return out


def _ref_ints(value: Any) -> list:
    """LLM が書いた refs から、重複のない整数を、書かれた順に返す(文字列の数字も読む)。"""
    if not isinstance(value, list):
        value = [value]
    out = []
    for v in value:
        if isinstance(v, bool):
            continue
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if n not in out:
            out.append(n)
    return out


def _truthy(value: Any) -> bool:
    return value is True or (isinstance(value, str) and value.strip().lower() in ("true", "yes", "1"))


async def generate_scenario(chat: Chat, scenes: list, outline: dict, character: dict, sceneries: list, *, timeline: str, frame: str,
                            background_text: str, roles: Any = (), narration_sentences: Optional[list] = None,
                            style: str = "none", voiceover: bool = False,
                            on_progress: Optional[Callable[[int, int], None]] = None) -> dict:
    """シーンごとの演出を BATCH_SIZE 個ずつ頼み、使う参照(refs)と整形した最終プロンプトを返す。概要は渡された outline(追加の呼び出しは無い)。
    番号と個数が合わなければ 1 回だけ頼み直し、それでも合わなければ ScenarioError。on_progress(終わったシーン数, 全シーン数)は batch ごと。

    refs の規則(1 つに決める): 人物の画像があれば、人物を **常に先頭に入れる**(物語の主人公。speech では必須)。例外は timeline が "music" / "free"
    のとき LLM がそのシーンに "no_person": true を付けた場合だけで、そのシーンは人物を外す(景色・物だけのカット)。続く景色・物は、LLM が選んだ
    番号のうち roles が "scenery" の添字だけ(重複は 1 つに、書かれた順)。人物が無く景色も選ばなければ refs は空 = t2va。
    sceneries[i] / roles[i] は params.images[i] の説明と役割。人物の見た目は character(辞書)。speech に人物の画像が無ければ ScenarioError
    (voiceover = True のときを除く。画面の外の声: 口の規則は入れず、読み上げの文(SPOKEN)を手がかりに景色を描く。人物は映さない)。"""
    if timeline not in TIMELINES:
        raise ScenarioError(f"時間軸の指定が正しくありません: {timeline}")
    roles = list(roles or [])
    descriptions = list(sceneries or [])
    person_idx = roles.index(ROLE_PERSON) if ROLE_PERSON in roles else None
    scenery_idx = [i for i, r in enumerate(roles) if r != ROLE_PERSON]
    voiceover = bool(voiceover) and timeline == "speech" and person_idx is None
    if timeline == "speech" and person_idx is None and not voiceover:
        raise ScenarioError("セリフのある動画には、人物の画像が必要です(口を合わせる相手がいません)")
    speech = timeline == "speech" and not voiceover          # 口を合わせる(voiceover は読み上げの文を見せるだけ)
    facts = json.dumps(character, ensure_ascii=False)
    scenery_list = [{"index": i, "description": (_clip(descriptions[i]) if i < len(descriptions) else "") or "(no description)"}
                    for i in scenery_idx]
    outline = outline if isinstance(outline, dict) else {}
    briefs = []
    for pos, s in enumerate(scenes):
        spoken = (sentences_in_scene(narration_sentences, s.get("start"), s.get("end"), last=pos == len(scenes) - 1)
                  if timeline == "speech" and narration_sentences else None)
        briefs.append(_brief(s, spoken))

    if voiceover:
        mouth_rule, person_block = RULE_VOICEOVER, ""
    elif person_idx is None:
        mouth_rule, person_block = RULE_NO_PERSON, ""
    else:
        mouth_rule = MOUTH_RULE_SPEECH if speech else MOUTH_RULE_PERSON
        person_block = (f"CHARACTER_REFERENCE_FACTS: {facts}\n"
                        "Keep exactly one character and never describe hair color, hairstyle, face, clothing, age or art style "
                        "differently from CHARACTER_REFERENCE_FACTS. ")
    scenery_block = ""
    if scenery_list:
        scenery_block = (f"SCENERY_REFERENCES (reference images of places or objects; refs lists the index of each one a scene uses): "
                         f"{json.dumps(scenery_list, ensure_ascii=False)}\n"
                         "Put an index in refs only when that scene is set in that place or shows that object (never more than "
                         "three); leave refs empty for a scene that uses none of them. ")

    outputs: list = []
    previous: Optional[dict] = None
    for start in range(0, len(scenes), BATCH_SIZE):
        batch, batch_briefs = scenes[start:start + BATCH_SIZE], briefs[start:start + BATCH_SIZE]
        expected = [int(s["index"]) for s in batch]
        template = []
        for i in expected:
            item = {"index": i, "emotion": "", "place": "", "refs": []}
            if person_idx is not None and not speech:
                item["no_person"] = False
            template.append({**item, "shot": "", "camera": "", "prompt": ""})
        batch_prompt = (
            "Write one production prompt per fixed scene of a short narrative video. Do not add/remove scenes or change times. "
            "Prompts must be English and suitable for MiniMax-H3 (a text-to-video or reference-to-video model). "
            + person_block + mouth_rule + scenery_block +
            "Use restrained camera motion and avoid cuts inside a generated clip. Follow CONTINUITY_RULES so consecutive scenes keep the "
            "same place, time of day, clothing and props unless the story moves on. Describe only what is visible; the place of each "
            "scene follows the story. " + M.NO_OVERLAY_RULE + "\n"
            + M.FRAME_LLM_GUIDANCE.get(frame, "")
            + M.BACKGROUND_TEXT_GUIDANCE.get(background_text, "")
            + M.style_rule(style)
            + f"TREATMENT: {json.dumps(outline, ensure_ascii=False)}\n"
            f"CONTINUITY_RULES: {json.dumps(outline.get('continuity_rules') or [], ensure_ascii=False)}\n"
            f"PREVIOUS: {json.dumps(previous, ensure_ascii=False)}\n"
            f"SCENES: {json.dumps(batch_briefs, ensure_ascii=False)}\n"
            f"Return exactly {len(batch)} array items with indexes {expected}. "
            "Use this exact JSON structure and replace only the empty values: "
            f"{json.dumps(template, ensure_ascii=False)}"
        )
        result = _items(await scenario_request(chat, batch_prompt))
        if result is None or [_index_of(it) for it in result] != expected:
            result = _items(await scenario_request(
                chat, batch_prompt + "\nCORRECTION: The previous answer had the wrong item count or indexes. "
                f"Return exactly {len(batch)} items in this order: {expected}. JSON only."))
        if result is None or len(result) != len(batch):
            raise ScenarioError(f"シーン {start + 1} 以降の演出の数が合いません: 期待 {len(batch)} 個、LLM の答え "
                                f"{len(result) if result is not None else 0} 個")
        actual = [_index_of(it) for it in result]
        if actual != expected:
            raise ScenarioError(f"シーンの番号が合いません: 期待 {expected}、LLM の答え {actual}")
        previous = dict(result[-1])          # 整形前の項目(mvscript と同じ。整形後の定型文を次の batch が真似ないように)
        for scene, item in zip(batch, result, strict=True):
            refs = [i for i in _ref_ints(item.get("refs")) if i in scenery_idx]
            drop_person = not speech and _truthy(item.get("no_person"))
            if person_idx is not None and not drop_person:
                refs.insert(0, person_idx)
            placed = {**scene, "refs": refs}
            raw = str(item.get("prompt") or "")
            if person_idx is not None and not drop_person:
                if speech:
                    direction = SPEAKING_PREFIX.format(identity=M.identity_instruction(character)) + M.keep_mouth_visible(raw, "speaking framing")      # 口が見える構図は歌唱と同じ規則、演技は「語り」
                else:
                    direction = (f"Use the exact same person from the reference image; preserve facial identity, hair, outfit, and "
                                 f"visual style unchanged: {M.identity_instruction(character)}. " + CLOSED_MOUTH_PREFIX + raw)
            else:
                direction = raw                  # 人物なし: 「人物は映らない」の文は format_h3_prompt が足す
            outputs.append({"index": _index_of(item), "emotion": _single_line(item.get("emotion")),
                            "place": _single_line(item.get("place")) or _single_line(scene.get("place")), "refs": refs,
                            "shot": _single_line(item.get("shot")), "camera": _single_line(item.get("camera")),
                            "prompt": format_h3_prompt(placed, direction, character, descriptions, roles, frame=frame, timeline=timeline,
                                                     style=style, with_reference=bool(roles))})
        if on_progress is not None:
            on_progress(len(outputs), len(scenes))
    return {"prompt_format": "MiniMax H3 official format (t2va: three fields, ref2va: six sections)",
            "character": character, "overview": outline, "scenes": outputs}
