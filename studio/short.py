"""ショート動画(第 3 版)の管理役: 入力の検証、シーンの切り方、次にやること。制作の進め方(一時停止・再開・中止・やり直し・結合し直し・削除・進み)は
MV と同じ mv.Director で、ここの ShortDirector は、その種類ごとの違い(kind = "short"、ジョブ short.*、入力の検証、次にやること、画面に出す項目)だけを持つ。

流れ(それぞれが 1 つのジョブ。時間軸は作成時に決まる):
  speech(セリフあり。人物の画像が無ければ voiceover = 画面の外の声で、景色だけのシーン): short.analyze(読み上げを合成 → 文の切れ目で 5〜10 秒に束ねる)→ short.script → short.scene × N → short.assemble
  music(曲だけ)    : short.analyze(MV と同じ。Demucs で分離して曲を分ける)→ short.script → short.scene × N → short.assemble
  free(どちらも無し) : short.analyze(長さを置くだけ)→ short.plan(物語からシーンに割る)→ short.script → short.scene × N → short.assemble
"""
import math
import re
from pathlib import Path
from typing import Optional

from . import mv, shortscript
from .llm import LlmError
from .mv import CHARACTER_MAX, Director, _clip, _number, _output, pick_song, resolve_format

KIND = "short"
TIMELINES = ("speech", "music", "free")
ROLES = ("person", "scenery")
ROLE_LABEL = {"person": "人物", "scenery": "景色・物"}
FINAL_AUDIOS = ("auto", "none")
IMAGES_MAX = 4                                  # 参照画像の枚数(人物は 1 枚まで)
LENGTH_MIN_S, LENGTH_DEFAULT_S, LENGTH_WARN_S = 30.0, 60.0, 300.0     # 曲もセリフも無いときの長さ(最短・既定)。5 分を超えたら画面が警告する
LENGTH_MAX_S = 3600.0                           # 同じく最長(1 時間。シーン割りを 1 回の LLM の答えで書かせるので、これより長いと答えが収まらない)
STORY_MAX, NARRATION_MAX = 4000, 4000

STAGE_LABEL = {**mv.STAGE_LABEL, "analyze": "音声の準備"}
TEXT = {"analyze": "音声を準備します", "plan": "物語からシーンに分けます", "script": "演出を書きます", "assemble": "シーンを結合します",
        "done": "ショート動画ができました"}
PARAM_KEYS = ("kind", "story", "song_id", "narration", "speaker_id", "images", "length_s", "timeline", "voiceover", "aspect", "quality", "width", "height",
              "min_s", "max_s", "seed", "background_text", "style", "final_audio", "subtitles", "character", "character_source", "character_note", "title")
SCENE_KEYS = (*Director.SCENE_KEYS, "place", "summary", "refs", "speech", "text")


def work_dir(outputs_dir, pid: str) -> Path:
    return Path(outputs_dir) / "short" / pid


# ---- 入力の検証と解決 ----

def timeline_of(narration: str, song_id) -> str:
    """時間軸: セリフがあれば speech、無くて曲があれば music、どちらも無ければ free。"""
    return "speech" if narration else "music" if song_id else "free"


def _images(ledger, raw) -> list:
    """参照画像(0〜4 枚)を検証して、{id, role, description}(人物が先頭、以降は景色・物。同じ役割の中は渡された順)にする。"""
    if raw in (None, ""):
        raw = []
    if not isinstance(raw, (list, tuple)) or any(not isinstance(it, dict) for it in raw):
        raise ValueError("参照画像は {id, role} の並びで指定してください")
    if len(raw) > IMAGES_MAX:
        raise ValueError(f"参照画像は {IMAGES_MAX} 枚までです(いま {len(raw)} 枚)")
    out = []
    for n, it in enumerate(raw, start=1):
        role = str(it.get("role") or "")
        if role not in ROLES:
            raise ValueError(f"参照画像 {n} の役割は「{ROLE_LABEL['person']}」(person)か「{ROLE_LABEL['scenery']}」(scenery)で指定してください")
        rec = _output(ledger, it.get("id"), "image", f"参照画像 {n}", "画像")
        typed, prompt = _clip(str(it.get("description") or ""), CHARACTER_MAX), _clip(mv.description_prompt(rec["params"]), CHARACTER_MAX)
        out.append({"id": rec["id"], "role": role, "description": typed or prompt, "_typed": bool(typed), "_prompt": prompt})
    if sum(1 for it in out if it["role"] == "person") > 1:
        raise ValueError("人物の画像は 1 枚までです(複数の出演者は扱いません)")
    return sorted(out, key=lambda it: it["role"] != "person")       # sorted は安定。人物があれば先頭


