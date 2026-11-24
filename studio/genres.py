"""ジャンル: 音楽タブのジャンルの定義(同梱の 12 と、自作)。

同梱のジャンルは songplan.GENRES(世界観の一行 vibe・場面 settings・感情 emotions)。自作のジャンルは、名前(と任意の一行の説明)から
管理役の LLM が同じ形の定義を作り、台帳の genres 表に保存する。定義は songplan.theme_inspiration がテーマの種(場面・感情)を選ぶのに使う。
viz は、音楽のカードのスペクトラムの雰囲気(web/viz.js の 10 種)。songplan は台帳を知らないので、呼ぶ側が custom_map(ledger) を渡す。
"""
import json
import logging
import re
import threading
import unicodedata
from typing import Optional

from . import songplan
from .llm import LlmError

log = logging.getLogger("studio.genres")

GENRE_MAX = 200              # ジャンルの文字数の上限(チップの名前も、自由に書いた文も。自作のジャンルの説明も)
MAX_NAME = songplan.GENRE_NAME_MAX
SETTINGS_COUNT, EMOTIONS_COUNT = 8, 7       # LLM に作らせる数(同梱は 6〜8 個)
MIN_SEEDS = 4                # LLM の答えの種がこれより少なければ使えない(これ以上なら、足りないぶんを既定のジャンルの種で補う)
MAX_SEEDS = 20               # 手で書き換えるときの数の上限
MAX_SEED = 80                # LLM の答えの種 1 つの長さ(超えれば語の切れ目で切る)
MAX_SEED_EDIT = 100          # 手で書き換えるときの種 1 つの長さの上限
MAX_VIBE = 200
VIZ_IDS = ("energetic", "melancholic", "healing", "epic", "cyber", "romantic", "dark", "festive", "nostalgic", "mystical")     # web/viz.js の VIZ_MOOD_THEMES
VIZ_DEFAULT = "energetic"
BUILTIN = "builtin:"         # 同梱のジャンルの ID の頭(builtin:<名前>)
LOCKED = "同梱のジャンルは変更できません"
NOT_FOUND = "そのジャンルは見つかりません(削除されたかもしれません)"

_lock = threading.Lock()     # 名前の重なりの確認と登録を、同時に走らせない


