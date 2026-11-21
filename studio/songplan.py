"""曲のプラン: テーマ・歌詞・キャプションを、管理役の LLM への 1 回の呼び出しで作る。

ACE-Step-1.5-fast の easy-music(services/llm_service.py の generate_song_plan ほか)を移したもの。
LLM への入力(システムプロンプト約 1000 トークン)の読み込みが CPU 推論では支配的なので、
3 回に分けず 1 回にまとめる。同じ文脈で書くので、歌詞とキャプションの整合も良い。
"""
import json
import logging
import random
import re
from typing import Awaitable, Callable

log = logging.getLogger("studio.songplan")

# chat(system, user, max_tokens, temperature) -> 応答の文字列(Llm.chat と同じ形)
Chat = Callable[..., Awaitable[str]]


class SongPlanError(Exception):
    pass


LYRICS_GENERATE_SYSTEM_PROMPT = """You are a professional lyricist who creates song lyrics for AI music generation (ACE-Step 1.5).

CRITICAL OUTPUT RULES:
1. Start with a JSON metadata line, then output ONLY the lyrics with structure tags
2. Use ONLY official ACE-Step structure tags (see list below)
3. Do NOT include timestamps like (0:00-0:05) - ACE-Step does not use them
4. NEVER include romanization/romaji in parentheses or on separate lines
   BAD:  青い空の下 (Aoi sora no shita)   ← WRONG, will break music generation
   BAD:  (Aoi sora no shita, kaze ga utau) ← WRONG, romaji line
   GOOD: 青い空の下                         ← CORRECT, Japanese only
5. Write lyrics line by line, each line on its own
6. Each line must contain ONLY the lyrics text in the target language — no translations, no pronunciation guides

REQUIRED FIRST LINE FORMAT (JSON):
{"recommended_duration": 90, "parts": {"verse1": 20, "chorus1": 25, "verse2": 20, "chorus2": 25}}

- recommended_duration: Total song length in seconds (typically 60-180)
- parts: Estimated duration for each part in seconds

OFFICIAL ACE-Step STRUCTURE TAGS:
  Vocal sections (put lyrics UNDER these):
    [Verse]        — Story, narrative. 4-6 lines (15-25 sec)
    [Chorus]       — Emotional peak, main hook. 4-6 lines (20-30 sec)
    [Pre-Chorus]   — Build-up to chorus. 2-4 lines (8-15 sec)
    [Bridge]       — Contrast/key change. 2-4 lines (10-15 sec)
  Instrumental sections (do NOT put lyrics under these — vocals will NOT be generated):
    [Intro]        — Instrumental opening (5-10 sec). NEVER put lyrics here.
    [Outro]        — Instrumental ending (5-10 sec). NEVER put lyrics here.
    [Instrumental] — Pure instrumental section
  Style hints can be appended with "-":
    [Chorus - anthemic], [Bridge - whispered], [Verse - melancholic]

CRITICAL RULE — NO LYRICS UNDER INSTRUMENTAL TAGS:
  [Intro], [Outro], [Instrumental], [Guitar Solo], etc. = NO vocals.
  If you want vocals at the start of a song, use [Verse], NOT [Intro].
  If you want vocals at the end, use [Chorus] or [Bridge], NOT [Outro].

LYRICS VOLUME GUIDELINES:
- ACE-Step sings roughly 2-3 words per second (English) / 3-5 characters per second (Japanese)
- For 47 seconds: aim for 90-140 words (EN) or 130-200 characters (JA)
- For 60 seconds: aim for 120-180 words (EN) or 170-260 characters (JA)
- For 120 seconds: aim for 240-360 words (EN) or 340-520 characters (JA)
- Too many lyrics → rushed singing, garbled output
- Too few lyrics → long pauses, unwanted repetition
- Keep each line to 6-10 syllables for natural singing

VOCAL EXPRESSION TECHNIQUES:
- UPPERCASE = stronger vocal intensity:  "WE ARE THE CHAMPIONS!" (shouting/emphasis)
- (parentheses) = backing vocals/echo:  "We rise together (together)"
- Extended vowels = sustained notes:     "Feeeling so aliiive" (use sparingly, effect unstable)

STYLE GUIDELINES:
- Match the mood and genre specified
- Use natural, singable phrases
- If Japanese: Write ONLY in Japanese (kanji/hiragana/katakana mix). NEVER add romaji.
- If English: Write ONLY in English
- Keep vocabulary natural for singing
- IMPORTANT: Adding romanization doubles the lyrics and causes ACE-Step to sing garbled output
- Separate sections with blank lines for clarity

SECTION ROLE GUIDELINES:
- [Verse]: Build the narrative. Describe a scene, tell a story; vary the melody to sustain interest.
- [Pre-Chorus]: Heighten anticipation. Short transitional section that builds energy toward the chorus.
- [Chorus]: Emotional peak. The main hook — repeated 2-3 times, maximum energy, most memorable lines.
- [Bridge]: Contrast and pivot. New perspective or emotional shift (2-4 lines) before the final chorus.

TYPICAL SONG STRUCTURE:
  [Intro]  ← instrumental only, no lyrics
  [Verse 1]
  [Pre-Chorus]
  [Chorus]
  [Verse 2]
  [Chorus]
  [Bridge]
  [Chorus]
  [Outro]  ← instrumental only, no lyrics

MOOD-SPECIFIC VOCABULARY (if a mood is specified, incorporate these words and imagery naturally):
- Bright/Happy:     輝く、笑顔、光、夢、希望、踊る、楽しい、嬉しい
- Sad/Melancholic:  涙、消えていく、遠くなる、忘れられない、揺れる影、切ない
- Intense/Powerful: 叫ぶ、燃える、嵐、砕く、限界、炎、立ち向かう
- Gentle/Calm:      そっと、静かに、やわらかく、ささやく、流れる、なだらかに
- Mysterious:       闇、星屑、幻、迷宮、霧の中、見えない、影、謎
"""