def first_sentence(text: str, limit: int = 24) -> str:
    """文章の最初の文(行・句点・感嘆符・疑問符まで。末尾の句点は付けない)を limit 字までに切る。"""
    line = next((ln.strip() for ln in str(text or "").splitlines() if ln.strip()), "")
    return _clip(re.split(r"[。！？!?]", line, 1)[0].strip(), limit)


def resolve_params(ledger, params: dict) -> dict:
    """画面・API から来た入力を検証し、時間軸(speech / music / free)を決めて、制作の params にする。不正なら ValueError(日本語)。
    人物の見た目(character)は、画面の説明か画像のプロンプト(出どころは character_source = typed / image)。空なら short.script の最初に画像を見て作る。"""
    images = _images(ledger, params.get("images"))
    song_id = str(params.get("song_id") or "").strip()
    song = pick_song(ledger, song_id) if song_id else None
    narration = str(params.get("narration") or "").strip()
    if len(narration) > NARRATION_MAX:
        raise ValueError(f"セリフ・ナレーションは {NARRATION_MAX} 文字までです(いま {len(narration)} 文字)")
    story = _clip(str(params.get("story") or ""), STORY_MAX)
    person = next((it for it in images if it["role"] == "person"), None)
    speaker_id = params.get("speaker_id")
    # 人物の画像が無いセリフは、画面の外の声(ボイスオーバー): 時間軸は speech のまま(長さ = 読み上げ、文の切れ目で切る)、シーンは景色だけで口を合わせない
    voiceover = bool(narration) and person is None
    if narration:
        if speaker_id in (None, ""):
            raise ValueError("セリフ・ナレーションを読み上げる話者を選んでください")
    if speaker_id in (None, ""):
        speaker_id = None
    else:
        try:
            speaker_id = int(speaker_id)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("話者は整数(AivisSpeech のスタイル ID)で指定してください")
    if not story and not song and not narration:
        raise ValueError("物語・映像のイメージを書いてください(曲もセリフも無いときは必須です)")
    timeline = timeline_of(narration, song["id"] if song else None)
    length_s = None
    if timeline == "free":
        length_s = _number(params.get("length_s"), "長さ", LENGTH_DEFAULT_S)       # NaN・無限大は _number が断る
        if not LENGTH_MIN_S <= length_s <= LENGTH_MAX_S:
            raise ValueError(f"長さは {LENGTH_MIN_S:g}〜{LENGTH_MAX_S:g} 秒で指定してください")
    final_audio = str(params.get("final_audio") or "auto")
    if final_audio not in FINAL_AUDIOS:
        raise ValueError("最終の音声は " + " / ".join(FINAL_AUDIOS) + " のいずれかです")
    subtitles = params.get("subtitles", False)          # セリフのテロップ(字幕)を焼き込む。セリフが無ければ何もしない
    if not isinstance(subtitles, bool):
        raise ValueError("subtitles は true / false で指定してください")
    fmt = resolve_format(params)
    typed_character = _clip(str(params.get("character") or ""), CHARACTER_MAX) or (person["description"] if person and person["_typed"] else "")
    character = typed_character or (person["_prompt"] if person else "")
    # 仮の題: 物語の最初の文(句点まで、24 字まで)→ 曲のテーマ → 読み上げの最初の文。筋ができたら、その題に置き換える(_prepare)
    title = (first_sentence(story) or _clip(str((song or {}).get("params", {}).get("theme") or Path((song or {}).get("filename") or "").stem), 24)
             or first_sentence(narration) or "ショート動画")
    return {"kind": KIND, "story": story, "song_id": song["id"] if song else None, "narration": narration, "speaker_id": speaker_id,
            "images": [{k: it[k] for k in ("id", "role", "description")} for it in images], "length_s": length_s, "timeline": timeline, "voiceover": voiceover, **fmt,
            "final_audio": final_audio, "subtitles": subtitles, "character": character, "character_source": "typed" if typed_character else "image" if character else "",
            "character_note": "", "title": title}


def person_index(params: dict) -> Optional[int]:
    """params.images の中の人物の添字(無ければ None。人物が先頭に並ぶので、あれば 0)。"""
    return next((i for i, it in enumerate(params.get("images") or []) if it.get("role") == "person"), None)


# ---- セリフ(読み上げ)の文とシーンの切り方 ----

