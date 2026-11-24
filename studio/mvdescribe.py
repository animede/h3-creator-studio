# Ported from Minimax-H3-lipsync-mv (Apache-2.0, Copyright 2026 YUZUKI): app/services/llm.py の analyze_character(参照画像の見た目の解析:
# system プロンプトと、答えの JSON の形 subject / face / hair / outfit / style / identity_anchor)。
# modified: (1) 管理役の LLM(Llm.chat_image、1 往復)を呼ぶ非同期にした。(2) 見た目だけを読むよう、表情・場所・光・ポーズ・動作を除く指示を足した。
# (3) 人物の説明の欄に入れる日本語の 1 行(description_ja)を同じ答えで頼み、無ければ英語の特徴から日本語にする。(4) JSON が読めなければ
# 1 回だけ頼み直す。(5) 画像を読めないときは、台帳のプロンプトから見た目を抜き出す(mvscript.character_facts)か、中立の文に落とす。
# 曲を聞く(listen_song)と、曲の内容・世界観の提案(propose_concept)は、このスタジオで新しく書いた。
# SPDX-License-Identifier: Apache-2.0
"""MV の入力の「自動の説明」: 画像から人物の見た目(describe_character)、曲を聞いて曲の内容・世界観(listen_song と propose_concept)を、
管理役の LLM(Gemma 4 E4B の画像・音声の入力)で作る。

  r = await describe_character(llm, image_path, prompt="")
  r["text"]     # 人物の説明の欄に入れる日本語の 1 行(見た目だけ: 誰・顔・髪・服・画風。場所・光・ポーズ・動作は入れない)
  r["facts"]    # 英語の特徴 {"subject", "face", "hair", "outfit", "style", "identity_anchor"}(読めた項目だけ)
  r["source"]   # "vision"(画像を見た)/ "prompt"(画像を読めないので、台帳のプロンプトから)/ "default"(どちらも無い。text は空)
  r["note"]     # source が vision 以外のとき、その理由

  heard = await listen_song(llm, song_path, duration)     # {"language", "genre", "mood", "instruments", "vocal", "tempo"}(聞き取れた項目だけ。英語)
  text = await propose_concept(llm, heard, song_params)    # 曲の内容・世界観の案(日本語 2〜3 文)

llm は Llm と同じ口(chat と chat_image と chat_audio)を持つもの。画像を読めない(投影ファイルが取れない・LLM が断る)ときも、describe_character は
例外にせず、source と note で知らせる(制作の中で自動で使うので、MV を止めない)。LLM そのものが使えないときだけ LlmError。
曲を聞けない(listen_song)ときは LlmError(音声を読めない・LLM が止まっている・聞き取れなかった)か mv_audio.MvAudioError(ffmpeg が使えない)。
歌詞は、聞き取りにも案にも使わない(書き起こしの精度が分からず、歌詞を推測しないのが MV の方針)。
"""
import asyncio
import json
import logging
import mimetypes
import re
import tempfile
from pathlib import Path
from typing import Optional

from . import mv_audio, mvscript
from .llm import LlmError
from .mv import CHARACTER_MAX, CONCEPT_MAX, _clip

log = logging.getLogger("studio.mvdescribe")

FACT_KEYS = ("subject", "face", "hair", "outfit", "style", "identity_anchor")
VISION_SYSTEM = ("You analyze a character reference for identity-preserving video generation. "
                 "Describe only visible facts. Never invent a different hairstyle, hair color, outfit, age, "
                 "ethnicity, accessories, or art style. Return strictly valid JSON.")
