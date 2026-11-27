# Ported from Minimax-H3-lipsync-mv (Apache-2.0, Copyright 2026 YUZUKI): app/services/llm.py の _extract_json / _scenario_request /
# _identity_instruction / _subject_definition / frame_kind / _FRAME_LLM_GUIDANCE / _BACKGROUND_TEXT_GUIDANCE / _FRAME_SHOT_PREFIX /
# format_h3_official_ref2va_prompt / enforce_reference_and_lipsync / generate_scenario。プロンプトの文言と整形の規則はそのままで、
# modified: (1) 管理役の LLM(Llm.chat は system と user の 1 往復)を呼ぶ非同期にした。JSON の修復の 1 回は、前の答えを会話に足さず、
# 同じ依頼に「JSON だけで答え直す」指示を足して頼み直す。(2) JSON の取り出しは、地の文の中の小さな JSON(「[1, 2, 3]」など)を飛ばして、
# オブジェクトかオブジェクトの配列を優先する。(3) 読み上げ動画(narration)の分岐と analyze_character は、ここには移植しない
# (人物の特徴は文章の説明から来る。画像を見る analyze_character は mvdescribe.py)。(4) 全体の構成(overview)が読めなくても、空のまま進める。(5) 次の batch に渡す PREVIOUS は、
# 整形前の項目にする(整形後の定型文を小さなモデルが真似ないように)。(6) 配列の代わりに {"scenes": [...]} で返されても読む。
# (7) 歌わないシーン(前奏・間奏・後奏)は、構図を順に変える(NON_SINGING_SHOTS。最後のシーンは去っていく後ろ姿)。
# (8) 公式の手引き(ref-en.txt の detailed_description は 350〜500 語・ショットごとに構図から音まで具体的に)と、公式 SKILL.md の
# Tips(2026-08-11: 抽象語より具体的な描写)に合わせて、演出の詳しさの規則(detail_rule)と、全体の構成から作る冒頭の画風の文(overview_look)を足した。
# (9) シーンごとに LLM の演出(direction)も返し、やり直しで演出だけを書き換えたときに同じ規則で組み立て直せるようにした(scene_prompt)。
# SPDX-License-Identifier: Apache-2.0
"""MV のシーンごとの演出を、管理役の LLM に書かせる(H3 公式の Ref2VA 6 セクション形式のプロンプトにして返す)。

  result = await generate_scenario(chat, scenes, concept, character, frame="landscape", on_progress=cb)
  result["scenes"]    # [{"index", "emotion", "shot", "camera", "prompt"(整形済みの最終プロンプト)}, ...]
  result["overview"]  # {"title", "visual_theme", "color_script", "story_arc", "continuity_rules"}

chat は Llm.chat と同じ形(system, user, max_tokens, temperature → 応答の文字列)。scenes は {"index", "start", "end", "duration",
"vocal_ratio", ...} の並び、character は {"subject": 人物の説明, ...}。概要を 1 回、シーンを 5 つずつの batch で頼み、番号を確かめる。
"""
import json
import logging
import re
from typing import Any, Awaitable, Callable, Optional

from .styles import STYLES, style_phrase

log = logging.getLogger("studio.mvscript")

# chat(system, user, max_tokens, temperature) -> 応答の文字列(Llm.chat と同じ形)
Chat = Callable[..., Awaitable[str]]

BATCH_SIZE = 5
SINGING_RATIO = 0.35                 # vocal_ratio がこれ以上のシーンは歌唱シーン(analyze.py の VOCAL_SCENE_RATIO と同じ)
DEFAULT_SUBJECT = "the exact person, face, hair, clothing, and visual style visible in the image"
SYSTEM_PROMPT = ("You are a music-video director and MiniMax-H3 prompt engineer. "
                 "Preserve one character identity and return strictly valid JSON.")
REPAIR_INSTRUCTION = ("\n\nYour previous response was not valid JSON. Return the complete answer again as JSON only, with no "
                      "Markdown fence, commentary, or reasoning. Preserve the exact schema and requested number of scenes.")


