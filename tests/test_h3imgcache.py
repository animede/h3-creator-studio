"""MiniMax-H3-Image の本体のキャッシュの有無(studio/h3imgcache.py)と、画像タブ向けの GET /api/image/cache、ジョブの進み具合の文言。"""
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import httpx

from studio import h3imgcache as C
from studio import main
from studio.handlers import h3image
from studio.i18n import tr


def touch(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"")
    return p


class CacheStatusTest(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.d = Path(t.name) / "minimax-h3-image-server"
        self.cache = self.d / "models" / "cache"

    def test_markers_and_tmp(self):
        st = C.status(self.d, {})
        self.assertEqual((st["dir"], st["quant"], st["default_steps"]), (self.cache, None, 3))
        self.assertEqual(st["ready"], {6: False, 3: False, 2: False})
        touch(self.cache / "ckw4a8_adaln_table_pdmd3.tmp" / "transformer_adaln_table.safetensors")   # 作りかけは数えない
        touch(self.cache / "ckw4a8_adaln_table_pdmd3" / "config.json")                              # 印の無いものも数えない
        self.assertFalse(C.status(self.d, {})["ready"][3])
        touch(self.cache / "ckw4a8_adaln_table_pdmd3" / "transformer_adaln_table.safetensors")
        touch(self.cache / "int8wo_adaln_table" / "transformer_int8wo_adaln_table.pt")
        st = C.status(self.d, {})
        self.assertEqual(st["ready"], {6: True, 3: True, 2: False})          # auto: どちらかの量子化があれば ready
        self.assertEqual(st["by_quant"]["ck-w4a8"], {6: False, 3: True, 2: False})
        self.assertTrue(C.steps_ready(self.d, None, {}))
        self.assertFalse(C.steps_ready(self.d, 2, {}))

    def test_env_quant_steps_and_dir(self):
        touch(self.cache / "int8wo_adaln_table_pdmd3" / "transformer_int8wo_adaln_table.pt")
        self.assertTrue(C.status(self.d, {})["ready"][3])
        st = C.status(self.d, {"H3IMG_TRANSFORMER_QUANT": "ck-w4a8"})        # 決め打ちならその量子化だけ
        self.assertEqual((st["quant"], st["ready"][3]), ("ck-w4a8", False))
        self.assertEqual(C.default_steps({"H3IMG_STEPS": "6"}), 6)
        self.assertEqual(C.default_steps({"H3IMG_STEPS": "x"}), 3)
        self.assertEqual(C.default_steps({"H3IMG_STEPS": "5"}), 3)
        self.assertEqual(C.cache_dir(self.d, {"H3IMG_CACHE_DIR": "/x/c"}), Path("/x/c"))
        self.assertEqual(C.cache_dir(self.d, {"H3IMG_MODELS_DIR": "m"}), self.d / "m" / "cache")

    def test_commands(self):
        self.assertEqual(C.prepare_argv(3, "linux"), [".venv/bin/python", "scripts/prepare_models.py", "--build-cache", "3"])
        self.assertEqual(C.prepare_argv([2, 6], "win32"), [".venv\\Scripts\\python.exe", "scripts\\prepare_models.py", "--build-cache", "2", "6"])
        self.assertEqual(C.prepare_command(Path("C:/e"), 3, "win32"),
                         'cd "C:/e"; .\\.venv\\Scripts\\python.exe scripts\\prepare_models.py --build-cache 3')

    def test_endpoint(self):
        with mock.patch.object(main.settings, "component_dir", return_value=self.d):
            self.assertFalse(main.api_image_cache()["installed"])
            self.d.mkdir(parents=True)
            touch(self.cache / "ckw4a8_adaln_table_pdmd3" / "transformer_adaln_table.safetensors")
            with mock.patch.object(C, "engine_env", return_value={}):
                js = main.api_image_cache()
        self.assertEqual((js["installed"], js["default_steps"], js["ready"]), (True, 3, {"6": False, "3": True, "2": False}))
        self.assertIn("--build-cache 6 2", js["command"])


class BuildingProgressTest(unittest.IsolatedAsyncioTestCase):
    async def test_message_while_building(self):
        seen = []
        states = iter([("running", 0.0), ("running", 0.0), ("running", 0.5), ("completed", 1.0)])

        def handler(req):
            st, prog = next(states)
            return httpx.Response(200, json={"status": st, "progress": prog, "image_url": "/outputs/a.png" if st == "completed" else None})
        job = types.SimpleNamespace(cancel_requested=False, remote_id="r", progress=0.0, message="")
        orig_sleep = h3image.asyncio.sleep

        async def fake_sleep(_):
            seen.append(job.message)
            await orig_sleep(0)
        with mock.patch.object(h3image.asyncio, "sleep", fake_sleep):
            async with httpx.AsyncClient(base_url="http://x", transport=httpx.MockTransport(handler)) as c:
                await h3image._poll(job, c, building=True)
        self.assertEqual(seen, [h3image.BUILDING_MESSAGE] * 2 + ["生成中 50%"])
        self.assertTrue(tr(h3image.BUILDING_MESSAGE, "en").startswith("Building the main-model cache"))

    def test_cache_ready_defaults_to_true_without_the_engine(self):
        with mock.patch.object(h3image.config.settings, "component_dir", return_value=Path("/nonexistent/h3img")):
            self.assertTrue(h3image._cache_ready(3))


if __name__ == "__main__":
    unittest.main()