VISION_PROMPT = (
    "Look at the image. Describe ONLY the fixed visual appearance of the ONE main person (or character) in it, "
    "for a video model that also receives this image as a reference.\n"
    'Return JSON only: {"subject":"","face":"","hair":"","outfit":"","style":"","identity_anchor":"","description_ja":""}\n'
    'subject: who it is, e.g. "a young anime-style girl". face: fixed features such as eye shape and color (never the expression). '
    "hair: color, length, style. outfit: the clothes and accessories worn. "
    "style: the art style / rendering (e.g. anime cel shading, photorealistic).\n"
    "identity_anchor: a concise English instruction that preserves the exact same person.\n"
    "description_ja: the same facts as ONE compact Japanese line, like a character sheet: hair, eyes, outfit, who, then the art style "
    "(for example: 「長い銀髪、青い瞳、白いセーラー服の少女。アニメ調」). No sentence ending such as 「〜している」.\n"
    "Leave out expression, emotion, smile, mouth, location, background, lighting, time of day, pose, action, held props and camera. "
    "An unknown field is an empty string.")
JSON_REPAIR = ("\n\nYour previous response was not valid JSON. Return the complete answer again as JSON only, "
               "with no Markdown fence, commentary, or reasoning. Preserve the exact schema.")
LINE_SYSTEM = "You write short Japanese character descriptions for a music-video creator."
LINE_PROMPT = ("Write the appearance of the ONE character below as ONE compact Japanese line, like a character sheet: hair, eyes, outfit, who, "
               "then the art style (for example: 「長い銀髪、青い瞳、白いセーラー服の少女。アニメ調」). Use only the facts given. "
               "No sentence ending such as 「〜している」, and nothing about expression, location, lighting, pose or action. "
               "Output the line only.\nFACTS: ")
LISTEN_FIELDS = ("language", "genre", "mood", "instruments", "vocal", "tempo")
LISTEN_SYSTEM = ("You are a music listener who describes songs for a music-video planner. Describe only what you hear. "
                 "Never transcribe or guess lyrics. Return strictly valid JSON.")
LISTEN_PROMPT = (      # 配列で頼むと、どの曲も "Pop, Rock" に寄った。文字列+ジャンルの例を出すと雰囲気まで細かくなる(2026-11-24 に 3 曲で比べた)
    "Listen to this song excerpt and describe it for a music-video director. Return JSON only: "
    '{"language": "<language of the vocals, or none>", "genre": "<genre>", "mood": "<mood, 3-6 words>", '
    '"instruments": "<main instruments>", "vocal": "<vocal type: female, male, duet, choir or none>", "tempo": "<slow/medium/fast>"}\n'
    "For genre, prefer the most specific fitting style, e.g. J-Pop, City Pop, Bossa Nova, Jazz, Enka, Anime song, "
    "Idol pop, Ballad, Rock, Metal, Hip Hop, R&B, EDM, Lo-fi, Folk, Classical, Vocaloid, K-Pop, Reggaeton. "
    "Name traditional instruments (shamisen, koto, shakuhachi, taiko, erhu) when you hear them. "
    "Do not write lyrics. An unknown field is empty.")
NO_SOUND = {"language": "instrumental", "vocal": "none"}      # 歌が無い、という答え(別の 1 か所に歌があれば、混ぜるときに捨てる)
EXCERPT_S = 30             # 聞かせる 1 か所の長さ(Gemma の音声入力は 1 つ 30 秒まで)
SHORT_SONG_S = 40          # この長さ以下の曲(長さ不明も)は、先頭の 1 か所だけ
EXCERPT_AT = (0.25, 0.60)  # 長い曲は、長さのこの割合の位置から(2 か所)
EXCERPT_GAP_S = 10         # 2 か所の開始がこれ以上離れていなければ、先頭の 1 か所だけ
LISTEN_RATE = 16000        # 聞かせる音声は 16kHz モノラルの WAV
CONCEPT_SYSTEM = "You are a music-video director who plans visuals from a song's description. You answer in Japanese."
CONCEPT_PROMPT = ("Write the \"song content and world\" note for a music video (MV) in JAPANESE: 2 to 3 short sentences, within 200 Japanese characters "
                  "in total. First the song's mood and genre, then fitting scenes, colours and a simple story arc for the MV. "
                  "Plain text only: no title, no bullet points, no quotation marks. Do not write numbers such as BPM or the key. "
                  "Do not mention the singer's gender or looks (the MV's character is chosen separately). Do not quote or invent lyrics.\nSONG INFO")