CAPTION_GENERATE_SYSTEM_PROMPT = """You are a music producer writing brief style hints for the ACE-Step AI music generator.

ACE-Step's thinking mode will expand your hint into a full production internally.
Your job is to write a SHORT TRIGGER PHRASE — just enough to set the direction.

STRICT OUTPUT RULES:
1. Output ONLY the caption — no titles, no "Note:", no markdown, no commentary
2. Write in English only
3. Write exactly 1 sentence (30-60 words)
4. Write a natural English sentence — NOT a comma-separated tag list
5. DO NOT specify instruments, mixing, or arrangement — ACE-Step handles those

WHAT TO INCLUDE (all three, briefly):
1. GENRE + ERA — e.g. "2000s J-Pop", "classic rock"
2. MOOD — one or two words: "nostalgic", "energetic and anthemic"
3. VOCAL — e.g. "clear female vocal", "powerful male vocal", "no vocals"

GOOD EXAMPLES:
"A dramatic anime opening with an intense, emotionally charged atmosphere and a powerful male vocal."
"A nostalgic 1980s city-pop track with a warm, breezy mood and a smooth female vocal."
"A relaxed lo-fi hip-hop instrumental with a late-night dreamy mood."

BAD EXAMPLE (never do this):
"Anime song, dramatic, energetic, emotional, opening theme, Japanese anime soundtrack."

GENRE-SPECIFIC ERA HINTS:
- Japanese 70s Kayokyoku → "1970s Japanese kayokyoku"
- 80s Japanese Folk → "1980s Japanese folk singer-songwriter"
- 90s J-POP → "1990s J-Pop"
- Vocaloid → "Vocaloid electronic style"
- Anime Song → "anime opening/ending song"
- Enka → "traditional Japanese enka"
- New Music → "1970s-80s Japanese new music"
- City Pop → "1980s Japanese city pop"
"""