class GenreError(Exception):
    """利用者に見せる失敗(日本語の理由)。status は HTTP ステータス。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


# ---- スペクトラムの雰囲気 ----

# web/viz.js の GENRE_TO_VIZ と同じ規則(ジャンル名 → 雰囲気。tests/test_genres.py が一致を見る)。同梱のジャンルの雰囲気と、LLM が雰囲気を外したときの代わり
GENRE_VIZ_RULES = [
    (re.compile(r"ballad|enka|folk|kayokyoku|new music", re.I), "nostalgic"),
    (re.compile(r"rock|metal|punk", re.I), "energetic"),
    (re.compile(r"edm|techno|house|trance|phonk|vocaloid", re.I), "cyber"),
    (re.compile(r"jazz|lo-?fi|chill|ambient|bossa", re.I), "healing"),
    (re.compile(r"r&b|soul|city pop", re.I), "romantic"),
    (re.compile(r"anime|game|orchestr|cinematic", re.I), "epic"),
    (re.compile(r"hip ?hop|rap|reggaeton|latin", re.I), "festive"),
    (re.compile(r"j-?pop|k-?pop|pop|idol", re.I), "energetic"),
]


def derive_viz(*texts: str) -> str:
    """ジャンルの名前(など)から雰囲気を決める。最初に規則に当たった文字列で決め、どれも当たらなければ energetic。"""
    for text in texts:
        for rule, viz in GENRE_VIZ_RULES:
            if rule.search(text or ""):
                return viz
    return VIZ_DEFAULT


# ---- 検証 ----

def clean_genre(text) -> str:
    """音楽タブで使うジャンル(チップの名前か、自由に書いた文)を 1 行にそろえる。空なら ""。長すぎれば ValueError(日本語)。"""
    text = " ".join(str(text or "").split())
    if len(text) > GENRE_MAX:
        raise ValueError(f"genre は {GENRE_MAX} 文字以内にしてください")
    return text


def check_name(name) -> str:
    """前後の空白を削り、途中の空白は 1 つにして、1〜40 文字か確かめる。"""
    text = " ".join(name.split()) if isinstance(name, str) else ""
    if not 1 <= len(text) <= MAX_NAME:
        raise GenreError(400, f"名前は 1〜{MAX_NAME} 文字で入力してください")
    if not text.isprintable():          # 制御文字や、見えない文字(ゼロ幅の空白など)
        raise GenreError(400, "名前に使えない文字があります")
    return text


def check_description(text) -> str:
    text = " ".join(str(text or "").split())
    if len(text) > GENRE_MAX:
        raise GenreError(400, f"説明は {GENRE_MAX} 文字以内にしてください")
    return text


def check_vibe(vibe) -> str:
    text = " ".join(vibe.split()) if isinstance(vibe, str) else ""
    if not 1 <= len(text) <= MAX_VIBE:
        raise GenreError(400, f"世界観(vibe)は 1〜{MAX_VIBE} 文字で入力してください")
    return text


def check_seeds(items, label: str) -> list:
    """手で書き換えた場面・感情の配列を確かめる(空の項目と、大文字小文字だけ違う重なりは除く)。"""
    if not isinstance(items, list) or not all(isinstance(x, str) for x in items):
        raise GenreError(400, f"{label}は文字の配列にしてください")
    out, seen = [], set()
    for x in items:
        text = " ".join(x.split())
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
    if not 1 <= len(out) <= MAX_SEEDS:
        raise GenreError(400, f"{label}は 1〜{MAX_SEEDS} 個にしてください")
    if any(len(x) > MAX_SEED_EDIT for x in out):
        raise GenreError(400, f"{label}は 1 つ {MAX_SEED_EDIT} 文字以内にしてください")
    return out


def check_viz(viz) -> str:
    text = str(viz or "").strip().lower()
    if text not in VIZ_IDS:
        raise GenreError(400, "viz は " + " / ".join(VIZ_IDS) + " のいずれかにしてください")
    return text


def _key(name: str) -> str:
    """名前の重なりを見るときの形(大文字小文字と、全角・半角の違いを無視する)。"""
    return unicodedata.normalize("NFKC", name).casefold()


def _check_free(ledger, name: str, exclude: str = None) -> None:
    """同梱と、ほかの自作のジャンルに同じ名前が無いか。あれば 409。exclude は、名前を変える自分自身の ID。"""
    key = _key(name)
    if any(_key(n) == key for n in songplan.GENRES) or any(g["id"] != exclude and _key(g["name"]) == key for g in ledger.list_genres()):
        raise GenreError(409, f"「{name}」という名前のジャンルがすでにあります")


# ---- 定義を LLM で作る ----

EXAMPLE_GENRES = ("J-POP", "Rock")      # プロンプトに見せる同梱の定義(場面 8・感情 7 のもの)


def _example(name: str) -> str:
    d = songplan.GENRES[name]
    return f"Genre name: {name}\n" + json.dumps({"vibe": d["vibe"], "settings": d["settings"], "emotions": d["emotions"], "viz": derive_viz(name)},
                                                  ensure_ascii=False)


DEFINITION_SYSTEM_PROMPT = """You are a music genre expert. Write a compact definition of a music genre for an AI songwriting tool.
The tool picks one scene and one emotion at random from your lists to inspire the theme of a song in this genre.

OUTPUT: ONE JSON object and nothing else - no markdown, no code fence, no commentary.
{"vibe": "<one line>", "settings": ["<8 items>"], "emotions": ["<7 items>"], "viz": "<one word>"}

RULES:
1. "vibe": ONE English line of 5-10 comma-separated keywords or short phrases: the sound, era, themes, imagery and attitude of the genre
2. "settings": EXACTLY 8 concrete scenes or places where a song in this genre could be set, 3-8 words each ("a convenience store at midnight"). Specific nouns, never abstract ideas
3. "emotions": EXACTLY 7 emotional tones, 2-4 words each ("bittersweet youth", "burning rage"), all different from each other
4. "viz": the ONE word that best fits the mood of the genre's music: energetic (upbeat, driving), melancholic (sad, wistful), healing (calm, soothing), epic (grand, heroic), cyber (electronic, futuristic), romantic (tender, sensual), dark (heavy, gloomy), festive (party, celebration), nostalgic (retro, traditional), mystical (dreamy, otherworldly)
5. The genre name and the description may be in Japanese or another language. Understand them, but write EVERY value in English: no Japanese, no romanization, no translation in parentheses
6. If a description is given, the definition must reflect it. Never just repeat the genre name, and do not copy the examples

