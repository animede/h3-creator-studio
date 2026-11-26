"""AivisSpeech(音声合成)の利用: 話者の一覧と、文章 → WAV の合成。VOICEVOX 互換の API を使う。

AivisSpeech は CPU で動くエンジンなので、必要なら scheduler.ensure("aivis") で起こしても GPU の常駐者は変わらない。
"""
import asyncio
import re
import time
import uuid
import wave
from pathlib import Path

import httpx

from .ledger import Ledger
from .scheduler import Scheduler, SwitchError

COMPONENT = "aivis"
MAX_TEXT = 2000
SPEAKERS_TTL_S = 60.0
MIN_SPEED, MAX_SPEED = 0.5, 2.0


class TtsError(Exception):
    """利用者向けの日本語の理由と、返す HTTP ステータス。"""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.message, self.status = message, status


def _wav_seconds(path: Path):
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / w.getframerate()
    except (wave.Error, EOFError, OSError, ZeroDivisionError):
        return None


class Tts:
    def __init__(self, sched: Scheduler, ledger: Ledger, outputs_dir: Path, transport=None):
        self.sched, self.ledger, self.outputs_dir, self.transport = sched, ledger, Path(outputs_dir), transport
        self._speakers: list = []
        self._speakers_at = 0.0
        self._active = 0             # 合成・一覧の最中の数(RAM の少ない PC で、GPU の仕事の前に AivisSpeech を止めてよいかの判定)

    def in_use(self) -> bool:
        return self._active > 0

    async def _ready(self) -> httpx.AsyncClient:
        """AivisSpeech を使える状態にして、その API 用のクライアントを返す。"""
        comp = self.sched.comps.get(COMPONENT)
        if comp is None:
            raise TtsError("AivisSpeech の設定がありません(studio.toml の [components.aivis])", 503)
        check = getattr(self.sched, "check_alive", None)
        if check is not None:
            check(COMPONENT)        # 落ちた AivisSpeech は状態が ready のまま残り、ensure を飛ばして合成が失敗し続けていた。落ちていれば stopped に戻して起こし直す
        if comp.state != "ready":
            op = self.sched.new_operation("ensure", COMPONENT)
            try:
                await self.sched.ensure(COMPONENT, op)
            except SwitchError as e:
                raise TtsError(f"AivisSpeech を起動できません: {e.message}", 503)
            finally:
                op.done = True
        return httpx.AsyncClient(base_url=comp.spec.base_url, timeout=120.0, transport=self.transport)

    async def speakers(self) -> list:
        """[{style_id, label: "<話者> / <スタイル>"}]。60 秒は覚えておく。"""
        if self._speakers and time.monotonic() - self._speakers_at < SPEAKERS_TTL_S:
            return self._speakers
        async with await self._ready() as c:
            try:
                r = await c.get("/speakers")
                r.raise_for_status()
                data = r.json()
            except (httpx.HTTPError, ValueError) as e:
                raise TtsError(f"AivisSpeech から話者の一覧を取れません: {type(e).__name__}")
        self._speakers = [{"style_id": st["id"], "label": f'{sp["name"]} / {st["name"]}'}
                          for sp in data for st in sp.get("styles", [])]
        self._speakers_at = time.monotonic()
        return self._speakers

    async def synthesize_wav(self, text: str, style_id: int, speed: float = 1.0) -> bytes:
        """text を読み上げた WAV のバイト列を返す(保存も台帳への登録もしない。ショート動画が、文ごとに合成してつなぐのに使う)。"""
        text = (text or "").strip()
        if not text:
            raise TtsError("読み上げる文を入力してください", 400)
        if len(text) > MAX_TEXT:
            raise TtsError(f"読み上げる文は {MAX_TEXT} 文字までです(いま {len(text)} 文字)", 400)
        speed = min(MAX_SPEED, max(MIN_SPEED, float(speed or 1.0)))
        self._active += 1
        try:
            return await self._synthesize_wav(text, style_id, speed)
        finally:
            self._active -= 1

    async def _synthesize_wav(self, text: str, style_id: int, speed: float) -> bytes:
        async with await self._ready() as c:
            try:
                r = await c.post("/audio_query", params={"text": text, "speaker": style_id})
                r.raise_for_status()
                query = r.json()
                query["speedScale"] = speed
                r = await c.post("/synthesis", params={"speaker": style_id}, json=query)
                r.raise_for_status()
                wav = r.content
            except httpx.HTTPStatusError as e:
                raise TtsError(f"AivisSpeech が合成に失敗しました(HTTP {e.response.status_code})。話者の指定を確かめてください")
            except (httpx.HTTPError, ValueError) as e:
                raise TtsError(f"AivisSpeech と通信できません: {type(e).__name__}")
        return wav

    async def synthesize(self, text: str, style_id: int, speed: float = 1.0) -> dict:
        """text を読み上げた WAV を outputs/audio/ に保存し、台帳(音声・動画タブ)に登録して返す。"""
        text = (text or "").strip()
        speed = min(MAX_SPEED, max(MIN_SPEED, float(speed or 1.0)))
        wav = await self.synthesize_wav(text, style_id, speed)
        dest = self.outputs_dir / "audio" / f"{uuid.uuid4().hex}.wav"
        dest.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(dest.write_bytes, wav)
        name = re.sub(r'[\\/:*?"<>|\s]+', "_", text)[:20] + ".wav"
        return await asyncio.to_thread(
            self.ledger.add_output, "audio", dest, "video", None, name,
            {"text": text, "style_id": style_id, "speed": speed}, duration_s=_wav_seconds(dest))