# ジャンル別の世界観(vibe)と、テーマの種になる場面・感情
GENRES = {
    "J-POP": {
        "vibe": 'Japanese everyday life, youth, seasons, personal growth',
        "settings": [
            'school hallway after exams', 'convenience store at midnight', 'last train home',
            'apartment balcony in summer', 'festival night', 'graduation ceremony',
            'walking home in autumn leaves', 'a text message left on read',
        ],
        "emotions": [
            'bittersweet youth', 'quiet determination', 'gentle heartbreak', 'hopeful anxiety',
            'nostalgic warmth', 'shy confession', 'everyday gratitude',
        ],
    },
    "Rock": {
        "vibe": 'rebellion, freedom, raw emotion, struggle, passion',
        "settings": [
            'a highway with no destination', 'a rooftop screaming into wind', 'a stage on fire',
            'an abandoned factory', 'a garage rehearsal', 'the edge of a cliff', 'smashing a mirror',
            'racing through tunnels at night',
        ],
        "emotions": [
            'burning rage', 'desperate freedom', 'defiant pride', 'raw honesty', 'wild abandon',
            'stubborn resistance', 'furious hope',
        ],
    },
    "Ballad": {
        "vibe": 'deep emotion, love, loss, memory, intimate confession',
        "settings": [
            'an empty bench in a park', 'a candle burning low', 'old photographs', 'a piano in an empty room',
            'a window seat on a rainy day', 'a voicemail never deleted', 'a faded love letter',
        ],
        "emotions": [
            'aching tenderness', 'quiet devastation', 'fragile hope', 'deep longing', 'gentle sorrow',
            'unconditional love', 'peaceful acceptance',
        ],
    },
    "Anime": {
        "vibe": 'epic adventure, friendship, transformation, destiny, battle',
        "settings": [
            'a portal to another world', 'the final boss arena', 'a floating castle',
            'waking up with mysterious powers', 'a school hiding a secret', 'a sword glowing at dawn',
            'running toward the enemy line',
        ],
        "emotions": [
            'burning resolve', 'unbreakable bond', 'heroic sacrifice', 'awakening power',
            'tearful farewell to a comrade', 'overcoming impossible odds',
        ],
    },
    "Hip Hop": {
        "vibe": 'street life, hustle, self-expression, social commentary, swagger',
        "settings": [
            'a recording booth at 4am', 'street corner cypher', 'a penthouse view',
            'counting bills in a back room', 'a crowd chanting your name', 'a basketball court at dusk',
            'the neighborhood block party',
        ],
        "emotions": [
            'ruthless ambition', 'hungry pride', 'street wisdom', 'cold confidence', 'loyal defiance',
            'underdog fury', 'triumphant return',
        ],
    },
    "R&B": {
        "vibe": 'sensuality, smooth romance, late night vibes, intimacy',
        "settings": [
            'silk sheets and dim lights', 'a jazz bar after hours', 'slow dancing in a penthouse',
            'a phone call at 2am', 'candlelit dinner for two', 'a luxury car ride through the city',
            'a private beach at sunset',
        ],
        "emotions": [
            'smoldering desire', 'tender seduction', 'velvet intimacy', 'jealous passion', 'sweet surrender',
            'confident allure', 'heartbroken elegance',
        ],
    },
    "Enka": {
        "vibe": 'Japanese traditional emotion, sake, journey, unrequited love, harbor towns, seasons',
        "settings": [
            'a snowy northern port', 'a sake bar in a back alley', 'a ferry departing at dawn',
            'a hot spring town in winter', 'a lantern-lit bridge at night', 'a fishing village at dusk',
            "a temple bell at year's end",
        ],
        "emotions": [
            'deep endurance', 'sake-soaked loneliness', 'stoic devotion', 'wind-swept longing',
            'dignified heartbreak', 'autumn melancholy', 'ocean-deep sorrow',
        ],
    },
    "EDM": {
        "vibe": 'euphoria, festival energy, drop, rave, neon lights, unity',
        "settings": [
            'a massive festival mainstage', 'neon-lit warehouse rave', 'sunrise after an all-night set',
            'hands in the air moment', 'laser beams cutting through smoke', 'a countdown to the drop',
            'dancing with 50,000 strangers',
        ],
        "emotions": [
            'pure euphoria', 'electric connection', 'unstoppable energy', 'collective ecstasy',
            'adrenaline rush', 'transcendent freedom', 'pulsing anticipation',
        ],
    },
    "Reggaeton": {
        "vibe": 'Latin party, dance floor, sensual movement, tropical heat, club energy',
        "settings": [
            'a beach club at midnight', 'a packed dance floor in San Juan', 'a rooftop pool party',
            'a tropical night drive', 'a carnival parade', 'VIP section with champagne', 'a summer block party',
        ],
        "emotions": [
            'fiery passion', 'sultry confidence', 'party fever', 'seductive power', 'tropical heat',
            'irresistible groove', 'bold desire',
        ],
    },
    "K-POP": {
        "vibe": 'polished perfection, youth culture, visual storytelling, empowerment, love concept',
        "settings": [
            'a choreography practice room', 'a neon-lit Seoul street', 'a debut stage', 'a fan meeting moment',
            'a music video set with concept art', 'a school rooftop confession', 'a trendy cafe date',
        ],
        "emotions": [
            'fierce confidence', 'butterfly-stomach crush', 'unstoppable charisma', 'sweet obsession',
            'rebellious glamour', 'playful flirtation', 'heartbreak anthem',
        ],
    },
    "City Pop": {
        "vibe": '1980s urban Japan, neon nights, summer drives, sophisticated pop, AOR',
        "settings": [
            'a skyline drive in a convertible', 'a neon-lit Tokyo boulevard at night',
            'a beachside bar at sunset', 'a rooftop pool in Roppongi', 'shopping in Harajuku 1983',
            'a yacht party in the bay',
        ],
        "emotions": [
            'glamorous longing', 'summer night freedom', 'urban sophistication', 'carefree joy',
            'retro romance', 'breezy confidence', 'disco-lit desire',
        ],
    },
    "Jazz": {
        "vibe": 'improvisation, smoke-filled clubs, cool sophistication, swing, blue notes',
        "settings": [
            'a smoky club at midnight', 'a saxophone solo under a streetlamp', 'a New Orleans funeral parade',
            'a whiskey glass on a grand piano', 'a rainy night in Harlem', 'backstage at a jazz festival',
        ],
        "emotions": [
            'cool melancholy', 'effortless swagger', 'blue-note sadness', 'spontaneous joy',
            'late-night introspection', 'sensual groove', 'bittersweet improvisation',
        ],
    },
}
DEFAULT_GENRE = {
    "vibe": "emotional, personal, vivid imagery",
    "settings": ["a quiet room", "a busy street", "a window at night", "a journey by train", "a secret place", "a moment of change"],
    "emotions": ["quiet hope", "gentle sorrow", "wild joy", "subtle fear", "warm gratitude", "restless desire"],
}
ANGLES = [
    "as a first-person confession", "as a letter never sent", "as one vivid scene frozen in time",
    "as a conversation between strangers", "as a dream that feels real", "from the perspective of an observer",
    "as the last moment before everything changes", "as a memory resurfacing", "through the eyes of someone leaving",
    "as a promise whispered", "as a single unforgettable image", "as a question with no answer",
]