EXAMPLES (existing definitions):
""" + "\n\n".join(_example(n) for n in EXAMPLE_GENRES)

RETRY_SUFFIX = ("\n\nYour previous reply could not be used. Reply with ONLY the JSON object: start with { and end with }, "
                "English values only, exactly 8 settings and 7 emotions.")

_CJK = re.compile(r"[⺀-鿿가-힯豈-﫿＀-￯]")       # 漢字・かな・ハングル・全角(英語のはずの値に混ざったら使わない)


def extract_json(text: str) -> Optional[dict]:
    """応答から JSON の辞書を取り出す。コードブロックの囲みや、前後の説明があってもよい。末尾のカンマは許す。読めなければ None。"""
    text = re.sub(r"```[a-zA-Z]*", "", text or "")
    decoder = json.JSONDecoder()
    for candidate in (text, re.sub(r",\s*([}\]])", r"\1", text)):
        for m in re.finditer(r"\{", candidate):
            try:
                obj, _ = decoder.raw_decode(candidate, m.start())
            except ValueError:
                continue
            if isinstance(obj, dict):
                return obj
    return None


def _clean_text(value, limit: int) -> str:
    """文字列 1 つを整える: 空白を 1 つにし、箇条書き・番号・引用符を削り、limit を超えれば語の切れ目で切る。文字列でなければ空。"""
    if not isinstance(value, str):
        return ""
    text = re.sub(r"^(?:[-*•・]|\d+[.)])\s+", "", " ".join(value.split()))
    if len(text) > 1 and text[0] == text[-1] and text[0] in "\"'`":        # 全体を囲む引用符
        text = text[1:-1].strip()
    if len(text) > limit:
        cut = text[:limit]
        i = cut.rfind(" ")
        text = (cut[:i] if i > limit // 2 else cut).rstrip(" ,;:-")
    return text


def _clean_items(value, limit: int) -> list:
    """LLM の答えの種のリスト(配列、または改行・「;」区切りの文字列)。空・重複(大文字小文字は無視)・日本語の混ざったものは除く。"""
    if isinstance(value, str):
        value = re.split(r"[\n;]", value)
    if not isinstance(value, list):
        return []
    out, seen = [], set()
    for v in value:
        text = _clean_text(v, limit)
        if text and not _CJK.search(text) and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
    return out


def _fit(items: list, count: int, filler: list) -> list:
    """count 個にそろえる。多ければ先頭から count 個、少なければ既定のジャンルの種(filler)のうち重ならないものを足す。"""
    out = items[:count]
    seen = {x.lower() for x in out}
    for x in filler:
        if len(out) >= count:
            break
        if x.lower() not in seen:
            seen.add(x.lower())
            out.append(x)
    return out


def parse_definition(response: str, name: str = "") -> Optional[dict]:
    """LLM の応答から定義 {vibe, settings, emotions, viz} を取り出して整える。読めない・中身が足りないときは None。
    種の数は SETTINGS_COUNT / EMOTIONS_COUNT にそろえ、viz が 10 種に無ければ名前と世界観から決める。"""
    obj = extract_json(response)
    if obj is None:
        return None
    vibe = obj.get("vibe")
    vibe = _clean_text(", ".join(str(v) for v in vibe) if isinstance(vibe, list) else vibe, MAX_VIBE).rstrip(".")
    settings, emotions = _clean_items(obj.get("settings"), MAX_SEED), _clean_items(obj.get("emotions"), MAX_SEED)
    if not vibe or _CJK.search(vibe) or len(settings) < MIN_SEEDS or len(emotions) < MIN_SEEDS:
        return None
    if len(settings) < SETTINGS_COUNT or len(emotions) < EMOTIONS_COUNT:
        log.info("ジャンル「%s」の種が少ないので、既定のジャンルの種で補います(場面 %d、感情 %d)", name, len(settings), len(emotions))
    viz = str(obj.get("viz") or "").strip().lower()
    return {"vibe": vibe, "settings": _fit(settings, SETTINGS_COUNT, songplan.DEFAULT_GENRE["settings"]),
            "emotions": _fit(emotions, EMOTIONS_COUNT, songplan.DEFAULT_GENRE["emotions"]),
            "viz": viz if viz in VIZ_IDS else derive_viz(name, vibe)}


async def generate_definition(chat: songplan.Chat, name: str, description: str = "") -> dict:
    """ジャンルの定義 {vibe, settings, emotions, viz} を LLM に作らせる(同梱のジャンルと同じ形で、値は英語)。
    名前と説明は日本語でもよい。答えが読めない・中身が足りないときは、「JSON だけ」と念を押して 1 回だけ作り直す。
    LLM が使えない・2 回とも駄目なときは GenreError(日本語の理由)。"""
    user = f"Genre name: {name}\n" + (f"Description: {description}\n" if description else "") + "\nWrite the JSON definition:"
    for attempt in range(2):
        try:
            response = await chat(DEFINITION_SYSTEM_PROMPT, user + (RETRY_SUFFIX if attempt else ""), max_tokens=700,
                                  temperature=0.3 if attempt else 0.7)
        except LlmError as e:
            raise GenreError(503, f"ジャンルの定義を作れませんでした(LLM が使えません): {e.message}")
        definition = parse_definition(response, name)
        if definition:
            return definition
        log.warning("ジャンル「%s」の定義を読めません%s: %r", name, "。作り直します" if attempt == 0 else "", response[:200])
    raise GenreError(503, "LLM がジャンルの定義をうまく作れませんでした。もう一度試してください")


# ---- 一覧・作成・変更・削除 ----

def _public(rec: dict) -> dict:
    return {"id": rec["id"], "name": rec["name"], "vibe": rec["vibe"], "settings": rec["settings"], "emotions": rec["emotions"],
            "viz": rec["viz"], "description": rec["description"], "builtin": False}


def builtin_genres() -> list:
    """同梱のジャンル(songplan.GENRES の順)。雰囲気は、画面が名前から決めるものと同じ。"""
    return [{"id": BUILTIN + name, "name": name, "vibe": d["vibe"], "settings": list(d["settings"]), "emotions": list(d["emotions"]),
             "viz": derive_viz(name), "description": "", "builtin": True} for name, d in songplan.GENRES.items()]


def list_genres(ledger) -> list:
    """同梱のジャンル(builtin: true)、自作のジャンル(作った順)の順。"""
    return builtin_genres() + [_public(g) for g in ledger.list_genres()]


def custom_map(ledger) -> dict:
    """自作のジャンル {名前: 定義}。songplan.theme_inspiration などに custom として渡す。"""
    return {g["name"]: g for g in ledger.list_genres()}


def _own(ledger, gid: str) -> dict:
    """自作のジャンルの行。同梱のものは変更できない(400)。無ければ 404。"""
    if str(gid).startswith(BUILTIN):
        if str(gid)[len(BUILTIN):] in songplan.GENRES:
            raise GenreError(400, LOCKED)
        raise GenreError(404, NOT_FOUND)
    rec = ledger.get_genre(gid)
    if rec is None:
        raise GenreError(404, NOT_FOUND)
    return rec


async def create_genre(ledger, chat: songplan.Chat, name, description="") -> dict:
    """名前(と任意の説明)から LLM で定義を作り、自作のジャンルとして保存する。名前が同梱・ほかの自作と重なれば、LLM を呼ぶ前に 409。"""
    name, description = check_name(name), check_description(description)
    with _lock:
        _check_free(ledger, name)
    definition = await generate_definition(chat, name, description)
    with _lock:
        _check_free(ledger, name)          # 作っている間に、同じ名前ができたかもしれない
        return _public(ledger.create_genre(name, definition["vibe"], definition["settings"], definition["emotions"], definition["viz"], description))


def update_genre(ledger, gid: str, name=None, vibe=None, settings=None, emotions=None, viz=None) -> dict:
    """自作のジャンルの、渡した項目だけを書き換える(手での編集。LLM は呼ばない)。同梱のジャンルは変更できない。
    名前を変えるときは、同梱・ほかの自作と同じ名前にはできない(409)。"""
    with _lock:
        rec = _own(ledger, gid)
        fields = {}
        if name is not None:
            fields["name"] = check_name(name)
            if fields["name"] != rec["name"]:
                _check_free(ledger, fields["name"], exclude=gid)
        if vibe is not None:
            fields["vibe"] = check_vibe(vibe)
        if settings is not None:
            fields["settings"] = check_seeds(settings, "場面")
        if emotions is not None:
            fields["emotions"] = check_seeds(emotions, "感情")
        if viz is not None:
            fields["viz"] = check_viz(viz)
        if not fields:
            raise GenreError(400, "変更する項目を指定してください")
        return _public(ledger.update_genre(gid, **fields))


def delete_genre(ledger, gid: str) -> None:
    with _lock:
        _own(ledger, gid)
        ledger.delete_genre(gid)
