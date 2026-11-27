"""MCP サーバ(studio.mcp_server): 道具の一覧、スタジオの API への呼び方、断られたときの文、ファイルの場所。スタジオは偽物(MockTransport)。"""
import asyncio
import base64
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx
import mcp

from studio import mcp_server as ms


class FakeStudio:
    """呼ばれた (method, path, body) を覚え、routes の値(関数なら呼んだ結果)を返す。"""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = request.content
        if request.headers.get("content-type", "").startswith("application/json") and body:
            body = json.loads(body)
        self.calls.append((request.method, request.url.path, body, dict(request.url.params)))
        r = self.routes.get((request.method, request.url.path))
        if r is None:
            return httpx.Response(404, json={"detail": "Not Found"})
        r = r(request) if callable(r) else r
        return r if isinstance(r, httpx.Response) else httpx.Response(200, json=r)


class McpServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        patches = [mock.patch.object(ms, "OUTPUTS_DIR", self.tmp), mock.patch.object(ms, "POLL_S", 0)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def use(self, routes=None, transport=None):
        self.studio = FakeStudio(routes or {})
        c = ms.StudioClient(base_url="http://studio:8700/", lang="en", transport=transport or httpx.MockTransport(self.studio))
        p = mock.patch.object(ms, "client", c)
        p.start()
        self.addCleanup(p.stop)
        return c

    def run_tool(self, name, args):
        async def go():
            async with mcp.Client(ms.build_server()) as c:
                return await c.call_tool(name, args)
        return asyncio.run(go())

    def test_tools_listed_with_instructions(self):
        server = ms.build_server()
        tools = asyncio.run(server.list_tools())
        self.assertEqual([t.name for t in tools], [f.__name__ for f in ms.TOOLS])
        self.assertEqual(len(tools), 14)
        self.assertTrue(all(t.description for t in tools))
        self.assertIn("retry_scene", server.instructions)
        gen = next(t for t in tools if t.name == "generate_image")
        self.assertEqual(gen.input_schema["required"], ["prompt"])

    def test_not_running_message_reaches_agent(self):
        def refuse(request):
            raise httpx.ConnectError("refused")
        self.use(transport=httpx.MockTransport(refuse))
        r = self.run_tool("studio_status", {})
        self.assertTrue(r.is_error)
        self.assertIn("not running at http://studio:8700", r.content[0].text)

    def test_refusal_detail_reaches_agent(self):
        self.use({("POST", "/api/mv"): httpx.Response(400, json={"detail": "Song not found"})})
        r = self.run_tool("create_music_video", {"song_id": "nope", "image_id": "img"})
        self.assertTrue(r.is_error)
        self.assertIn("The studio refused (400): Song not found", r.content[0].text)

    def test_bad_arguments_explained(self):
        self.use()
        r = self.run_tool("control_project", {"project_id": "p1", "action": "explode"})
        self.assertTrue(r.is_error)
        self.assertIn('action must be "pause"', r.content[0].text)
        r = self.run_tool("get_project", {"project_id": "p1", "kind": "movie"})
        self.assertIn('kind must be "mv"', r.content[0].text)
        self.assertEqual(self.studio.calls, [])

    def test_generate_image_waits_and_returns_file(self):
        (self.tmp / "images").mkdir()
        (self.tmp / "images" / "a.png").write_bytes(b"png")
        polls = iter([{"id": "j1", "status": "running", "progress": 0.5},
                      {"id": "j1", "kind": "h3image.t2i", "status": "completed", "outputs": ["m1"]}])
        self.use({("POST", "/api/jobs"): {"id": "j1", "status": "queued"},
                  ("GET", "/api/jobs/j1"): lambda req: next(polls),
                  ("GET", "/api/gallery"): [{"id": "m1", "kind": "image", "url": "/outputs/images/a.png", "width": 768,
                                             "height": 768, "params": {"prompt": "a cafe"}}, {"id": "other", "url": "/x"}]})
        out = asyncio.run(ms.generate_image("a cafe", width=768, height=768, seed=7))
        method, path, body, _ = self.studio.calls[0]
        self.assertEqual((method, path), ("POST", "/api/jobs"))
        self.assertEqual(body, {"tab": "image", "kind": "h3image.t2i",
                                "params": {"prompt": "a cafe", "width": 768, "height": 768, "style": "none", "seed": 7}})
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["outputs"], [{"id": "m1", "kind": "image", "width": 768, "height": 768, "prompt": "a cafe",
                                           "url": "http://studio:8700/outputs/images/a.png",
                                           "file": str(self.tmp / "images" / "a.png")}])

    def test_generate_image_with_references_edits(self):
        self.use({("POST", "/api/jobs"): {"id": "j2"}, ("GET", "/api/jobs/j2"): {"id": "j2", "status": "queued"}})
        out = asyncio.run(ms.generate_image("same person in rain", reference_ids=["i1", "i2"], steps=3, wait=False))
        body = self.studio.calls[0][2]
        self.assertEqual(body["kind"], "h3image.edit")
        self.assertEqual(body["params"]["references"], ["i1", "i2"])
        self.assertEqual(body["params"]["steps"], 3)
        self.assertEqual(out, {"job_id": "j2", "status": "queued"})      # wait=False は 1 回見るだけ

    def test_project_view_scene_files_and_prompts(self):
        (self.tmp / "work" / "p1").mkdir(parents=True)
        (self.tmp / "work" / "p1" / "scene_02.mp4").write_bytes(b"mp4")
        (self.tmp / "video").mkdir()
        (self.tmp / "video" / "mv_p1.mp4").write_bytes(b"mp4")
        p = {"id": "p1", "state": "completed", "scenes": [
            {"index": 1, "status": "failed", "error": "OOM", "prompt": "first", "clip_url": None},
            {"index": 2, "status": "done", "prompt": "second", "clip_url": "/outputs/work/p1/scene_02.mp4?v=1764000000"}],
            "output": {"id": "o1", "url": "/outputs/video/mv_p1.mp4", "duration_s": 30.0}}
        self.use({("GET", "/api/mv/p1"): p})
        v = asyncio.run(ms.get_project("p1"))
        self.assertEqual(v["scenes"][0], {"index": 1, "status": "failed", "error": "OOM"})
        self.assertEqual(v["scenes"][1]["clip_file"], str(self.tmp / "work" / "p1" / "scene_02.mp4"))   # ?v= を外して探す
        self.assertNotIn("prompt", v["scenes"][1])
        self.assertEqual(v["output"]["file"], str(self.tmp / "video" / "mv_p1.mp4"))
        v = asyncio.run(ms.get_project("p1", include_prompts=True))
        self.assertEqual([s["prompt"] for s in v["scenes"]], ["first", "second"])
        self.assertIsNone(ms.local_file("/outputs/work/p1/missing.mp4"))
        self.assertIsNone(ms.local_file("https://elsewhere/outputs/x.mp4"))

    def test_retry_scene_and_controls_paths(self):
        self.use({("POST", "/api/short/s1/scenes/3/retry"): {"id": "s1", "state": "running"},
                  ("POST", "/api/mv/p1/reassemble"): {"id": "p1", "state": "running"}})
        v = asyncio.run(ms.retry_scene("s1", 3, kind="short_video", direction="slower camera"))
        self.assertEqual(v, {"project_id": "s1", "state": "running"})
        self.assertEqual(self.studio.calls[0][2], {"direction": "slower camera", "prompt": None, "seed": None})
        asyncio.run(ms.control_project("p1", " Reassemble "))
        self.assertEqual(self.studio.calls[1][:2], ("POST", "/api/mv/p1/reassemble"))

    def test_whole_prompt_edits_are_checked_against_the_official_form(self):
        original = ("subject_definitions:\n<Subject 1> is the exact character shown in <Picture 1>: a girl.\n<Audio 1> is the vocal.\n\n"
                    "summary:\n[reference generation + audio reuse] ...\n\nretention_analysis:\n...\n\ndetailed_description:\n"
                    "Style. [Shot 1] She sings.\n\noverall_soundscape:\nNone.\n\nnon_diegetic_music:\nN/A")
        self.use({("GET", "/api/mv/p1"): {"id": "p1", "scenes": [{"index": 1, "prompt": original}]},
                  ("POST", "/api/mv/p1/scenes/1/retry"): {"id": "p1", "state": "running"}})
        broken = original.replace("<Audio 1> is the vocal.\n", "").replace("[Shot 1] She sings.", "[Shot 1] At 00:01 she sings. [Shot 1] again")
        r = self.run_tool("retry_scene", {"project_id": "p1", "index": 1, "prompt": broken})
        self.assertTrue(r.is_error)
        for word in ("exactly one [Shot 1]", "must not have a timestamp", "reference labels must stay", "direction=..."):
            self.assertIn(word, r.content[0].text)
        self.assertEqual([c[:2] for c in self.studio.calls], [("GET", "/api/mv/p1")])          # 送らない
        ok = original.replace("She sings.", "She sings softly under the lanterns.")
        asyncio.run(ms.retry_scene("p1", 1, prompt=ok))
        self.assertEqual(self.studio.calls[-1][:3], ("POST", "/api/mv/p1/scenes/1/retry", {"direction": None, "prompt": ok, "seed": None}))
        r = self.run_tool("retry_scene", {"project_id": "p1", "index": 1, "prompt": ok, "direction": "x"})
        self.assertIn("either direction or prompt", r.content[0].text)

    def test_include_prompts_shows_the_direction(self):
        self.use({("GET", "/api/mv/p1"): {"id": "p1", "scenes": [{"index": 1, "prompt": "LONG", "direction": "She walks"},
                                                                 {"index": 2, "prompt": "EDITED WHOLE", "direction": ""}]}})
        v = asyncio.run(ms.get_project("p1", include_prompts=True))
        self.assertEqual(v["scenes"], [{"index": 1, "direction": "She walks"}, {"index": 2, "prompt": "EDITED WHOLE"}])

    def test_wait_for_project_stops_at_timeout(self):
        self.use({("GET", "/api/mv/p1"): {"id": "p1", "state": "running", "progress": 0.4}})
        v = asyncio.run(ms.wait_for_project("p1", timeout_s=0))
        self.assertEqual(v["state"], "running")
        self.assertEqual(len(self.studio.calls), 1)

    def test_status_summarises(self):
        self.use({("GET", "/api/status"): {"version": "1.0.0", "gpu_mode": "share", "ram": {"used_mib": 1},
                                           "gpu": {"name": "RTX", "used_mib": 100, "total_mib": 8192, "temp": 50},
                                           "components": {"h3": {"state": "ready", "pid": 1}, "ace": {"state": "stopped"}}},
                  ("GET", "/api/jobs"): [{"id": "a", "status": "running", "kind": "ace.simple"}, {"id": "b", "status": "completed"}]})
        s = asyncio.run(ms.studio_status())
        self.assertEqual(s["engines"], {"h3": "ready", "ace": "stopped"})
        self.assertEqual(s["gpu"], {"name": "RTX", "used_mib": 100, "total_mib": 8192, "mode": "share"})
        self.assertEqual(s["active_jobs"], [{"job_id": "a", "kind": "ace.simple", "status": "running"}])
        self.assertEqual(self.studio.calls[0], ("GET", "/api/status", b"", {}))

    def make_clip(self, path: Path, seconds: float = 2.0) -> bytes:
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size=320x180:rate=8:duration={seconds}",
                        "-pix_fmt", "yuv420p", str(path)], check=True, capture_output=True)
        return path.read_bytes()

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg が要る")
    def test_preview_scenes_returns_one_contact_sheet(self):
        from PIL import Image
        self.make_clip(self.tmp / "mv" / "p1" / "scene_001.mp4")
        remote = self.make_clip(self.tmp / "elsewhere" / "scene_002.mp4")          # この PC の outputs に無い(HTTP で取る)
        scenes = [{"index": 1, "kind": "instrumental", "start": 0.0, "end": 2.0, "clip_url": "/outputs/mv/p1/scene_001.mp4?v=1"},
                  {"index": 2, "kind": "vocal", "start": 2.0, "end": 4.0, "clip_url": "/outputs/other/scene_002.mp4?v=2"},
                  {"index": 3, "kind": "vocal", "start": 4.0, "end": 6.0, "clip_url": None}]
        self.use({("GET", "/api/mv/p1"): {"id": "p1", "scenes": scenes},
                  ("GET", "/outputs/other/scene_002.mp4"): httpx.Response(200, content=remote)})
        r = self.run_tool("preview_scenes", {"project_id": "p1"})
        self.assertFalse(r.is_error, r.content)
        text, image = r.content
        self.assertIn("rows top to bottom are scenes [1, 2], 4 frames each", text.text)
        self.assertIn("Not rendered yet: [3]", text.text)
        self.assertEqual(image.mime_type, "image/jpeg")
        sheet = Image.open(io.BytesIO(base64.b64decode(image.data)))
        self.assertEqual(sheet.size, (4 * 288, 2 * (ms.PREVIEW_LABEL_PX + 162)))      # 320x180 → 288x162 のコマが 4 つ、2 行
        self.assertIn(("GET", "/outputs/other/scene_002.mp4"), [c[:2] for c in self.studio.calls])
        with mock.patch.object(ms, "PREVIEW_MAX_SCENES", 1):
            r = self.run_tool("preview_scenes", {"project_id": "p1", "scenes": [1, 2]})
        self.assertIn("More scenes: call again with scenes=[2]", r.content[0].text)
        r = self.run_tool("preview_scenes", {"project_id": "p1", "scenes": [3]})
        self.assertTrue(r.is_error)
        self.assertIn("No finished scenes to show yet (not rendered: [3])", r.content[0].text)

    def test_preview_scenes_needs_ffmpeg(self):
        self.use()
        with mock.patch.object(ms.shutil, "which", return_value=None):
            r = self.run_tool("preview_scenes", {"project_id": "p1"})
        self.assertTrue(r.is_error)
        self.assertIn("ffmpeg is not installed", r.content[0].text)
        self.assertEqual(self.studio.calls, [])

    def test_upload_media(self):
        f = self.tmp / "song.mp3"
        f.write_bytes(b"ID3")
        self.use({("POST", "/api/uploads"): [{"id": "u1", "kind": "audio", "filename": "song.mp3", "url": "/outputs/uploads/song.mp3"}]})
        v = asyncio.run(ms.upload_media(str(f)))
        self.assertEqual(v["id"], "u1")
        self.assertIn(b'filename="song.mp3"', self.studio.calls[0][2])
        with self.assertRaises(ms.StudioError):
            asyncio.run(ms.upload_media(str(self.tmp / "none.png")))


if __name__ == "__main__":
    unittest.main()
