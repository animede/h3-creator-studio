"""MCP サーバ(agent/server.py): 道具の一覧、スタジオの API への呼び方、断られたときの文、ファイルの場所。スタジオは偽物(MockTransport)。
MCP の部品(mcp)は MCP を使う人だけが入れる(agent/requirements.txt)ので、入っていなければ全部飛ばす。"""
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

import importlib.util

import httpx

if importlib.util.find_spec("mcp") is None:
    raise unittest.SkipTest("mcp が入っていない(MCP を使う人だけが agent/requirements.txt から入れる)")

import mcp  # noqa: E402

from agent import server as ms  # noqa: E402
from studio import mvscript  # noqa: E402


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
        self.assertEqual(len(tools), 17)
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

    def test_generate_image_waits_and_returns_file_and_picture(self):
        from PIL import Image
        (self.tmp / "images").mkdir()
        Image.new("RGB", (1536, 1024), (200, 120, 40)).save(self.tmp / "images" / "a.png")
        polls = iter([{"id": "j1", "status": "running", "progress": 0.5},
                      {"id": "j1", "kind": "h3image.t2i", "status": "completed", "outputs": ["m1"]}])
        self.use({("POST", "/api/jobs"): {"id": "j1", "status": "queued"},
                  ("GET", "/api/jobs/j1"): lambda req: next(polls),
                  ("GET", "/api/gallery"): [{"id": "m1", "kind": "image", "url": "/outputs/images/a.png", "width": 768,
                                             "height": 768, "params": {"prompt": "a cafe"}}, {"id": "other", "url": "/x"}]})
        text, picture = asyncio.run(ms.generate_image("a cafe", width=768, height=768, seed=7))
        method, path, body, _ = self.studio.calls[0]
        self.assertEqual((method, path), ("POST", "/api/jobs"))
        self.assertEqual(body, {"tab": "image", "kind": "h3image.t2i",
                                "params": {"prompt": "a cafe", "width": 768, "height": 768, "style": "none", "seed": 7}})
        out = json.loads(text)
        self.assertEqual(out["status"], "completed")
        self.assertIsInstance(picture, ms.Image)                               # できた画像そのもの(縮小)も返す
        self.assertEqual(Image.open(io.BytesIO(picture.data)).size, (768, 512))
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
        self.assertEqual([json.loads(t) for t in out], [{"job_id": "j2", "status": "queued"}])      # wait=False は 1 回見るだけ

    def test_generate_video_modes(self):
        self.use({("POST", "/api/jobs"): {"id": "v1"}, ("GET", "/api/jobs/v1"): {"id": "v1", "status": "queued"}})
        asyncio.run(ms.generate_video("A lighthouse at dawn", wait=False))
        asyncio.run(ms.generate_video("It ends at dusk", mode="fl2va", first_image_id="f1", seconds=8, resolution="1344x768", wait=False))
        asyncio.run(ms.generate_video("<Picture 1> sings with <Audio 1>", mode="REF2VA", reference_ids=["i1", "a1"], vocal_lock=True,
                                      seconds=0, seed=3, wait=False))
        posts = [c[2] for c in self.studio.calls if c[0] == "POST"]
        self.assertEqual(posts[0], {"tab": "video", "kind": "h3.t2va", "params": {"prompt": "A lighthouse at dawn", "seconds": 5, "style": "none"}})
        self.assertEqual(posts[1]["kind"], "h3.fl2va")
        self.assertEqual(posts[1]["params"], {"prompt": "It ends at dusk", "seconds": 8, "style": "none", "resolution": "1344x768",
                                              "first_image": "f1", "last_image": None})
        self.assertEqual(posts[2]["kind"], "h3.ref2va")
        self.assertEqual(posts[2]["params"], {"prompt": "<Picture 1> sings with <Audio 1>", "seconds": 0, "style": "none", "seed": 3,
                                              "references": ["i1", "a1"], "vocal_lock": True})
        r = self.run_tool("generate_video", {"prompt": "x", "mode": "i2v"})
        self.assertIn("mode must be one of", r.content[0].text)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg が要る")
    def test_finished_video_comes_back_as_frames(self):
        from PIL import Image
        self.make_clip(self.tmp / "video" / "v1.mp4")
        self.use({("POST", "/api/jobs"): {"id": "v1"},
                  ("GET", "/api/jobs/v1"): {"id": "v1", "kind": "h3.t2va", "status": "completed", "outputs": ["o1"]},
                  ("GET", "/api/gallery"): [{"id": "o1", "kind": "video", "url": "/outputs/video/v1.mp4", "duration_s": 2.0}]})
        r = self.run_tool("generate_video", {"prompt": "A lighthouse at dawn"})
        self.assertEqual([c.type for c in r.content], ["text", "image"])
        sheet = Image.open(io.BytesIO(base64.b64decode(r.content[1].data)))
        self.assertEqual(sheet.size, (4 * 288, ms.PREVIEW_LABEL_PX + 162))

    def test_preview_failures_do_not_fail_the_job(self):
        self.use({("GET", "/api/jobs/j1"): {"id": "j1", "status": "completed", "outputs": ["m1"]},
                  ("GET", "/api/gallery"): [{"id": "m1", "kind": "image", "url": "/outputs/gone.png"}]})     # この PC に無く、取ってきても 404
        out = asyncio.run(ms.get_job("j1"))
        self.assertEqual(len(out), 1)
        self.assertEqual(json.loads(out[0])["status"], "completed")

    def test_music_result_lists_the_lyrics_used(self):
        rec = {"id": "s1", "kind": "audio", "url": "/outputs/audio/s1.mp3", "duration_s": 50.0,
               "params": {"theme": "冬の灯り", "caption": "warm j-pop ballad", "lyrics": "[verse]\n木枯らしが吹く帰り道", "bpm": 88,
                          "key_scale": "C major", "time_signature": "4"}}
        self.use({("POST", "/api/jobs"): {"id": "m1"},
                  ("GET", "/api/jobs/m1"): {"id": "m1", "kind": "ace.simple", "status": "completed", "outputs": ["s1"]},
                  ("GET", "/api/gallery"): [rec]})
        (text,) = asyncio.run(ms.generate_music(theme="冬の灯り", duration=50))
        song = json.loads(text)["outputs"][0]
        self.assertEqual((song["lyrics"], song["bpm"], song["key_scale"]), ("[verse]\n木枯らしが吹く帰り道", 88, "C major"))
        self.assertIn("冬の灯り", text)                                          # 日本語のまま(\u エスケープしない)
        self.assertNotIn("lyrics", ms.media_view(rec))                           # 一覧では歌詞を出さない

    def test_enhance_prompt(self):
        self.use({("POST", "/api/enhance"): lambda req: {"text": "An expanded prompt.", "aspect_ratio": "16:9", "suggested_width": 1344,
                                                         "suggested_height": 768} if json.loads(req.content)["tab"] == "image" else
                                                        {"text": "integrated_multimodal_description: [Shot 1] ..."}})
        v = asyncio.run(ms.enhance_prompt("夜明けの灯台", reference_ids=["i1"]))
        self.assertEqual(v, {"prompt": "An expanded prompt.", "aspect_ratio": "16:9", "suggested_width": 1344, "suggested_height": 768})
        self.assertEqual(self.studio.calls[0][2], {"tab": "image", "text": "夜明けの灯台", "references": ["i1"]})
        v = asyncio.run(ms.enhance_prompt("夜明けの灯台", tab="video", task="ref2va", seconds=8))
        self.assertEqual(v, {"prompt": "integrated_multimodal_description: [Shot 1] ..."})
        self.assertEqual(self.studio.calls[1][2], {"tab": "video", "text": "夜明けの灯台", "mode": "h3-official", "task": "ref2va",
                                                   "lang": "en", "seconds": 8})
        for args, word in (({"text": "x", "tab": "music"}, 'tab must be "image" or "video"'),
                           ({"text": "x", "tab": "video", "task": "t2i"}, "task must be one of")):
            r = self.run_tool("enhance_prompt", args)
            self.assertIn(word, r.content[0].text)

    def test_listen_song(self):
        heard = {"language": "Japanese", "genre": "J-Pop", "vocal": "female", "tempo": "slow"}
        self.use({("POST", "/api/mv/describe/song"): {"text": "冬の街の歌", "source": "listen", "listen": heard}})
        self.assertEqual(asyncio.run(ms.listen_song("s1")), {"heard": heard, "description": "冬の街の歌"})
        self.assertEqual(self.studio.calls[0][2], {"song_id": "s1", "listen": True})

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

    def test_prompt_problems(self):
        original = mvscript.scene_prompt({"index": 1, "start": 0.0, "end": 8.0, "duration": 8.0, "vocal_ratio": 0.9}, "She sings", {"subject": "a woman"})
        self.assertEqual(ms.prompt_problems(original, original.replace("She sings", "She sings softly")), [])
        no_audio = original.replace("<Audio 1>", "the audio")
        self.assertIn("reference labels must stay", " ".join(ms.prompt_problems(original, no_audio)))
        no_summary = original.replace("summary:\n", "")
        self.assertIn("sections must stay", " ".join(ms.prompt_problems(original, no_summary)))
        two_shots = original.replace("[Shot 1] ", "[Shot 1] At 00:02.000, ") + "\n[Shot 1] more"
        problems = " ".join(ms.prompt_problems(original, two_shots))
        self.assertIn("exactly one [Shot 1]", problems)
        self.assertIn("must not have a timestamp", problems)

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
        r = self.run_tool("preview_scenes", {"project_id": "p1", "scenes": [1], "frames": 8})          # 乗り物・動きの大きいシーンは 8 コマ
        self.assertIn("8 frames each", r.content[0].text)
        sheet8 = Image.open(io.BytesIO(base64.b64decode(r.content[1].data)))
        self.assertEqual(sheet8.size, (8 * 200, ms.PREVIEW_LABEL_PX + 113))           # 横幅 1600 に収めるので 1 コマ 200px(320x180 → 200x113)
        for bad in (1, 9):
            r = self.run_tool("preview_scenes", {"project_id": "p1", "frames": bad})
            self.assertTrue(r.is_error)
            self.assertIn("frames must be 2-8", r.content[0].text)
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
