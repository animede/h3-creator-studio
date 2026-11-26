"""画風(画像・動画・MV・ショート動画で共通の設定)。プロンプトの入力欄には入れず、生成のときにサーバーが英語の指示を足す。

ここが画風の一覧の唯一の出どころ(GET /api/styles が画面に返す)。
参照画像があるとき(人物・景色の画像、edit、ref2va / ref2i、fl2va のフレーム)は、被写体の見た目を参照のまま保つ文にする。
"""

STYLE_NONE = "none"

# id -> 画面の名前(ja / en)と、生成のときに足す英語の文
STYLES = {
    "none": {"ja": "なし(参照画像に合わせる)", "en": "None (match the reference)", "phrase": ""},
    "real": {"ja": "リアル", "en": "Realistic", "phrase": "photorealistic, natural lighting, real camera, true-to-life skin and materials"},
    "cinema": {"ja": "映画", "en": "Cinematic", "phrase": "cinematic film look, anamorphic framing, shallow depth of field, graded color"},
    "drama": {"ja": "ドラマ", "en": "TV drama", "phrase": "TV drama look, naturalistic lighting, handheld-feel camera, everyday locations"},
    "anime": {"ja": "アニメ", "en": "Anime", "phrase": "Japanese anime style, clean line art, flat cel shading, expressive eyes"},
    "illust": {"ja": "イラスト", "en": "Illustration", "phrase": "digital illustration, clean shapes, bold colors, polished finish"},
    "sketch": {"ja": "スケッチ", "en": "Sketch", "phrase": "pencil sketch, hand-drawn lines, hatching, paper texture, mostly monochrome"},
    "watercolor": {"ja": "水彩", "en": "Watercolor", "phrase": "watercolor painting, soft bleeding edges, paper texture, muted palette"},
    "oil": {"ja": "油彩", "en": "Oil painting", "phrase": "oil painting, visible brush strokes, rich impasto texture"},
    "cg3d": {"ja": "3DCG", "en": "3D CG", "phrase": "3D CG render, stylized characters, soft global illumination"},
}


def check_style(value) -> str:
    """画風の id を検証して返す(空・None は none)。知らない id は ValueError(日本語)。"""
    sid = str(value or STYLE_NONE).strip() or STYLE_NONE
    if sid not in STYLES:
        raise ValueError(f"画風は {' / '.join(STYLES)} のいずれかです")
    return sid


def style_phrase(style_id, with_reference: bool) -> str:
    """生成のときに足す英語の文(none は "")。参照ありは、被写体を参照のまま保ち、場面・背景・光・質感だけを画風にする文。"""
    phrase = STYLES.get(str(style_id or STYLE_NONE), STYLES[STYLE_NONE])["phrase"]
    if not phrase:
        return ""
    if with_reference:
        return ("Keep the referenced subject exactly as in the reference image; render the scene, background, lighting and materials "
                f"in {phrase}.")
    return f"Visual style: {phrase}."


def apply_style(prompt: str, style_id, with_reference: bool) -> str:
    """プロンプトの後ろに画風の文を足す(none ならそのまま)。"""
    sentence = style_phrase(style_id, with_reference)
    prompt = str(prompt or "").rstrip()
    if not sentence:
        return prompt
    return f"{prompt}\n\n{sentence}" if prompt else sentence


def public_list() -> list:
    """画面に返す一覧(id と名前。文は返さない)。"""
    return [{"id": k, "ja": v["ja"], "en": v["en"]} for k, v in STYLES.items()]
