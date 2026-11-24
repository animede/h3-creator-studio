import json
import tempfile
import types
import unittest
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from studio import main
from studio.llm import LlmError

PLAN = """THEME: 夜明けの駅
{"recommended_duration": 75, "parts": {}}

[Verse]
始発を待つホーム
[Chorus]
朝の光が来る
"""


class FakeLlm:
    def __init__(self, response=PLAN, error=None):
        self.response, self.error, self.calls = response, error, []

    async def chat(self, system, user, max_tokens=0, temperature=0):
        self.calls.append(user)
        if self.error:
            raise self.error
        return self.response


class MusicApiTest(unittest.TestCase):
    """音楽タブの「作る」ボタンの API。lifespan は使わず、必要な app.state だけを差し替える。"""

    def setUp(self):
        self.llm, self.busy = FakeLlm(), False
        self.ace_requests = []

        def ace(req: httpx.Request) -> httpx.Response:
            self.ace_requests.append(req.url.path)
            return httpx.Response(200, json={"code": 200, "error": None, "data": {
                "models": [{"name": "acestep-v15-turbo", "is_loaded": True, "vram_ok": True}], "default_model": "acestep-v15-turbo"}})
        self.ace = ace
        self.saved = {k: getattr(main.app.state, k, None) for k in ("llm", "jobs", "sched")}
        self.addCleanup(lambda: [setattr(main.app.state, k, v) for k, v in self.saved.items()])
        main.app.state.llm = self.llm
        main.app.state.sched = types.SimpleNamespace(comps={"ace": types.SimpleNamespace(spec=types.SimpleNamespace(base_url="http://ace.test"))})
        self.use_jobs(httpx.MockTransport(self.ace))
        self.client = TestClient(main.app)

    def use_jobs(self, transport):
        main.app.state.jobs = types.SimpleNamespace(gpu_busy=lambda: self.busy, ctx=types.SimpleNamespace(transport=transport))

    def test_theme(self):
        self.llm.response = "夜行バスの窓の朝焼け"
        r = self.client.post("/api/music/theme", json={"genre": "Rock", "language": "Japanese"})
        self.assertEqual((r.status_code, r.json()), (200, {"theme": "夜行バスの窓の朝焼け"}))
        self.assertIn("Genre: Rock", self.llm.calls[0])

    def test_lyrics_with_and_without_theme(self):
        r = self.client.post("/api/music/lyrics", json={"genre": "J-POP", "language": "Japanese", "mood": "calm and gentle"})
        js = r.json()
        self.assertEqual((r.status_code, js["theme"], js["recommended_duration"]), (200, "夜明けの駅", 75))
        self.assertIn("[Chorus]", js["lyrics"])
        self.assertNotIn("CAPTION:", self.llm.calls[0])
        r = self.client.post("/api/music/lyrics", json={"genre": "J-POP", "theme": "海辺の町"})      # テーマは固定
        self.assertEqual(r.json()["theme"], "海辺の町")
        self.assertIn("海辺の町", self.llm.calls[1])

    def test_caption(self):
        self.llm.response = "A warm ballad with a gentle mood and a clear female vocal"
        r = self.client.post("/api/music/caption", json={"genre": "Ballad", "theme": "窓辺", "lyrics": "[Verse]\nあ", "mood": "calm and gentle",
                                                         "vocal": "female vocal", "thinking": True})
        self.assertEqual((r.status_code, r.json()), (200, {"caption": "A warm ballad with a gentle mood and a clear female vocal."}))
        self.assertIn("Theme/Concept: 窓辺", self.llm.calls[0])
        self.client.post("/api/music/caption", json={"genre": "Ballad", "thinking": False})
        self.assertIn("3-5 complete English prose sentences", self.llm.calls[1])

    def test_gpu_work_does_not_refuse(self):
        """GPU で生成している間も、LLM は(CPU 専用で)動くので、「GPU で生成中」と断らない。"""
        self.busy = True
        for path in ("theme", "lyrics", "caption"):
            r = self.client.post(f"/api/music/{path}", json={"genre": "Jazz"})
            self.assertEqual(r.status_code, 200, path)
        self.assertEqual(len(self.llm.calls), 3)
        r = self.client.post("/api/songplan", json={"genre": "Jazz"})
        self.assertEqual(r.status_code, 200)

    def test_llm_errors_become_503(self):
        self.llm.error = LlmError("llama-server を起動できません")
        r = self.client.post("/api/music/theme", json={})
        self.assertEqual(r.status_code, 503)
        self.assertIn("llama-server", r.json()["detail"])

    def test_models_passthrough_and_ace_down(self):
        r = self.client.get("/api/music/models")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["default_model"], "acestep-v15-turbo")
        self.assertEqual(r.json()["models"][0], {"name": "acestep-v15-turbo", "is_loaded": True, "vram_ok": True, "min_vram_gb": None, "is_default": True})

        def down(req):
            raise httpx.ConnectError("refused")
        self.use_jobs(httpx.MockTransport(down))
        self.assertEqual(self.client.get("/api/music/models").json(), {"models": [], "default_model": None, "note": "ACE が起動していません"})


if __name__ == "__main__":
    unittest.main()


class MusicModelsWhenAceDisabledTest(unittest.TestCase):
    """ACE を無効にした設定でも、モデル一覧は 404 にせず空で答える(画面のモデル欄が落ちないように)。lifespan は使わない。"""

    def test_disabled_ace_returns_empty_list(self):
        saved = getattr(main.app.state, "sched", None)
        self.addCleanup(setattr, main.app.state, "sched", saved)
        main.app.state.sched = types.SimpleNamespace(comps={})          # [components.ace] enabled = false のとき
        r = TestClient(main.app).get("/api/music/models")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["models"], [])
        self.assertIn("無効", r.json()["note"])