def split_sentences(text: str) -> list:
    """読み上げの文に分ける(句点・感嘆符・疑問符の後と、改行で切る。区切りの記号は残す)。Minimax-H3-lipsync-mv の split_sentences と同じ。"""
    normalized = re.sub(r"[ \t]+", " ", (text or "").strip())
    parts = re.split(r"(?<=[。！？!?])(?:[　 \t]*|\n+)|\n+", normalized)
    return [part.strip() for part in parts if part.strip()]


SPLIT_PENALTY = 10_000.0         # 最短に満たないシーン 1 つにかける罰(最後のシーンが短いときなど、どうしても満たせないときだけ残る)


def plan_speech_scenes(sentences: list, min_s: float, max_s: float) -> list:
    """読み上げの文 [{"text", "start", "end"}](つながっている)を、文の切れ目で min_s〜max_s 秒のシーンに束ねる(動的計画法)。
    各シーンの長さが目標(最短と最長の真ん中)に近い切り方を選ぶ。1 文が max_s を超えるときだけ、その文を等分して途中にも切れ目を足す
    (boundary_kind = "split")。最後のシーンが最短に満たなくても、他に切り方が無ければそのまま(H3 に渡す音声は handlers 側で 5 秒まで無音で延ばす)。
    mv_audio.analyze の出力と同じ形(index / start / end / duration / vocal_ratio / kind / boundary_kind)のシーンの並びを返す(コマの格子への丸めは呼ぶ側)。"""
    if not sentences:
        return []
    points = [(float(sentences[0]["start"]), "start")]
    for s in sentences:
        start, end = points[-1][0], float(s["end"])
        pieces = max(1, math.ceil((end - start) / max_s - 1e-9))
        points += [(start + (end - start) * k / pieces, "split") for k in range(1, pieces)] + [(end, "sentence")]
    points[-1] = (points[-1][0], "end")
    times = [t for t, _ in points]
    target, eps = (min_s + max_s) / 2, 1e-6
    best, back = [0.0] + [math.inf] * (len(times) - 1), [0] * len(times)
    for i in range(1, len(times)):
        for j in range(i - 1, -1, -1):
            length = times[i] - times[j]
            if length > max_s + eps:
                break
            if best[j] == math.inf:
                continue
            cost = best[j] + (length - target) ** 2 + (SPLIT_PENALTY if length < min_s - eps else 0.0)
            if cost < best[i] - 1e-9:
                best[i], back[i] = cost, j
    cuts, i = [], len(times) - 1
    while i > 0:
        cuts.append(i)
        i = back[i]
    cuts.reverse()
    out, start = [], 0
    for n, end in enumerate(cuts, start=1):
        out.append({"index": n, "start": times[start], "end": times[end], "duration": times[end] - times[start], "vocal_ratio": 1.0,
                    "kind": "vocal", "boundary_kind": points[end][1]})
        start = end
    return out


def sentence_text(sentences: list, start: float, end: float, last: bool = False) -> str:
    """シーン(start〜end 秒)に含まれる文(中点が [start, end) に入るもの。最後のシーンは end も含む)を、つなげた文章にする。"""
    out = []
    for s in sentences:
        mid = (float(s["start"]) + float(s["end"])) / 2
        if start <= mid < end or (last and mid >= end):
            out.append(str(s["text"]))
    return "".join(out)


def make_scenes(plan_scenes: list, timeline: str, person: Optional[int] = None, voiceover: bool = False) -> list:
    """量子化済みのシーンの並びを、制作が持つシーンの形(MV のシーン + place / summary / refs / speech)にする。
    speech は全シーンが「読み上げに口を合わせる」(参照は人物)。ただしボイスオーバー(人物の画像が無い speech)は口を合わせない(speech = False)。
    それ以外の参照は short.script が決める(free の場面・要約は short.plan が書く)。"""
    scenes = mv.make_scenes(plan_scenes)
    lip = timeline == "speech" and not voiceover
    for scene, src in zip(scenes, plan_scenes, strict=True):
        scene.update({"place": str(src.get("place") or ""), "summary": str(src.get("summary") or ""), "text": str(src.get("text") or ""),
                      "speech": lip, "refs": [person] if lip and person is not None else []})
    return scenes


# ---- 次にやること ----

def next_step(proj: dict) -> tuple:
    """制作のデータから、次にやること: ("analyze",) / ("plan",) / ("script",) / ("scene", 番号) / ("assemble",) / ("done",)。
    plan は free だけ(分析は長さを置くだけで、シーンは plan が作る)。"""
    params, scenes = proj["params"], proj["scenes"]
    if not params.get("analysis"):
        return ("analyze",)
    if not scenes:
        return ("plan",) if params.get("timeline") == "free" else ("analyze",)
    if any(not s.get("prompt") for s in scenes):
        return ("script",)
    for s in sorted(scenes, key=lambda s: s["index"]):
        if s["status"] != "done":
            return ("scene", s["index"])
    if not params.get("assembled"):
        return ("assemble",)
    return ("done",)