CONCEPT_GUESSED = " (guessed by listening to short excerpts, so approximate)"
CONCEPT_RETRY = "\nIMPORTANT: The answer must be written in Japanese."
JAPANESE_RE = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]")      # ひらがな・カタカナ・漢字


class NothingFound(mvscript.ScenarioError):
    """画像から人物の見た目を、曲から聞き取りを、読み取れなかった(JSON は読めた。頼み直しても同じ)。"""


def _is_japanese(text: str) -> bool:
    return bool(JAPANESE_RE.search(text or ""))


# ---- 画像から人物の見た目 ----

def _read_facts(data) -> tuple:
    """画像の解析の答え(JSON)から、(英語の特徴の dict(読めた項目だけ), 日本語の 1 行)を取る。人物が読み取れなければ ScenarioError。"""
    if isinstance(data, list) and data and isinstance(data[0], dict):
        data = data[0]
    if not isinstance(data, dict):
        raise mvscript.ScenarioError("画像の解析の答えがオブジェクトではありません")
    facts = {k: mvscript._single_line(data.get(k)) for k in FACT_KEYS}
    facts = {k: v for k, v in facts.items() if v}
    ja = mvscript._single_line(data.get("description_ja"))
    if not (facts.get("subject") or len(facts) >= 2):
        raise NothingFound("画像から人物の見た目を読み取れませんでした(人物が写っていないかもしれません)")
    return facts, ja


async def _look(llm, data: bytes, mime: str) -> tuple:
    """画像を見て (特徴の dict, 日本語の 1 行)。JSON が読めなければ、JSON だけで答え直す指示を足して 1 回だけ頼み直す。"""
    for repair in ("", JSON_REPAIR):
        content = await llm.chat_image(VISION_SYSTEM, VISION_PROMPT + repair, data, mime, max_tokens=700, temperature=0.0)
        try:
            return _read_facts(mvscript.extract_json(content))
        except NothingFound:
            raise
        except mvscript.ScenarioError:
            log.warning("画像の解析の応答が JSON ではありません(%d 文字)", len(content or ""))
    raise mvscript.ScenarioError("画像の解析の答えを読めませんでした")


async def _japanese_line(llm, facts: dict) -> str:
    """英語の特徴を、人物の説明の欄に入れる日本語の 1 行にする。日本語にできなければ、英語の特徴をつなげたもの。"""
    keep = {k: v for k, v in facts.items() if k != "identity_anchor" and v}
    try:
        line = await llm.chat(LINE_SYSTEM, LINE_PROMPT + json.dumps(keep, ensure_ascii=False), max_tokens=200, temperature=0.2)
    except LlmError as e:
        log.warning("日本語の 1 行にできませんでした: %s", e.message)
        line = ""
    line = mvscript._single_line(line).strip("「」\"'")
    return line if _is_japanese(line) else ", ".join(keep.values())


async def describe_character(llm, image_path, prompt: str = "") -> dict:
    """画像を見て、人物の説明(日本語の 1 行)と英語の特徴を返す。画像を読めなければ、prompt(台帳の画像のプロンプト)から見た目を抜き出し、
    それも無ければ空(source = "default")。"""
    try:
        data = await asyncio.to_thread(Path(image_path).read_bytes)
        facts, ja = await _look(llm, data, mimetypes.guess_type(str(image_path))[0] or "image/png")
    except (OSError, LlmError, mvscript.ScenarioError) as e:
        why = e.message if isinstance(e, NothingFound) else f"画像を読めませんでした({getattr(e, 'message', e)})"
        log.warning("%s", why)
    else:
        text = ja if _is_japanese(ja) else await _japanese_line(llm, facts)
        return {"text": _clip(text, CHARACTER_MAX), "facts": facts, "source": "vision", "note": ""}
    prompt = (prompt or "").strip()
    if prompt:       # この先の LlmError は、LLM そのものが使えない
        facts = await mvscript.character_facts(llm.chat, prompt)
        return {"text": _clip(await _japanese_line(llm, facts), CHARACTER_MAX), "facts": facts, "source": "prompt",
                "note": f"{why}。画像のプロンプトから見た目を抜き出しました"}
    return {"text": "", "facts": {"subject": mvscript.DEFAULT_SUBJECT}, "source": "default",
            "note": f"{why}。画像のプロンプトも無いので、空のままにします(演出は、画像の人物としか書きません)"}