class ScenarioError(Exception):
    """シナリオを作れなかった。message は利用者向けの日本語。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


# ---- JSON の取り出し ----

def _structured(value) -> bool:
    """中身のあるオブジェクト、またはオブジェクトを含む配列(前置きの [1, 2, 3] や {} ではない、本体らしい値)。"""
    return (isinstance(value, dict) and bool(value)) or (isinstance(value, list) and any(isinstance(v, dict) for v in value))


def extract_json(text: str) -> Any:
    """応答から JSON を取り出す。コードブロックの囲みや前置きの文があっても、途中の JSON を探して読む。
    前置きの中に [ や { があっても飛ばし、オブジェクトかオブジェクトの配列を優先する(無ければ最初に読めた値)。"""
    cleaned = re.sub(r"\s*```$", "", re.sub(r"^```(?:json)?\s*", "", (text or "").strip()))
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    decoder, first, pos = json.JSONDecoder(), None, 0
    while True:
        m = re.search(r"[\[{]", cleaned[pos:])
        if m is None:
            break
        start = pos + m.start()
        try:
            value, end = decoder.raw_decode(cleaned, start)
        except json.JSONDecodeError:
            pos = start + 1
            continue
        if _structured(value):
            return value
        first = first if first is not None else value
        pos = end
    if first is None:
        raise ScenarioError("LLM の応答に JSON がありません" if not re.search(r"[\[{]", cleaned) else "LLM の応答の JSON を読めません")
    return first


async def scenario_request(chat: Chat, prompt: str, max_tokens: int = 3000) -> Any:
    """LLM に JSON を頼む。読めなければ、JSON だけで答え直す指示を足して 1 回だけ頼み直す(温度 0)。"""
    content = await chat(SYSTEM_PROMPT, prompt, max_tokens=max_tokens, temperature=0.35)
    try:
        return extract_json(content)
    except ScenarioError:
        log.warning("LLM の応答が JSON ではありません。頼み直します(%d 文字)", len(content or ""))
    content = await chat(SYSTEM_PROMPT, prompt + REPAIR_INSTRUCTION, max_tokens=max_tokens, temperature=0.0)
    return extract_json(content)



CHARACTER_KEYS = ("subject", "face", "hair", "outfit", "style")
CHARACTER_FACTS_PROMPT = (
    "Extract the fixed visual appearance of the ONE person in the description below, for a video model that also receives "
    "that person's reference image. Keep only: subject (who, e.g. 'a young anime-style girl'), face (fixed features such as "
    "eye shape and color, never the expression), hair, outfit, style (art style / rendering). Leave out expression, emotion, "
    "location, background, lighting, time of day, pose, action, held props and camera. "
    "Use short English phrases; an unknown field is an empty string. Return JSON only: "
    '{"subject":"","face":"","hair":"","outfit":"","style":""}\nDESCRIPTION: ')


async def character_facts(chat: Chat, description: str) -> dict:
    """人物の説明(画像のプロンプトなど)から、見た目だけを {subject, face, hair, outfit, style} に抜き出す。

    場所・光・ポーズまで人物の定義に入ると、全シーンがその場面に縛られる(2026-11-22 の実機で、窓辺の画像から作った MV が
    5 シーン中 4 つ同じ部屋になった)。説明が無ければ LLM を呼ばない。読めない答えなら、説明をそのまま subject にする。
    """
    text = _single_line(description)
    if not text or text == DEFAULT_SUBJECT:
        return {"subject": text or DEFAULT_SUBJECT}
    try:
        data = await scenario_request(chat, CHARACTER_FACTS_PROMPT + text, max_tokens=400)
    except ScenarioError:
        data = None
    if isinstance(data, list) and data and isinstance(data[0], dict):
        data = data[0]
    facts = {k: _single_line(data.get(k)) for k in CHARACTER_KEYS} if isinstance(data, dict) else {}
    facts = {k: v for k, v in facts.items() if v}
    return facts if facts.get("subject") or len(facts) >= 2 else {"subject": text}


# ---- 人物・構図 ----

def _single_line(value: Any) -> str:
    """LLM の返した値を 1 行の文にする。文字列のはずがオブジェクト・配列で返ってきたら、中の値を「; 」でつなぐ
    (str にすると {'scene_description': '...'} がそのままプロンプトに入る。2026-11-26 のショート動画の筋の places)。"""
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, (list, tuple)):
        return "; ".join(t for t in (_single_line(v) for v in value) if t)
    return re.sub(r"\s+", " ", str(value or "")).strip().rstrip(".")


def identity_instruction(character: dict) -> str:
    anchor = str(character.get("identity_anchor") or "").strip()
    visible = ", ".join(str(character.get(k) or "").strip() for k in ("subject", "face", "hair", "outfit", "style")
                        if str(character.get(k) or "").strip())
    return anchor or visible or "the exact same person and appearance shown in the reference image"


def subject_definition(character: dict) -> str:
    facts = [_single_line(character.get(k)) for k in ("subject", "face", "hair", "outfit", "style") if _single_line(character.get(k))]
    return ", ".join(facts) or DEFAULT_SUBJECT


def frame_kind(width: int, height: int) -> str:
    """生成サイズから構図の種類を返す: "portrait" / "square" / "landscape"。"""
    ratio = width / max(height, 1)
    if ratio < 0.9:
        return "portrait"
    if ratio > 1.1:
        return "landscape"
    return "square"


# LLM へのシーン指示(縦長・正方形では被写体を中央に置かせる)。横長は何も足さない。
FRAME_LLM_GUIDANCE = {
    "portrait": (
        "The output is a VERTICAL frame for mobile viewing (9:16 or 4:5). Keep the character "
        "horizontally centered. Place props, screens and scenery above, behind or around the "
        "character, never to one side in a way that pushes the character toward the frame edge. "
    ),
    "square": (
        "The output is a SQUARE 1:1 frame. Keep the character centered; keep props and scenery "
        "behind the character rather than to one side. "
    ),
    "landscape": "",
}

# 背景の文字の扱い。動画モデルは大きな短い文字は描けるが、小さな文章は崩れる。
BACKGROUND_TEXT_MODES = ("auto", "large", "none")
BACKGROUND_TEXT_GUIDANCE = {
    "auto": "",
    "large": (
        "Background text rule: any readable text in the scene (signs, titles, logos, headings, "
        "screens) must be LARGE and SHORT, one to three words, clearly legible. Render small or "
        "dense text (book pages, documents, labels, paragraphs on screens or boards) as abstract "
        "shapes, lines, icons or diagrams instead of letters. "
    ),
    "none": (
        "Background text rule: the scene contains no readable letters or words. Signs, screens, "
        "boards, books and labels show only shapes, colors, icons, diagrams or pictures. "
    ),
}

# 画面に文字を重ねない(テロップ・字幕・歌詞の文字・タイトル・ロゴ・透かし)。H3 は MV らしい場面に字幕を描くことがあった(2026-11-24、利用者の報告)。
# 背景の文字(看板など)とは別に、すべてのシーンに必ず入れる。H3 には否定の指示(negative prompt)が無いので、肯定の文で書く
NO_OVERLAY_TEXT = ("The picture is clean: no subtitles, captions, lyric text, karaoke text, title cards, credits, logos, "
                   "watermarks or any other text overlay appear anywhere in the frame.")
NO_OVERLAY_RULE = ("Never describe subtitles, captions, lyrics or any words shown on screen, title cards, credits, logos or "
                   "watermarks; the picture has no text overlay. ")
# LLM が書いてしまった「画面の文字」の文は、機械的に取り除く
_OVERLAY_WORDS = re.compile(r"subtitle|caption|lyric[s]? (?:text|appear|scroll|display|on screen|overlay)|karaoke|text overlay|"
                            r"on-?screen text|title card|credits? roll|watermark|telop", re.IGNORECASE)


def scrub_overlay_text(text: str) -> str:
    """演出の文から、画面に文字を重ねる文(字幕・テロップ・歌詞の表示など)を文ごと取り除く。"""
    sentences = re.split(r"(?<=[.!?])\s+", str(text or "").strip())
    return " ".join(s for s in sentences if s and not _OVERLAY_WORDS.search(s))


# [Shot 1] の冒頭に置く構図文。H3 は文頭の指示が最も効く。横長は LLM の構図に任せる。
FRAME_SHOT_PREFIX = {
    "portrait": (
        "Vertical frame composition: <Subject 1> is centered horizontally, with the head and "
        "shoulders in the upper-middle of the frame and the background arranged above and behind "
        "the subject. "
    ),
    "square": "Square frame composition: <Subject 1> is centered in the frame. ",
    "landscape": "",
}
# 歌わないシーンの構図文。上の縦長の文は「頭と肩」で寄りの画になるので、引きや横からの画に合う文にする
FRAME_SHOT_PREFIX_NON_SINGING = {
    **FRAME_SHOT_PREFIX,
    "portrait": (
        "Vertical frame composition: <Subject 1> stays horizontally centered, with the scenery arranged above, "
        "below and behind the subject. "
    ),
}

# 歌わないシーン(前奏・間奏・後奏)の構図。参照画像は正面の顔が多く、構図を書かないと H3 はそのポーズに寄り、口も動きがちだった
# (2026-11-27 の実機「柚子色の灯り」: 前奏の 2 シーンが、歌うシーンと同じ正面の顔で話しているように見えた。引きの画と横からの画に
# 書き直すと、口を閉じた落ち着いた画になった)。歌わないシーンの順に回し、続く前奏どうしが同じ構図にならないようにする。
# 構図の文を頭に置くだけでは弱く(同じ曲の試験で、引きの指示でも正面のバストアップに寄った)、場所と周りの変化(灯りがともる など)を
# 構図に合わせて書いた演出で効いたので、LLM にもシーンの framing として渡し、その構図で場面を書かせる(試験: 横から追う画は真横の
# 姿になり、口も閉じた。引きの画は、こちらに歩いてくる姿にはなるが寄り気味のまま。構図の文を演出の後ろに回しても同じ seed で変わらない)。
NON_SINGING_SHOTS = (
    "Wide establishing shot of the whole place: <Subject 1> appears small and full-body in the distance, walking slowly "
    "toward the camera while the camera gently dollies backward.",
    "Side tracking shot: the camera glides sideways alongside <Subject 1>, seen in profile, as they walk through the scene.",
    "Low-angle medium-wide shot: <Subject 1> stands still, then slowly looks up and around at the surroundings.",
    "Over-the-shoulder shot from behind <Subject 1>: the camera follows slowly as they gaze out over the scene ahead.",
)
# 曲の最後のシーンが歌わないシーン(後奏)なら、去っていく後ろ姿で締める
NON_SINGING_LAST_SHOT = ("Closing wide shot from behind: <Subject 1> walks slowly away from the camera, deeper into the scene, "
                         "while the camera holds and they grow smaller in the frame.")


# ---- H3 公式の Ref2VA 6 セクション形式 ----

DEFAULT_STYLE_SENTENCE = "The target video uses a coherent cinematic music-video style while fully preserving the reference character."
LOOK_MAX_WORDS = 30
LOOK_RULE = ("visual_theme and color_script are each ONE concrete sentence of at most 25 words (light sources, colors, materials, "
             "weather), without abstract words such as cinematic. ")


def _first_sentence(value: Any, max_words: int = LOOK_MAX_WORDS) -> str:
    """最初の 1 文(長ければ max_words 語で切り、終わりの「,」「and」などは落とす)。"""
    text = _single_line(value)
    first = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0].rstrip(".!?")
    words = first.split()
    if len(words) > max_words:
        words = words[:max_words]
        while words and (words[-1].lower().strip(",;:") in ("and", "or", "with", "of", "the", "a", "an") or not words[-1].strip(",;:")):
            words.pop()
    return " ".join(words).rstrip(",;:")


def overview_look(overview: Any) -> str:
    """全体の構成(visual_theme・color_script)から、冒頭の画風の文を作る(無ければ "")。公式の手引きは、[Shot 1] の前の 1〜2 文で画風を
    決め、例も「柔らかい光とやや彩度を落とした色」のように具体的。既定の文(DEFAULT_STYLE_SENTENCE)の「cinematic」は抽象語なので、
    LLM の書いた具体的な見た目があれば、それを使う。どちらも最初の 1 文だけ(2026-11-27 の実機で、visual_theme が 3 文・60 語を超え、
    語数で切ると文の途中で切れた。全体の構成の依頼にも LOOK_RULE で 1 文にするよう頼む)。"""
    if not isinstance(overview, dict):
        return ""
    theme, colors = _first_sentence(overview.get("visual_theme")), _first_sentence(overview.get("color_script"))
    if not theme and not colors:
        return ""
    look = f"The target video is a music video that fully preserves the reference character, with one consistent look: {theme or colors}."
    return look + (f" Color palette: {colors}." if theme and colors else "")


def detail_rule(sound: bool = False) -> str:
    """演出の詳しさの規則(LLM への依頼)。公式の Ref2VA の手引き(ref-en.txt の冒頭と 5.2 節)は、ショットごとに構図・被写体の見た目と
    位置・環境と光・動作と状態の変化・カメラの動き・音を具体的に書き、detailed_description 全体で 350〜500 語を目安にする。定型文が
    約 150 語あるので、LLM の書く分は 120〜180 語にする(2026-11-27 の時点では 50〜60 語で、全体は 190〜250 語だった)。
    音は、H3 の音をそのまま使うときだけ書かせる(MV、読み上げ・曲のあるショート動画は、音を決まった音声に差し替える)。"""
    sound_part = ("; and the sounds of the moment" if sound else
                  ". Do not describe sounds or music: the audio is fixed separately")
    return ("Each prompt is about 120-180 English words of concrete, visible detail covering, in this order: the composition and "
            "where the main subject is in the frame; the place, its light sources and colors; what the subject does and how it "
            "changes during the clip; the camera movement (type, amplitude and speed)" + sound_part + ". Prefer concrete details "
            "(light sources, colors, materials, textures, weather) over abstract words such as cinematic or beautiful. ")


def style_rule(style) -> str:
    """演出を書く LLM への画風の規則(none なら "")。全シーンで同じ画風にさせる。"""
    phrase = STYLES.get(str(style or "none"), STYLES["none"])["phrase"]
    return f"STYLE: All scenes share this visual style: {phrase}; never contradict it.\n" if phrase else ""


def style_sentence(style, default: str = DEFAULT_STYLE_SENTENCE, with_reference: bool = True) -> str:
    """最終のプロンプトの画風の文。画風を選んだら、既定の「cinematic ... style」の文をその画風の文に差し替える。"""
    return style_phrase(style, with_reference) or default


def format_h3_official_ref2va_prompt(scene: dict, visual_direction: str, character: dict, frame: str = "landscape",
                                     lipsync: bool = True, style: str = "none", look: str = "") -> str:
    """1 つのクリップを、MiniMax H3 公式の Ref2VA の 6 セクション形式に包む。

    参照画像は「キーフレーム」ではなく再利用する被写体なので、公式の指針どおり <Picture 1> の単独の保持項目にはせず、
    <Subject 1> の中で引く。1 シーン = H3 への 1 リクエストなので、タイムラインは常にタイムスタンプなしの [Shot 1] が 1 つ。
    歌詞の文字は書かない(Vocal Lock が <Audio 1> から歌の中身を移す。推測した歌詞は公式の指針に反する)。
    lipsync=False(歌わせないイメージ MV)は、H3 に音声を渡さないので <Audio 1> が無い形(画像だけの参照生成)。音声は結合で曲に差し替える。
    look は overview_look の冒頭の画風の文(画風を選んだときは、画風の文が優先)。
    """
    subject = subject_definition(character)
    identity = _single_line(identity_instruction(character))
    direction = _single_line(visual_direction)
    if not lipsync:
        return _format_image_mv_prompt(subject, identity, direction, frame, style, look)
    sings = singing(scene)
    frame_prefix = (FRAME_SHOT_PREFIX if sings else FRAME_SHOT_PREFIX_NON_SINGING).get(frame, "")

    if sings:
        performance = (
            "<Subject 1> (S1) physically performs the supplied vocal. The full face and "
            "unobstructed mouth remain visible, and the lips, jaw, and facial muscles articulate "
            "every audible phoneme in precise synchronization with <Audio 1>. The sung content "
            "comes only from <Audio 1>."
        )
        summary_action = "performs the supplied vocal with clearly visible synchronized lip movement"
    else:
        performance = (
            "<Audio 1> remains the reused timing and vocal track for this interval. "
            "<Subject 1> does not sing during the near-silent vocal interval and keeps a naturally "
            "closed, relaxed mouth without unrelated speech-like movement."
        )
        summary_action = "remains on screen with a closed, relaxed mouth during the non-vocal interval"

    return (
        "subject_definitions:\n"
        f"<Subject 1> is the exact character shown in <Picture 1>: {subject}. "
        "The same identity, facial structure, hair, clothing, and visual style must remain unchanged.\n"
        "<Audio 1> is the supplied isolated vocal signal for this entire target clip and is reused "
        "as the clip's synchronized audio track for <Subject 1> (S1).\n\n"
        "summary:\n"
        "[reference generation + audio reuse] The target video is a single continuous music-video "
        f"shot in which <Subject 1> {summary_action}, while <Audio 1> is reused for synchronization.\n\n"
        "retention_analysis:\n"
        "<Subject 1> (appears in [Shot 1]): fully_preserved - the referenced character's identity, "
        f"face, hair, clothing, and visual style are retained exactly: {identity}.\n"
        "<Audio 1>: fully_copy - <Audio 1> is reused 1:1 as the target clip's complete synchronized "
        "audio track.\n\n"
        "detailed_description:\n"
        f"{style_sentence(style, look or DEFAULT_STYLE_SENTENCE)} It contains one continuous shot with no internal cut. {NO_OVERLAY_TEXT}\n"
        f"[Shot 1] {frame_prefix}{scrub_overlay_text(direction)} {performance} No text overlay.\n\n"
        "overall_soundscape:\n"
        "No additional dialogue, ambience, crowd voices, or sound effects are introduced; the "
        "synchronized vocal content comes only from <Audio 1>.\n\n"
        "non_diegetic_music:\n"
        "N/A"
    )


def _format_image_mv_prompt(subject: str, identity: str, direction: str, frame: str, style: str = "none", look: str = "") -> str:
    """歌わせない(イメージ MV)クリップの 6 セクション形式。参照は <Picture 1> だけで、音声の参照は無い。
    H3 が作る音声は使わない(mute で頼み、結合で曲に差し替える)ので、音の欄は「声も歌も無い」とだけ書く。"""
    return (
        "subject_definitions:\n"
        f"<Subject 1> is the exact character shown in <Picture 1>: {subject}. "
        "The same identity, facial structure, hair, clothing, and visual style must remain unchanged.\n\n"
        "summary:\n"
        "[reference generation] The target video is a single continuous music-video shot in which "
        "<Subject 1> appears in the scene without singing or speaking; the music is added later in editing.\n\n"
        "retention_analysis:\n"
        "<Subject 1> (appears in [Shot 1]): fully_preserved - the referenced character's identity, "
        f"face, hair, clothing, and visual style are retained exactly: {identity}.\n\n"
        "detailed_description:\n"
        f"{style_sentence(style, look or DEFAULT_STYLE_SENTENCE)} It contains one continuous shot with no internal cut. {NO_OVERLAY_TEXT}\n"
        f"[Shot 1] {FRAME_SHOT_PREFIX.get(frame, '')}{scrub_overlay_text(direction)} "
        "<Subject 1> (S1) does not sing or speak; the mouth stays naturally closed and relaxed. No text overlay.\n\n"
        "overall_soundscape:\n"
        "No dialogue, singing, or vocals; only quiet natural ambience of the location.\n\n"
        "non_diegetic_music:\n"
        "N/A"
    )


# 歌わせない(イメージ MV)の、全シーン共通の頭書き
IMAGE_MV_PREFIX = ("The character does not sing or mouth words in this shot and keeps a naturally closed, relaxed mouth; "
                   "the music is added later, so the performance is in the eyes, posture, movement and the scene itself. ")


# 口の動きを確かめられなくする構図の指示(口を合わせるシーンで取り除く)
_MOUTH_HIDING_REWRITES = {
    r"\bextreme close-up(?: shot)? of (?:the )?(?:eye|eyes)\b": "medium close-up portrait",
    r"\b(?:extreme )?wide shot\b": "medium close-up shot",
    r"\b(?:full-body|full body|long) shot\b": "medium close-up shot",
    r"\bover-the-shoulder shot\b": "front three-quarter medium close-up shot",
    r"\bfrom behind\b": "from a front three-quarter angle",
    r"\bprofile(?: shot)?\b": "three-quarter face view",
    r"\btilts? (?:her|his|their) head back\b": "keeps the face oriented toward the camera",
}


def keep_mouth_visible(prompt: str, neutral: str = "performance framing") -> str:
    """口を合わせるシーンの演出から、口を隠す構図を取り除く。公式の検証は、1 つの [Shot] の中でショットの大きさの語が重なると
    構図の変更とみなすことがあるので(固定の頭書きが唯一のショットの大きさを持つ)、残ったショットの大きさの語は中立の語 neutral にする。"""
    for pattern, replacement in _MOUTH_HIDING_REWRITES.items():
        prompt = re.sub(pattern, replacement, prompt, flags=re.IGNORECASE)
    return re.sub(r"\b(?:medium close-up(?: shot| portrait)?|medium shot|medium-wide shot)\b", neutral, prompt, flags=re.IGNORECASE)


def singing(scene: dict) -> bool:
    return float(scene.get("vocal_ratio") or 0.0) >= SINGING_RATIO


def non_singing_shots(scenes: list) -> dict:
    """歌わないシーンの番号 → 構図の文。歌わないシーンの順に NON_SINGING_SHOTS を回し、曲の最後のシーンは NON_SINGING_LAST_SHOT。"""
    shots, n = {}, 0
    for pos, sc in enumerate(scenes):
        if singing(sc):
            continue
        last = pos == len(scenes) - 1 and len(scenes) > 1
        shots[int(sc["index"])] = NON_SINGING_LAST_SHOT if last else NON_SINGING_SHOTS[n % len(NON_SINGING_SHOTS)]
        n += 1
    return shots


_CLOSE_SHOT_WORDS = re.compile(r"\b(?:extreme )?(?:medium close-up|close-up|medium shot)(?: shot| portrait)?\b", re.IGNORECASE)


def neutral_shot_size(prompt: str, neutral: str = "scene framing") -> str:
    """構図の文を頭に置くシーンで、LLM の演出に残った寄りのショットの語を中立の語にする(1 つの [Shot] でショットの大きさが
    食い違うと、構図の変更とみなされることがある。keep_mouth_visible と同じ考え)。引き・全身の語は、構図と食い違わないので残す。"""
    prompt = _CLOSE_SHOT_WORDS.sub(neutral, prompt)
    return prompt[:1].upper() + prompt[1:]


def enforce_reference_and_lipsync(scene: dict, raw_prompt: str, character: dict, lipsync: bool = True,
                                  shot: Optional[str] = None) -> str:
    """LLM の後で、人物の同一性と口の見えを守る規則を、必ず当てる。
    lipsync=False(イメージ MV)は、歌の割合を見ずに、どのシーンも「歌わない・口は閉じたまま」にする(口を動かすのは音声の参照そのもので、
    音声を渡さなければプロンプトの閉口の指示が効く。mv_studio_V3 の 2026-08-26 の実機プローブ)。
    shot(non_singing_shots の構図の文)は、歌わないシーンの頭に置き、LLM の演出のショットの大きさの語は中立にする。"""
    identity = identity_instruction(character)
    prompt = str(raw_prompt or "").strip()
    if not lipsync:
        return ("Use the exact same person from the reference image; preserve facial identity, hair, outfit, "
                f"and visual style unchanged: {identity}. " + IMAGE_MV_PREFIX + prompt)
    if singing(scene):
        prompt = keep_mouth_visible(prompt)
        prefix = (
            "Medium close-up performance shot, front-facing or three-quarter face view. "
            "Use the exact same person from the reference image; preserve facial identity, hair, outfit, "
            f"and visual style unchanged: {identity}. "
            "The full face and mouth remain clearly visible and unobstructed throughout the shot. "
            "The character delivers the supplied vocal naturally; the lips and jaw articulate every phoneme "
            "in precise synchronization with the reference vocal audio. Subtle natural breathing and facial "
            "muscle motion, direct performance presence. "
        )
    else:
        prefix = (
            "Use the exact same person from the reference image; preserve facial identity, hair, outfit, "
            f"and visual style unchanged: {identity}. "
            "The character is not singing in this interval and keeps a naturally closed, relaxed mouth. "
        )
        if shot:
            prefix, prompt = f"{shot} {prefix}", neutral_shot_size(prompt)
    return prefix + prompt


_FRAMING_WORDS = re.compile(r"\b(?:close-up|medium(?:-wide)? shot|wide shot|long shot|full[- ]body|establishing shot|tracking shot|"
                            r"profile|over-the-shoulder|from behind|low[- ]angle|high[- ]angle|aerial|bird'?s[- ]eye)\b", re.IGNORECASE)


def names_framing(text: str) -> bool:
    """演出が自分で構図(ショットの大きさ・向き)を決めているか。やり直しで人が書いた演出は、その構図を優先する。"""
    return bool(_FRAMING_WORDS.search(str(text or "")))


def scene_prompt(scene: dict, direction: str, character: dict, *, frame: str = "landscape", lipsync: bool = True,
                 style: str = "none", look: str = "", shot: Optional[str] = None) -> str:
    """LLM(またはやり直しで人・エージェント)の演出 1 つから、規則を当てた最終のプロンプトを作る(generate_scenario とやり直しで同じ)。"""
    enforced = enforce_reference_and_lipsync(scene, direction, character, lipsync=lipsync, shot=shot)
    return format_h3_official_ref2va_prompt(scene, enforced, character, frame=frame, lipsync=lipsync, style=style, look=look)


def rebuild_scene_prompt(params: dict, scenes: list, index: int, direction: str) -> str:
    """やり直しで演出だけを書き換えたシーンの最終のプロンプト。制作の params(character_facts・script・幅と高さ・lipsync・style)から、
    台本を書いたときと同じ規則で組み立てる。歌わないシーンの構図は、演出が自分で構図を決めていなければ、台本と同じ順の構図を足す。"""
    scene = next(s for s in scenes if int(s["index"]) == int(index))
    lipsync = bool(params.get("lipsync", True))
    shots = non_singing_shots(scenes) if lipsync and not names_framing(direction) else {}
    return scene_prompt(scene, direction, params.get("character_facts") or {}, frame=frame_kind(params["width"], params["height"]),
                        lipsync=lipsync, style=params.get("style") or "none", look=overview_look(params.get("script")),
                        shot=shots.get(int(index)))


# ---- シナリオ全体 ----

def _items(result: Any) -> Optional[list]:
    """batch の答えから項目の配列を取る。配列そのもの、{"scenes": [...]}(値が配列 1 つだけのオブジェクトも)、
    1 シーンだけの batch で項目そのもの(配列に入っていないオブジェクト)を返された場合を受ける。"""
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        if isinstance(result.get("scenes"), list):
            return result["scenes"]
        lists = [v for v in result.values() if isinstance(v, list)]
        if len(lists) == 1:
            return lists[0]
        if "index" in result:
            return [result]
    return None


def _index_of(item: Any) -> int:
    try:
        return int(item.get("index", -1))
    except (AttributeError, TypeError, ValueError):
        return -1


def _brief(scene: dict, framing: str = "") -> dict:
    """LLM に見せるシーンの項目(尺・歌の割合・境界の種類だけ。点数やコマ数は要らない)。framing は歌わないシーンの構図の文。"""
    out = {"index": int(scene["index"]), "start": round(float(scene["start"]), 3), "end": round(float(scene["end"]), 3),
           "duration": round(float(scene.get("duration") or float(scene["end"]) - float(scene["start"])), 3),
           "vocal_ratio": round(float(scene.get("vocal_ratio") or 0.0), 3)}
    if scene.get("boundary_kind"):
        out["boundary_kind"] = scene["boundary_kind"]
    if framing:
        out["framing"] = framing.replace("<Subject 1>", "the character")
    return out


# シーンごとの演出の依頼のうち、口の扱い。歌わせる(リップシンク)か、歌わせない(イメージ MV)かで入れ替える
MOUTH_RULE_LIPSYNC = (
    "For vocal_ratio >= 0.35 use a medium close-up, keep the entire "
    "face and mouth visible, and explicitly request phoneme-level lip articulation synchronized to "
    "the supplied vocal. Never use a wide shot, rear view, eye-only shot or obscured mouth for a "
    "singing scene. For lower vocal_ratio (intro, interlude, outro) explicitly describe a closed relaxed mouth and never "
    "a pose facing the camera. Such scenes come with a framing: write the prompt around it, starting with that framing and "
    "the whole place, where the character is in the frame and what they do (walking, pausing, looking up, a small gesture), "
    "and one visible change in the surroundings (lights coming on, snow starting, wind in the trees). ")
MOUTH_RULE_IMAGE_MV = (
    "This is an image music video: the character NEVER sings, speaks or mouths words on camera, in any scene, "
    "whatever vocal_ratio says (the music is added later in editing). Describe cinematic scenes instead: walking, "
    "gazing, turning, small gestures, light and weather, the place around the character. Any shot size is fine, "
    "including wide shots and views from behind. Explicitly describe a closed relaxed mouth. ")


async def generate_scenario(chat: Chat, scenes: list, concept: str, character: dict, *, frame: str = "landscape",
                            background_text: str = "auto", lipsync: bool = True, style: str = "none",
                            on_progress: Optional[Callable[[int, int], None]] = None) -> dict:
    """全体の構成(overview)を 1 回、シーンごとの演出を BATCH_SIZE 個ずつ頼み、整形した最終プロンプトを返す。
    番号と個数が合わなければ 1 回だけ頼み直し、それでも合わなければ ScenarioError。
    on_progress(終わったシーン数, 全シーン数)は batch が終わるごとに呼ぶ。lipsync=False は歌わせないイメージ MV(口の規則と最終の形が変わる)。"""
    shots = non_singing_shots(scenes) if lipsync else {}
    briefs = [_brief(s, shots.get(int(s["index"]), "")) for s in scenes]
    facts = json.dumps(character, ensure_ascii=False)
    try:
        overview = await scenario_request(chat, (
            ("Create a coherent MV treatment. Do not change scene times.\n" if lipsync else
             "Create a coherent treatment for an image music video in which the character never sings on camera. Do not change scene times.\n") +
            f"USER_CONCEPT: {_single_line(concept) or '(derive from the music and character)'}\n"
            f"CHARACTER_REFERENCE_FACTS: {facts}\n"
            "The reference character's identity, hair, face, outfit and style are immutable. "
            "Do not invent a named alternate character or redesign their appearance.\n"
            f"SCENES: {json.dumps(briefs, ensure_ascii=False)}\n"
            + LOOK_RULE +
            'Return: {"title":"","visual_theme":"","color_script":"","story_arc":"","continuity_rules":[""]}.'))
    except ScenarioError as e:
        log.warning("全体の構成を読めませんでした。空のまま進めます: %s", e.message)
        overview = {}
    if not isinstance(overview, dict):
        log.warning("全体の構成がオブジェクトではありません。空のまま進めます")
        overview = {}

    look = overview_look(overview)
    outputs: list = []
    previous: Optional[dict] = None
    for start in range(0, len(scenes), BATCH_SIZE):
        batch, batch_briefs = scenes[start:start + BATCH_SIZE], briefs[start:start + BATCH_SIZE]
        expected = [int(s["index"]) for s in batch]
        template = [{"index": i, "emotion": "", "shot": "", "camera": "", "prompt": ""} for i in expected]
        batch_prompt = (
            "Write one production prompt per fixed scene. Do not add/remove scenes or change times. "
            "Prompts must be English and suitable for MiniMax-H3 ref2va. Keep exactly one character. "
            "Never describe hair color, hairstyle, face, clothing, age or art style differently from "
            "CHARACTER_REFERENCE_FACTS. " + (MOUTH_RULE_LIPSYNC if lipsync else MOUTH_RULE_IMAGE_MV) + detail_rule(sound=False) +
            "Use restrained camera motion and avoid cuts inside a generated clip. " + NO_OVERLAY_RULE + "\n"
            + FRAME_LLM_GUIDANCE.get(frame, "")
            + BACKGROUND_TEXT_GUIDANCE.get(background_text, "")
            + style_rule(style)
            + f"CHARACTER_REFERENCE_FACTS: {facts}\n"
            f"TREATMENT: {json.dumps(overview, ensure_ascii=False)}\n"
            f"PREVIOUS: {json.dumps(previous, ensure_ascii=False)}\n"
            f"SCENES: {json.dumps(batch_briefs, ensure_ascii=False)}\n"
            f"Return exactly {len(batch)} array items with indexes {expected}. "
            "Use this exact JSON structure and replace only the empty string values: "
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
        previous = dict(result[-1])          # 整形前の項目(下で prompt を書き換えても、次の batch に渡す内容は変わらない)
        for scene, item in zip(batch, result, strict=True):
            raw = str(item.get("prompt") or "")
            item["direction"] = _single_line(raw)
            item["prompt"] = scene_prompt(scene, raw, character, frame=frame, lipsync=lipsync, style=style, look=look,
                                          shot=shots.get(int(scene["index"])))
        outputs.extend(result)
        if on_progress is not None:
            on_progress(len(outputs), len(scenes))
    return {"prompt_format": "MiniMax H3 official Ref2VA six-section format" + ("" if lipsync else " (image MV, no audio reference)"),
            "character": character,
            "overview": overview, "scenes": outputs}
