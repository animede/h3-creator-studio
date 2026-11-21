import io
import json
import tempfile
import types
import unittest
import wave
from pathlib import Path

import httpx

from studio.ledger import Ledger
from studio.tts import MAX_TEXT, Tts, TtsError

SPEAKERS = [
    {"name": "Anneli", "speaker_uuid": "u1", "styles": [{"id": 888753760, "name": "ノーマル", "type": "talk"}, {"id": 888753761, "name": "通常"}]},
    {"name": "まい", "speaker_uuid": "u2", "styles": [{"id": 5, "name": "ノーマル"}]},
]


def wav_bytes(seconds=0.5, rate=8000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


class FakeAivis:
    def __init__(self):
        self.calls = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append((req.method, req.url.path, dict(req.url.params), req.content))
        if req.url.path == "/speakers":
            return httpx.Response(200, json=SPEAKERS)
        if req.url.path == "/audio_query":
            return httpx.Response(200, json={"accent_phrases": [], "speedScale": 1.0, "text": req.url.params["text"]})
        if req.url.path == "/synthesis":
            return httpx.Response(200, content=wav_bytes(), headers={"content-type": "audio/wav"})
        return httpx.Response(404)


class TtsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.ledger = Ledger(self.root)
        self.addCleanup(self.ledger.close)
        self.ensured = []
        comp = types.SimpleNamespace(state="stopped", spec=types.SimpleNamespace(base_url="http://aivis.test"))
        outer = self

        class Sched:
            comps = {"aivis": comp}

            def new_operation(self, kind, name):
                return types.SimpleNamespace(done=False)

            async def ensure(self, name, op=None):
                outer.ensured.append(name)
                comp.state = "ready"
        self.sched, self.comp = Sched(), comp
        self.fake = FakeAivis()
        self.tts = Tts(self.sched, self.ledger, self.root, transport=httpx.MockTransport(self.fake))

    async def test_speakers_flattened_cached_and_ensure(self):
        got = await self.tts.speakers()
        self.assertEqual(got, [{"style_id": 888753760, "label": "Anneli / ノーマル"}, {"style_id": 888753761, "label": "Anneli / 通常"},
                               {"style_id": 5, "label": "まい / ノーマル"}])
        self.assertEqual(self.ensured, ["aivis"])          # 準備できていなければ ensure する
        await self.tts.speakers()
        self.assertEqual(len([c for c in self.fake.calls if c[1] == "/speakers"]), 1)    # 60 秒は覚えている
        self.assertEqual(self.ensured, ["aivis"])

    async def test_synthesize_saves_and_registers(self):
        self.comp.state = "ready"
        rec = await self.tts.synthesize("こんにちは、世界。", 5, 1.5)
        self.assertEqual(self.ensured, [])
        self.assertEqual((rec["kind"], rec["tab"], rec["params"]["text"], rec["params"]["style_id"]), ("audio", "video", "こんにちは、世界。", 5))
        self.assertTrue(rec["path"].startswith("audio/") and rec["path"].endswith(".wav"))
        self.assertAlmostEqual(rec["duration_s"], 0.5, places=2)
        self.assertEqual((self.root / rec["path"]).read_bytes()[:4], b"RIFF")
        self.assertEqual(self.ledger.get_output(rec["id"])["kind"], "audio")
        q = next(c for c in self.fake.calls if c[1] == "/audio_query")
        self.assertEqual(q[2], {"text": "こんにちは、世界。", "speaker": "5"})
        s = next(c for c in self.fake.calls if c[1] == "/synthesis")
        self.assertEqual(s[2], {"speaker": "5"})
        self.assertEqual(json.loads(s[3])["speedScale"], 1.5)       # 話速は AudioQuery に入れて送る

    async def test_bad_text(self):
        for text in ("", "  ", "あ" * (MAX_TEXT + 1)):
            with self.assertRaises(TtsError) as cm:
                await self.tts.synthesize(text, 1)
            self.assertEqual(cm.exception.status, 400)
        await self.tts.synthesize("あ" * MAX_TEXT, 1)

    async def test_upstream_error(self):
        self.comp.state = "ready"
        tts = Tts(self.sched, self.ledger, self.root, transport=httpx.MockTransport(lambda r: httpx.Response(422, json={"detail": "x"})))
        with self.assertRaises(TtsError) as cm:
            await tts.synthesize("やあ", 999)
        self.assertIn("422", cm.exception.message)


if __name__ == "__main__":
    unittest.main()