# ---- 曲を聞く ----

def excerpt_starts(duration) -> list:
    """曲のどこを聞かせるか(開始の秒数の並び)。40 秒以下(長さ不明も)は先頭の 1 か所、長い曲は長さの 25% と 60% の位置から 30 秒ずつ
    (終わりに収まるよう寄せる。2 か所が近すぎるときは、先頭の 1 か所だけ)。"""
    try:
        duration = float(duration)
    except (TypeError, ValueError):
        return [0.0]
    if duration <= SHORT_SONG_S:
        return [0.0]
    starts = [round(min(duration * at, duration - EXCERPT_S), 3) for at in EXCERPT_AT]
    return starts if starts[1] - starts[0] >= EXCERPT_GAP_S else [0.0]


def _words(value) -> list:
    """聞き取りの答えの 1 項目(文字列、または文字列の配列)を、語の並びにする(「,」「、」で区切る)。"""
    out = []
    for v in value if isinstance(value, (list, tuple)) else [value]:
        for word in re.split(r"[,、;]", "" if v is None else str(v)):
            word = re.sub(r"^(?:and|&)\s+", "", word.strip().strip("."), flags=re.IGNORECASE)      # 「…, and wistful」の and を落とす
            if word and word.lower() not in ("unknown", "n/a", "-"):
                out.append(word)
    return out


def _read_listen(data) -> dict:
    """聞き取りの答え(JSON)から {項目: 語の並び}(読めた項目だけ)を取る。1 つも無ければ NothingFound。"""
    if isinstance(data, list) and data and isinstance(data[0], dict):
        data = data[0]
    if not isinstance(data, dict):
        raise mvscript.ScenarioError("曲の聞き取りの答えがオブジェクトではありません")
    keyed = {str(k).strip().lower(): v for k, v in data.items()}
    heard = {f: _words(keyed.get(f)) for f in LISTEN_FIELDS}
    heard = {f: v for f, v in heard.items() if v}
    if not heard:
        raise NothingFound("曲から何も聞き取れませんでした")
    return heard


async def _hear(llm, clip: bytes) -> dict:
    """1 か所の音声を聞いて {項目: 語の並び}。JSON が読めなければ、JSON だけで答え直す指示を足して 1 回だけ頼み直す。"""
    for repair in ("", JSON_REPAIR):
        content = await llm.chat_audio(LISTEN_SYSTEM, LISTEN_PROMPT + repair, [clip], max_tokens=300, temperature=0.0)
        try:
            return _read_listen(mvscript.extract_json(content))
        except NothingFound:
            raise
        except mvscript.ScenarioError:
            log.warning("曲の聞き取りの応答が JSON ではありません(%d 文字)", len(content or ""))
    raise mvscript.ScenarioError("曲の聞き取りの答えを読めませんでした")