class ShortDirector(Director):
    """ショート動画の制作の管理役(kind = "short")。結合の設定(最終の音声)だけは、結合し直すときに変えられる。"""
    KIND = PREFIX = "short"
    NOUN = "ショート動画"
    WORK_SUB = "short"
    STAGE_LABEL = STAGE_LABEL
    TEXT = TEXT
    PARAM_KEYS = PARAM_KEYS
    SCENE_KEYS = SCENE_KEYS

    def _resolve(self, params: dict) -> dict:
        return resolve_params(self.ledger, params)

    def rebuild_prompt(self, proj: dict, index: int, direction: str) -> str:
        return shortscript.rebuild_scene_prompt(proj["params"], proj["scenes"], index, direction)

    def _next_step(self, proj: dict) -> tuple:
        return next_step(proj)

    def _params_view(self, params: dict) -> dict:
        return {**{k: params.get(k) for k in PARAM_KEYS}, "outline": params.get("outline")}

    def _summary_extra(self, params: dict) -> dict:
        return {"timeline": params.get("timeline"), "final_audio": params.get("final_audio") or "auto", "subtitles": bool(params.get("subtitles"))}

    def _cover_id(self, params: dict):
        images = params.get("images") or []
        return images[0]["id"] if images else None

    def reassemble(self, pid: str, final_audio: str = None, subtitles: bool = None) -> dict:
        """結合し直す。final_audio(auto / none)・subtitles(テロップの焼き込み)を渡せば、制作の設定を書き換えてから(前の動画はギャラリーに残る)。"""
        self._check_reassemble(pid)
        patch = {"assembled": False}
        if final_audio not in (None, ""):
            if final_audio not in FINAL_AUDIOS:
                raise ValueError("最終の音声は " + " / ".join(FINAL_AUDIOS) + " のいずれかです")
            patch["final_audio"] = final_audio
        if subtitles is not None:
            if not isinstance(subtitles, bool):
                raise ValueError("subtitles は true / false で指定してください")
            patch["subtitles"] = subtitles
        self.ledger.update_mv_project(pid, params_patch=patch)
        return self._start(pid)


# ---- 物語からナレーションの案 ----

NARRATION_SYSTEM = "You are a scriptwriter who writes narration for short narrative videos. You answer in Japanese."
NARRATION_PROMPT = ("Write a narration draft in JAPANESE for a short narrative video (the narration is read aloud over the whole video, and one "
                    "person on screen mouths it). 3 to 8 short sentences, each ending with a Japanese full stop or similar mark, spoken style, "
                    "within 400 Japanese characters in total, following the arc of the story. Plain text only: no title, no bullet points, "
                    "no stage directions, no quotation marks, no speaker names.\nSTORY: ")
NARRATION_RETRY = "\nIMPORTANT: The answer must be written in Japanese."


async def propose_narration(llm, story: str, song_concept: str = "") -> str:
    """物語の文章(と、あれば曲の内容)から、日本語のナレーションの案(3〜8 文)を LLM で書く。入力欄を埋める用(確かめて直してから作る)。
    物語が空、LLM の答えが空・日本語でなければ LlmError。"""
    story = (story or "").strip()
    if not story:
        raise LlmError("物語の文章を書いてください(ナレーションの元になります)")
    prompt = NARRATION_PROMPT + re.sub(r"\s+", " ", story)[:STORY_MAX] + "\n"
    if (song_concept or "").strip():
        prompt += "BACKGROUND MUSIC (match its mood): " + re.sub(r"\s+", " ", song_concept.strip()) + "\n"
    prompt += "NARRATION:"
    text = ""
    for extra in ("", NARRATION_RETRY):
        text = await llm.chat(NARRATION_SYSTEM, prompt + extra, max_tokens=800, temperature=0.7)
        text = re.sub(r"^\s*(?:NARRATION|ナレーション)\s*[:：]\s*", "", re.sub(r"^```\w*\s*|\s*```$", "", text or "")).strip().strip("「」\"'*")
        if re.search(r"[぀-ヿ一-鿿]", text):
            break
    else:
        raise LlmError("ナレーションを作れませんでした(LLM が日本語で答えませんでした)")
    return text[:NARRATION_MAX]
