"""アングル(画像タブの edit だけの設定)。プロンプトの入力欄には入れず、生成のときにサーバーが英語の指示を足す。

ここがアングルの一覧の唯一の出どころ(GET /api/angles が画面に返す)。
文は実機で確かめたもの。カメラの動き(「45 度回る」など)ではなく、画面に何が映るかを書く(左右は絵から見た向き)。
"""

ANGLE_NONE = "none"

# 被写体・場所を保つ文(none 以外のアングルの後ろに必ず足す)
KEEP_CLAUSE = ("Keep the same subject: identical face, hair, outfit, and art style. Keep the same place, lighting and background, "
               "only seen from the new camera position. Do not add other people.")

# id -> 画面の名前(ja / en)と、生成のときに足す英語の文
ANGLES = {
    "none": {"ja": "なし", "en": "None", "instruction": ""},
    "front": {"ja": "正面", "en": "Front",
              "instruction": "Re-render this scene with the camera directly in front of the subject at eye level, facing them straight on."},
    "left": {"ja": "斜め・左向き", "en": "Three-quarter, facing left",
             "instruction": "Three-quarter view: the subject's body and face turn toward the LEFT edge of the picture; we see more of their "
                            "right cheek, and their nose points to the left side of the frame."},
    "right": {"ja": "斜め・右向き", "en": "Three-quarter, facing right",
              "instruction": "Three-quarter view: the subject's body and face turn toward the RIGHT edge of the picture; we see more of their "
                             "left cheek, and their nose points to the right side of the frame."},
    "side": {"ja": "真横", "en": "Side profile",
             "instruction": "Re-render this scene with the camera moved 90 degrees to the subject's side, showing them in full profile."},
    "back": {"ja": "後ろ姿", "en": "From behind",
             "instruction": "Re-render this scene with the camera behind the subject, showing them from the back."},
    "high": {"ja": "見下ろし", "en": "High angle (looking down)",
             "instruction": "Re-render this scene from a high camera angle, the camera above the subject's head looking down at them."},
    "low": {"ja": "見上げ(全身になります)", "en": "Low angle (looking up; becomes full body)",
            "instruction": "Low-angle shot: the camera is near the floor looking steeply up at the subject. We see the underside of their chin, "
                           "their figure towers over the viewer, and the sky or ceiling fills the top two-thirds of the frame; nearby objects "
                           "are seen from below."},
}


def check_angle(value) -> str:
    """アングルの id を検証して返す(空・None は none)。知らない id は ValueError(日本語)。"""
    aid = str(value or ANGLE_NONE).strip() or ANGLE_NONE
    if aid not in ANGLES:
        raise ValueError(f"アングルは {' / '.join(ANGLES)} のいずれかです")
    return aid


def angle_instruction(angle_id) -> str:
    """生成のときに足す英語の文(アングルの指示 + 被写体・場所を保つ文。none は "")。"""
    text = ANGLES.get(str(angle_id or ANGLE_NONE), ANGLES[ANGLE_NONE])["instruction"]
    return f"{text} {KEEP_CLAUSE}" if text else ""


def apply_angle(prompt: str, angle_id) -> str:
    """プロンプトの後ろにアングルの文を足す(none ならそのまま。プロンプトが空なら文だけ)。"""
    sentence = angle_instruction(angle_id)
    prompt = str(prompt or "").rstrip()
    if not sentence:
        return prompt
    return f"{prompt}\n\n{sentence}" if prompt else sentence


def public_list() -> list:
    """画面に返す一覧(id と名前。文は返さない)。"""
    return [{"id": k, "ja": v["ja"], "en": v["en"]} for k, v in ANGLES.items()]