async def listen_song(llm, song_path, duration=None) -> dict:
    """曲を聞いて、{language, genre, mood, instruments, vocal, tempo}(英語の語を「, 」でつないだ文字列。聞き取れた項目だけ)を返す。

    曲の 30 秒を最大 2 か所(excerpt_starts。duration は曲の長さ)、ffmpeg で 16kHz モノラルの WAV に切り出し、1 か所ずつ聞かせて答えを混ぜる
    (2 か所を 1 回で聞かせると、先頭の方だけを答えがちだった)。読めない答えの 1 か所は飛ばし、1 か所も聞き取れなければ LlmError。
    LLM が使えない・音声を読めないときも LlmError、ffmpeg が使えないときは mv_audio.MvAudioError。"""
    heard, why = [], ""
    with tempfile.TemporaryDirectory(prefix="mvlisten_") as tmp:
        for i, start in enumerate(excerpt_starts(duration)):
            wav = await mv_audio.cut(song_path, start, start + EXCERPT_S, Path(tmp) / f"excerpt{i}.wav", rate=LISTEN_RATE, channels=1)
            try:
                heard.append(await _hear(llm, await asyncio.to_thread(wav.read_bytes)))
            except mvscript.ScenarioError as e:       # この 1 か所は読めなかった(もう 1 か所があれば、そちらで)
                why = e.message
                log.warning("曲の %.0f 秒からの聞き取りを使えません: %s", start, why)
    if not heard:
        raise LlmError(f"曲を聞き取れませんでした({why})")
    merged = {}
    for field in LISTEN_FIELDS:
        words = {}
        for h in heard:
            for word in h.get(field, []):
                words.setdefault(word.lower(), word)       # 同じ語は 1 つに(大文字小文字は区別しない。最初の書き方を使う)
        if len(words) > 1:
            words.pop(NO_SOUND.get(field, ""), None)       # 歌のある所があれば、別の所の「歌なし」は捨てる
        if words:
            merged[field] = ", ".join(words.values())
    return merged


# ---- 曲の情報から曲の内容・世界観 ----

def _concept_lines(info: dict, params: Optional[dict]) -> list:
    """LLM に見せる曲の情報(歌詞は入れない)。曲を聞いた結果(info。BPM は MV の分析のもの)を先に、無い項目は台帳の params(音楽タブの曲のテーマ・キャプションなど)で補う。"""
    p, info = params or {}, info or {}

    def pick(*values):
        return next((str(v).strip() for v in values if v not in (None, "", [], False) and str(v).strip()), "")
    bpm, key = pick(info.get("bpm"), p.get("bpm")), pick(p.get("key_scale"))
    tempo = " / ".join(x for x in (pick(info.get("tempo")), f"{bpm} BPM" if bpm else "", f"key {key}" if key else "") if x)
    rows = (("Theme given by the creator", pick(p.get("theme"))), ("Music description", pick(p.get("caption"))[:600]),
            ("Genre", pick(info.get("genre"), p.get("genre"))), ("Mood", pick(info.get("mood"), p.get("mood"))),
            ("Instruments", pick(info.get("instruments"))), ("Tempo and key", tempo),
            ("Vocal language", pick(info.get("language"), p.get("language"))), ("Vocals", pick(info.get("vocal"), p.get("vocal"))),
            ("Instrumental", "yes (no vocals)" if p.get("instrumental") else ""))
    return [f"- {label}: {value}" for label, value in rows if value]


async def propose_concept(llm, info: dict, song_params: Optional[dict] = None) -> str:
    """曲の内容・世界観の案(日本語 2〜3 文: 曲の雰囲気・ジャンルと、合う場面・色・簡単な物語の流れ)を LLM で書く。
    info は曲を聞いた結果(listen_song。language / genre / mood / instruments / vocal / tempo と、あれば MV の分析の bpm)、song_params は曲の台帳の
    params(theme / caption / genre など)。歌詞は使わない。材料が何も無い・LLM の答えが空なら LlmError。"""
    lines = _concept_lines(info, song_params)
    if not lines:
        raise LlmError("曲の情報が無いので、曲の内容を作れません")
    guessed = CONCEPT_GUESSED if any((info or {}).get(f) for f in LISTEN_FIELDS) else ""
    prompt = f"{CONCEPT_PROMPT}{guessed}:\n" + "\n".join(lines) + "\nNOTE:"
    text = ""
    for extra in ("", CONCEPT_RETRY):
        text = await llm.chat(CONCEPT_SYSTEM, prompt + extra, max_tokens=400, temperature=0.7)
        text = re.sub(r"\s*\n\s*", "", re.sub(r"^\s*(?:NOTE|ノート)\s*[:\uff1a]\s*", "", text or "")).strip().strip("「」\"'*")
        if _is_japanese(text):
            break
    if not _is_japanese(text):
        raise LlmError("曲の内容を作れませんでした(LLM が日本語で答えませんでした)")
    return _clip(text, CONCEPT_MAX)