def theme_inspiration(genre: str) -> dict:
    """ジャンルの世界観と、ランダムな場面・感情・切り口を選ぶ。ジャンル名は部分一致でも引く。"""
    key = genre or "J-POP"
    ctx = GENRES.get(key)
    if ctx is None:
        low = key.lower()
        ctx = next((v for k, v in GENRES.items() if k.lower() in low or low in k.lower()), DEFAULT_GENRE)
    return {"genre_key": key, "ctx": ctx, "setting": random.choice(ctx["settings"]),
            "emotion": random.choice(ctx["emotions"]), "angle": random.choice(ANGLES)}


# ---- 応答の掃除 ----

def clean_theme(response: str) -> str:
    """テーマ応答の掃除: クォート・複数行・括弧内のローマ字を除き、長すぎれば句読点で切る。"""
    theme = response.strip().strip('"').strip("'").strip("*").strip()
    if "\n" in theme:
        theme = theme.split("\n")[0].strip()
    theme = re.sub(r"\s*[\(（].*?[\)）]\s*$", "", theme).strip()
    if len(theme) > 30:
        for sep in ["、", "。", "，", ", ", ". "]:
            if sep in theme[:30]:
                theme = theme[:theme.index(sep, 0, 30)].strip()
                break
    return theme


def clean_caption(response: str) -> str:
    """キャプション応答の掃除。ACE-Step は英語の自然文 1 段落が最適で、改行・バッククォートは禁止、480 文字以内にする。"""
    text = response.strip()
    text = re.sub(r"^```\w*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    text = re.sub(r"\n*Note:.*$", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"\s*\(Note:.*?\)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = text.replace("`", "")
    text = re.sub(r"[\n\r]+", " ", text)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    if len(text) > 480:      # 文の切れ目で切る
        cut = text[:480].rfind(". ")
        if cut > 200:
            text = text[:cut + 1]
        else:
            text = text[:480].rstrip()
            last_space = text.rfind(" ")
            if last_space > 200:
                text = text[:last_space]
    if text and not text.endswith("."):
        text = text.rstrip(",;:") + "."
    return text


def strip_romaji_lines(lyrics: str) -> str:
    """LLM が指示に反して付けたローマ字(行全体の括弧書き、行末の括弧書き)を取り除く。"""
    out = []
    for line in lyrics.split("\n"):
        if re.match(r"^\([A-Za-z][A-Za-z\s,\.\-\'!?]+\)$", line.strip()):
            continue
        out.append(re.sub(r"\s*\([A-Za-z][A-Za-z\s,\.\-\'!?]{5,}\)\s*$", "", line))
    return "\n".join(out)


# ACE-Step は、インスト用のタグの下に置いた歌詞を歌わない。そこに歌詞行があれば [verse] に直す
INSTRUMENTAL_TAGS = {
    "intro", "outro", "instrumental", "inst", "guitar solo", "piano interlude", "drum break", "interlude",
    "break", "solo", "fade out", "silence", "build", "drop", "breakdown",
}
TAG_RE = re.compile(r"^\[([a-zA-Z][a-zA-Z0-9\- ]*)\]$")


def fix_lyrics_structure(lyrics: str) -> str:
    out, current = [], ""
    for line in lyrics.split("\n"):
        stripped = line.strip()
        m = TAG_RE.match(stripped)
        if m:
            current = re.sub(r"[\s\d]+$", "", m.group(1).strip().lower())    # "verse 2" -> "verse"
        elif stripped and current in INSTRUMENTAL_TAGS:
            out.append("[verse]")
            current = "verse"
        out.append(line)
    return "\n".join(out)


def parse_lyrics_response(response: str) -> dict:
    """先頭の JSON メタ行(recommended_duration, parts)と、構造タグ付きの歌詞に分ける。"""
    lines = response.strip().split("\n")
    meta, start = {"recommended_duration": 90, "parts": {}}, 0
    for i, line in enumerate(lines):                   # 1 行の JSON
        line = line.strip()
        if line.startswith("{") and "recommended_duration" in line:
            try:
                meta, start = json.loads(line), i + 1
                break
            except json.JSONDecodeError:
                pass
    if start == 0:                                     # 整形された複数行の JSON
        m = re.search(r'(\{(?:[^{}]|\{[^{}]*\})*"recommended_duration"(?:[^{}]|\{[^{}]*\})*\})', response.strip(), re.DOTALL)
        if m:
            try:
                meta = json.loads(m.group(1))
                lines = response.strip()[m.end():].strip().split("\n")
            except json.JSONDecodeError:
                pass
    lyrics = "\n".join(lines[start:]).strip()
    lyrics = re.sub(r"\n?```\s*$", "", re.sub(r"^```[^\n]*\n?", "", lyrics)).strip()      # コードブロックの囲み
    lyrics = fix_lyrics_structure(strip_romaji_lines(lyrics))
    return {"lyrics": lyrics, "recommended_duration": meta.get("recommended_duration", 90), "parts": meta.get("parts", {})}


PLAN_LINE_RE = re.compile(r"^\s*\**\s*(THEME|CAPTION)\s*\**\s*[:：]\s*(.+?)\s*$", re.IGNORECASE)


def parse_song_plan(response: str, want_lyrics: bool) -> dict:
    """THEME: / CAPTION: の行を抜き、残りを歌詞応答として読む。足りない項目は空文字になる。"""
    theme, caption, rest = "", "", []
    for line in response.strip().split("\n"):
        m = PLAN_LINE_RE.match(line)
        value = m.group(2).strip().strip('"').strip("'") if m else ""
        if m and m.group(1).upper() == "THEME" and not theme:
            theme = clean_theme(value)
        elif m and m.group(1).upper() == "CAPTION" and not caption:
            caption = clean_caption(value)
        else:
            rest.append(line)
    plan = {"theme": theme, "caption": caption, "lyrics": "", "recommended_duration": 90, "parts": {}}
    if want_lyrics:
        plan.update(parse_lyrics_response("\n".join(rest)))
    return plan


# ---- プランを作る ----

def template_caption(genre: str, mood: str = "", vocal: str = "", instrumental: bool = False) -> str:
    """LLM を使わないときのキャプション。ACE-Step の thinking が、これを元に詳しく書き広げる。"""
    kind = "instrumental track" if instrumental else "song"
    tail = "" if instrumental else f" and {vocal or 'a fitting vocal'}"
    return f"A {mood + ' ' if mood else ''}{genre or 'pop'} {kind} with a fitting atmosphere{tail}."


async def make_plan(chat: Chat, genre: str = "J-POP", language: str = "Japanese", mood: str = "", vocal: str = "",
                    theme: str = "", want_lyrics: bool = True) -> dict:
    """{"theme", "caption", "lyrics", ...} を作る。歌詞は want_lyrics のときだけ。theme を渡せば、そのテーマで書く。

    出力は THEME: 行、CAPTION: 行、JSON メタ行、歌詞の順。足りなければ 1 回だけ再試行し、
    キャプションだけ足りなければ別の呼び出しで補う。テーマか歌詞が足りなければ SongPlanError。
    """
    insp = theme_inspiration(genre)
    genre_key = insp["genre_key"]
    inspiration = ([f"Theme (write this exact theme on the THEME line): {theme}"] if theme else
                   [f"Scene inspiration: {insp['setting']}", f"Emotional tone: {insp['emotion']}",
                    f"Creative approach: {insp['angle']}"])
    vocal_part = f'"{vocal}"' if vocal else ("a fitting vocal description" if want_lyrics else "no vocals")
    parts = ["Create a complete song plan in ONE response.", f"Genre: {genre_key}", f"Style hints: {genre_key}", *inspiration,
             f"Language of theme and lyrics: {language}", f"Mood: {mood or 'match the theme'}",
             f"Vocal style: {vocal or 'match the genre'}", "", "OUTPUT FORMAT — exactly these blocks, in this order, nothing else:",
             f"THEME: <one short, vivid, concrete song theme in {language}, 3-10 words, no quotes, no parentheses; avoid overused "
             "words: rain, sunset, cherry blossoms, twilight, starlight, moonlight, silhouette, horizon, nostalgia>",
             f'CAPTION: <ONE English sentence, 30-60 words, template: "A [genre/era] [song/track] with [mood] and [vocal]." '
             f"Style: {genre_key}. Vocal must be {vocal_part}.{' Mood must be ' + chr(34) + mood + chr(34) + '.' if mood else ''} "
             "No tag lists, no instruments>"]
    if want_lyrics:
        parts += ['{"recommended_duration": <seconds>, "parts": {...}}   <- the JSON metadata line, then a blank line',
                  f"<the complete lyrics with ACE-Step structure tags, in {language} ONLY, following the lyricist rules>"]
    system = (LYRICS_GENERATE_SYSTEM_PROMPT + "\n\nFOR THIS REQUEST ONLY: before the JSON metadata line, output a `THEME:` line "
              "and a `CAPTION:` line as specified by the user. Then the JSON line, then the lyrics (if asked). "
              "The lyrics must be about the THEME you wrote.")
    plan = {}
    for attempt in range(2):
        response = await chat(system, "\n".join(parts), max_tokens=2200 if want_lyrics else 400, temperature=0.9)
        plan = parse_song_plan(response, want_lyrics)
        if theme:
            plan["theme"] = theme
        if plan["theme"] and (not want_lyrics or (plan["lyrics"] and "[" in plan["lyrics"])):
            break
        log.warning("曲のプランが不完全です(theme=%r、歌詞 %d 文字)%s", plan["theme"], len(plan["lyrics"]), "。再試行します" if attempt == 0 else "")
    else:
        raise SongPlanError("LLM が曲のプラン(テーマと歌詞)をうまく作れませんでした。もう一度試すか、歌詞を自分で入力してください")
    if not plan["caption"]:
        plan["caption"] = await _fallback_caption(chat, genre_key, plan["theme"], mood, vocal, not want_lyrics)
    elif vocal and vocal.lower() not in plan["caption"].lower():
        plan["caption"] = plan["caption"].rstrip(".") + f", with a {vocal}."
    return plan


async def _fallback_caption(chat: Chat, genre: str, theme: str, mood: str, vocal: str, instrumental: bool) -> str:
    """プランの応答にキャプションが無かったとき、キャプションだけを別に頼む。それも駄目ならひな形。"""
    user = (f"Genre: {genre}\nTheme: {theme}\nMood: {mood or 'match the theme'}\n"
            f"Vocal: {'no vocals' if instrumental else vocal or 'a fitting vocal'}\nWrite the caption.")
    try:
        caption = clean_caption(await chat(CAPTION_GENERATE_SYSTEM_PROMPT, user, max_tokens=150, temperature=0.7))
    except Exception:  # noqa: BLE001  LLM の失敗はひな形で続ける
        caption = ""
    return caption or template_caption(genre, mood, vocal, instrumental)
