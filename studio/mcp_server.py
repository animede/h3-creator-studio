"""YUZU Creator Studio の MCP サーバ(試作): エージェント(Claude など)から、言葉の指示でスタジオを操作する口。

動いているスタジオ(既定 http://127.0.0.1:8700)の API を呼ぶだけで、スタジオ自体は何も変えない。人が画面で直す場所(人物の説明、
シーンのプロンプト、やり直し、結合し直し、編集タブ)はそのまま残り、エージェントは下書きと面倒な繰り返しを受け持つ。

起動(MCP の利用側が子プロセスとして起こす。stdio):
  Linux:   <スタジオ>/.venv/bin/python <スタジオ>/scripts/mcp_server.py
  Windows: <スタジオ>\\.venv\\Scripts\\python.exe <スタジオ>\\scripts\\mcp_server.py
環境変数: YCS_URL(スタジオの URL。既定は studio.toml / studio.local.toml の host・port)、YCS_LANG(ja / en。スタジオの返す文言の言語)。
"""
from __future__ import annotations

import asyncio
import io
import logging
import mimetypes
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Optional, Union

import httpx
from mcp.server.mcpserver import Image
from mcp.server.mcpserver.exceptions import ToolError

from . import __version__, media, mvscript
from .config import settings

DEFAULT_URL = f"http://{'127.0.0.1' if settings.host in ('', '0.0.0.0', '::') else settings.host}:{settings.port}"
OUTPUTS_DIR = settings.root / "outputs"
TERMINAL_JOB = ("completed", "failed", "cancelled")
TERMINAL_PROJECT = ("completed", "failed", "cancelled")
POLL_S = 3.0
PREVIEW_FRAMES = 4             # 1 シーンから切り出すコマの数(時間の順に横に並べる)
PREVIEW_BOX = 288              # 1 コマを収める正方形の一辺(px)。横長でも縦長でも同じくらいの大きさになる
PREVIEW_MAX_SCENES = 8         # 1 回に見せるシーンの数(画像が大きいとエージェントの文脈を圧迫する)
PREVIEW_LABEL_PX = 24
NOT_RUNNING = ("YUZU Creator Studio is not running at {url}. Start it first (Windows: the desktop icon or run.bat; Linux: ./run.sh). "
               "/ スタジオが起動していません。先に起動してください")

INSTRUCTIONS = """Operate YUZU Creator Studio, a local app that makes images (MiniMax-H3-Image), videos and lip-synced music videos (MiniMax-H3),
music (ACE-Step), narrated short videos and simple edits on the user's own GPU.

Typical flow for a music video: find or make a song (list_media kind=audio, or generate_music) and one character image (list_media kind=image,
generate_image, or upload_media), then create_music_video, then wait_for_project. Short videos: create_short_video with a story and/or narration
(spoken by text-to-speech) and up to 4 images (role "person" = the speaker, "scenery" = places and objects).
Generation is slow on small GPUs (a 30-second music video takes about 6 minutes on a 96GB GPU and about 20 minutes on an 8-12GB GPU), so
start the work, tell the user, and check back with wait_for_project / get_project instead of blocking for long.
Before calling a video done, look at it with preview_scenes (frames of every scene) and check it.
The human stays in charge of taste: show the user the result (file paths are local files they can open), and when they want changes,
use retry_scene with a new direction for that scene (get_project include_prompts=true shows each scene's direction) rather than
starting over. The studio wraps a direction in MiniMax's official H3 prompt format (the h3-prompt-writing skill in
github.com/MiniMax-AI/MiniMax-H3) and keeps its lip-sync / closed-mouth rules, so write only what is seen, in concrete English.
Messages from the studio are in Japanese unless YCS_LANG=en."""


class StudioError(ToolError):
    """スタジオが断った・応答しない。ToolError なので、この文がそのままエージェントに返る(ほかの例外は文が隠される)。"""


class StudioClient:
    def __init__(self, base_url: str = None, lang: str = None, transport: httpx.AsyncBaseTransport = None):
        self.base_url = (base_url or os.environ.get("YCS_URL") or DEFAULT_URL).rstrip("/")
        self.lang = (lang or os.environ.get("YCS_LANG") or "ja").strip().lower()
        self.transport = transport

    def _client(self, timeout: float = 60.0) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base_url, timeout=timeout, transport=self.transport,
                                 headers={"Accept-Language": "en" if self.lang == "en" else "ja"})

    async def call(self, method: str, path: str, *, json: Any = None, params: dict = None, files: Any = None,
                   timeout: float = 60.0) -> Any:
        try:
            async with self._client(timeout) as c:
                r = await c.request(method, path, json=json, params=params, files=files)
        except httpx.ConnectError:
            raise StudioError(NOT_RUNNING.format(url=self.base_url))
        except httpx.HTTPError as e:
            raise StudioError(f"Could not reach the studio: {type(e).__name__}: {e}")
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail")
            except ValueError:
                detail = r.text[:300]
            raise StudioError(f"The studio refused ({r.status_code}): {detail}")
        try:
            return r.json()
        except ValueError:
            return r.text

    async def download(self, path: str, timeout: float = 120.0) -> bytes:
        """生成物(/outputs/...)の中身を取る。この PC に無いとき(別の場所のスタジオを YCS_URL で指しているとき)に使う。"""
        try:
            async with self._client(timeout) as c:
                r = await c.get(path)
        except httpx.ConnectError:
            raise StudioError(NOT_RUNNING.format(url=self.base_url))
        except httpx.HTTPError as e:
            raise StudioError(f"Could not reach the studio: {type(e).__name__}: {e}")
        if r.status_code >= 400:
            raise StudioError(f"Could not download {path.split('?', 1)[0]} ({r.status_code})")
        return r.content


client = StudioClient()


# ---- 整形(エージェントに返す形。大きなものは省く) ----

def local_file(url: Optional[str]) -> Optional[str]:
    """/outputs/<path> の URL を、この PC のファイルの場所にする(あれば)。"""
    if not url or not url.startswith("/outputs/"):
        return None
    p = OUTPUTS_DIR / url.split("?", 1)[0][len("/outputs/"):]      # シーンの URL には ?v=<更新時刻> がつく
    return str(p) if p.is_file() else None


def media_view(rec: dict) -> dict:
    params = rec.get("params") or {}
    out = {"id": rec.get("id"), "kind": rec.get("kind"), "tab": rec.get("tab"), "filename": rec.get("filename"),
           "width": rec.get("width"), "height": rec.get("height"), "duration_s": rec.get("duration_s"),
           "url": client.base_url + rec["url"] if rec.get("url") else None, "file": local_file(rec.get("url"))}
    for key in ("prompt", "theme", "title", "caption"):
        if params.get(key):
            out[key] = str(params[key])[:300]
    return {k: v for k, v in out.items() if v not in (None, "")}


def job_view(job: dict, outputs: list = None) -> dict:
    out = {"job_id": job.get("id"), "kind": job.get("kind"), "status": job.get("status"), "message": job.get("message"),
           "progress": job.get("progress"), "error": job.get("error")}
    if outputs:
        out["outputs"] = outputs
    return {k: v for k, v in out.items() if v not in (None, "")}


def project_view(p: dict, include_prompts: bool = False) -> dict:
    out = {"project_id": p.get("id"), "title": p.get("title"), "state": p.get("state"), "message": p.get("message"),
           "progress": p.get("progress"), "error": p.get("error")}
    scenes = p.get("scenes")
    if isinstance(scenes, list):
        out["scenes"] = []
        for s in scenes:
            v = {"index": s.get("index"), "start": s.get("start"), "end": s.get("end"), "kind": s.get("kind"),
                 "status": s.get("status"), "elapsed_s": s.get("elapsed_s"), "error": s.get("error")}
            if s.get("clip_url"):
                v["clip_file"] = local_file(s["clip_url"])
            if include_prompts:
                if s.get("direction"):
                    v["direction"] = s["direction"]
                elif s.get("prompt"):
                    v["prompt"] = s["prompt"]      # 演出が保存されていない(古い制作・全文を画面で直した)シーンは全文
            out["scenes"].append({k: x for k, x in v.items() if x not in (None, "")})
    elif scenes is not None:
        out["scenes"] = scenes          # 一覧では数だけ
    output = p.get("output")
    if output:
        out["output"] = {"id": output.get("id"), "url": client.base_url + output["url"], "file": local_file(output.get("url")),
                         "duration_s": output.get("duration_s")}
    return {k: v for k, v in out.items() if v not in (None, "")}


def _kind(kind: str) -> str:
    k = str(kind or "").strip().lower().replace("-", "_")
    if k in ("mv", "music_video"):
        return "mv"
    if k in ("short", "short_video"):
        return "short"
    raise StudioError('kind must be "mv" (music video) or "short" (short video)')


async def _job_outputs(job: dict) -> list:
    ids = job.get("outputs") or []
    if not ids:
        return []
    recs = await client.call("GET", "/api/gallery", params={"limit": 200})
    by_id = {r["id"]: r for r in recs}
    return [media_view(by_id[i]) for i in ids if i in by_id]


async def _wait_job(job_id: str, timeout_s: float) -> dict:
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    while True:
        job = await client.call("GET", f"/api/jobs/{job_id}")
        if job.get("status") in TERMINAL_JOB or time.monotonic() >= deadline:
            return job_view(job, await _job_outputs(job) if job.get("status") == "completed" else None)
        await asyncio.sleep(POLL_S)


# ---- 道具(MCP の tool。説明は英語: どの言語のエージェントにも読みやすいように) ----

async def studio_status() -> dict:
    """Show the studio's state: version, GPU and memory use, engines (loaded / stopped) and the jobs that are running or waiting."""
    s = await client.call("GET", "/api/status")
    jobs = await client.call("GET", "/api/jobs", params={"limit": 30})
    jobs = jobs.get("jobs", jobs) if isinstance(jobs, dict) else jobs
    gpu = s.get("gpu") or {}
    return {"version": s.get("version"), "url": client.base_url,
            "gpu": {"name": gpu.get("name"), "used_mib": gpu.get("used_mib"), "total_mib": gpu.get("total_mib"), "mode": s.get("gpu_mode")},
            "ram": s.get("ram"),
            "engines": {n: c.get("state") for n, c in (s.get("components") or {}).items()},
            "active_jobs": [job_view(j) for j in jobs if j.get("status") not in TERMINAL_JOB]}


async def list_media(kind: str = "all", limit: int = 20) -> list:
    """List recent images, videos and songs in the studio's gallery (newest first). kind: "all", "image", "video" or "audio".
    Each item has an id (use it as song_id / image_id / reference), a local file path and the prompt or theme it was made with."""
    recs = await client.call("GET", "/api/gallery", params={"kind": kind, "limit": max(1, min(int(limit), 200))})
    return [media_view(r) for r in recs]


async def upload_media(path: str) -> dict:
    """Add a local image, song (audio) or video file to the studio's gallery and return its record (use the id in other tools)."""
    p = Path(path).expanduser()
    if not p.is_file():
        raise StudioError(f"No such file: {p}")
    mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    with p.open("rb") as f:
        recs = await client.call("POST", "/api/uploads", files=[("files", (p.name, f, mime))], timeout=300.0)
    return media_view(recs[0])


async def generate_image(prompt: str, width: int = 1024, height: int = 1024, reference_ids: Optional[list[str]] = None,
                         steps: Optional[int] = None, seed: Optional[int] = None, style: str = "none",
                         wait: bool = True, timeout_s: int = 600) -> dict:
    """Make an image with MiniMax-H3-Image. With reference_ids (gallery image ids, up to 16) it edits / combines those images
    following the prompt; without, it draws from text. width/height 256-4096 (rounded to 32). steps: 2, 3 or 6 (default 6, best quality).
    With wait=true it waits up to timeout_s seconds and returns the finished image (the first run may also load the engine)."""
    params = {"prompt": prompt, "width": width, "height": height, "style": style}
    if reference_ids:
        params["references"] = list(reference_ids)
    if steps is not None:
        params["steps"] = steps
    if seed is not None:
        params["seed"] = seed
    kind = "h3image.edit" if reference_ids else "h3image.t2i"
    job = await client.call("POST", "/api/jobs", json={"tab": "image", "kind": kind, "params": params})
    return await _wait_job(job["id"], timeout_s if wait else 0)


async def generate_music(theme: str = "", lyrics: str = "", caption: str = "", genre: str = "J-POP", duration: int = 60,
                         instrumental: bool = False, language: str = "Japanese", wait: bool = True, timeout_s: int = 900) -> dict:
    """Make a song with ACE-Step. Give at least a theme (one line about the song); empty lyrics and caption are written by the
    studio's LLM. caption = style description in English (genre, instruments, vocal, tempo). duration 10-300 seconds.
    language: Japanese, English, Chinese, Korean, ... instrumental=true makes it without vocals."""
    params = {"theme": theme, "lyrics": lyrics, "caption": caption, "genre": genre, "duration": duration,
              "instrumental": instrumental, "language": language}
    job = await client.call("POST", "/api/jobs", json={"tab": "music", "kind": "ace.simple", "params": params})
    return await _wait_job(job["id"], timeout_s if wait else 0)


async def get_job(job_id: str, wait_s: int = 0) -> dict:
    """Check an image or music job started by generate_image / generate_music (optionally waiting up to wait_s seconds)."""
    return await _wait_job(job_id, wait_s)


async def create_music_video(song_id: str, image_id: str, character: str = "", concept: str = "", aspect: str = "16:9",
                             quality: str = "fast", lipsync: bool = True, style: str = "none", background_text: str = "auto",
                             seed: Optional[int] = None) -> dict:
    """Start a music video from a song (gallery audio id) and one character image (gallery image id). The studio splits the song
    into 5-10 second scenes, writes the direction with its LLM and renders each scene with MiniMax-H3; with lipsync=true the
    character sings the vocals. aspect: 16:9, 9:16, 1:1, 4:5 or 5:4. quality: low (512px), fast (768px), standard (1024px), high.
    style: none, real, cinema, drama, anime, illust, sketch, watercolor, oil, cg3d. character / concept override the
    automatic descriptions (look of the person / world of the song). Returns the project; follow it with wait_for_project."""
    body = {"song_id": song_id, "image_id": image_id, "character": character, "concept": concept, "aspect": aspect,
            "quality": quality, "lipsync": lipsync, "style": style, "background_text": background_text, "seed": seed}
    return project_view(await client.call("POST", "/api/mv", json=body))


async def create_short_video(story: str = "", narration: str = "", song_id: str = "", images: Optional[list[dict]] = None,
                             aspect: str = "9:16", quality: str = "fast", length_s: Optional[float] = None, subtitles: bool = True,
                             style: str = "none", final_audio: str = "auto") -> dict:
    """Start a short story video. story: what happens (any language). narration: words to be spoken by text-to-speech (the
    person image, if any, lip-syncs them; without a person it becomes an off-screen voice-over). song_id: background music.
    images: up to 4 items like {"id": "<gallery image id>", "role": "person" | "scenery", "description": "optional"}
    (one person at most). length_s: length when there is neither song nor narration (30 s or more). subtitles burns the
    narration in as captions. Returns the project; follow it with wait_for_project."""
    body = {"story": story, "narration": narration, "song_id": song_id, "images": images or [], "aspect": aspect,
            "quality": quality, "length_s": length_s, "subtitles": subtitles, "style": style, "final_audio": final_audio}
    return project_view(await client.call("POST", "/api/short", json=body))


async def list_projects(kind: str = "mv", limit: int = 10) -> list:
    """List music video (kind "mv") or short video (kind "short") projects, newest first."""
    items = await client.call("GET", f"/api/{_kind(kind)}")
    return [project_view(p) for p in items[:max(1, int(limit))]]


async def get_project(project_id: str, kind: str = "mv", include_prompts: bool = False) -> dict:
    """Show a music video ("mv") or short video ("short") project: state, progress, each scene and the finished file.
    include_prompts=true adds each scene's direction (the part written for that scene: composition, place and light, action,
    camera), to rewrite one and pass it to retry_scene. A scene whose prompt was edited as a whole shows the full prompt instead."""
    return project_view(await client.call("GET", f"/api/{_kind(kind)}/{project_id}"), include_prompts)


async def wait_for_project(project_id: str, kind: str = "mv", timeout_s: int = 300) -> dict:
    """Wait until the project finishes (or timeout_s seconds pass, at most 1800) and return its state. Call it again to keep waiting."""
    deadline = time.monotonic() + max(0, min(int(timeout_s), 1800))
    path = f"/api/{_kind(kind)}/{project_id}"
    while True:
        p = await client.call("GET", path)
        if p.get("state") in TERMINAL_PROJECT or time.monotonic() >= deadline:
            return project_view(p)
        await asyncio.sleep(POLL_S)


async def retry_scene(project_id: str, index: int, kind: str = "mv", direction: Optional[str] = None, prompt: Optional[str] = None,
                      seed: Optional[int] = None) -> dict:
    """Re-render one scene (index starts at 1). direction (recommended) replaces only what the scene shows: start from the
    direction given by get_project include_prompts=true, change only what the user asked, and write about 120-180 words of
    concrete English (composition and where the character is, the place and its light, the action and how it changes, camera
    movement; concrete details rather than words like "cinematic"). Naming a framing (wide shot, profile, from behind ...)
    chooses it for a scene without singing; singing / speaking scenes always keep the mouth visible. The studio rebuilds the
    official H3 prompt around it. prompt replaces the whole H3 prompt instead (advanced: it must keep the same sections, one
    [Shot 1] without a timestamp and the same <Subject N> / <Picture N> / <Audio N> labels, or it is refused). With neither,
    a new seed gives a different take. When all scenes are done the video is assembled again automatically."""
    path = f"/api/{_kind(kind)}/{project_id}"
    if direction is not None and prompt is not None:
        raise StudioError("Pass either direction or prompt, not both")
    if prompt is not None:
        proj = await client.call("GET", path)
        original = next((s.get("prompt") or "" for s in proj.get("scenes") or [] if s.get("index") == int(index)), "")
        problems = mvscript.prompt_problems(original, prompt) if original else []
        if problems:
            raise StudioError("The edited prompt breaks the official H3 format: " + "; ".join(problems)
                              + ". Pass only the scene's direction instead (retry_scene direction=...).")
    body = {"direction": direction, "prompt": prompt, "seed": seed}
    return project_view(await client.call("POST", f"{path}/scenes/{int(index)}/retry", json=body))


async def control_project(project_id: str, action: str, kind: str = "mv") -> dict:
    """pause, resume, cancel or reassemble a project. reassemble joins the finished scenes into the video again."""
    action = str(action).strip().lower()
    if action not in ("pause", "resume", "cancel", "reassemble"):
        raise StudioError('action must be "pause", "resume", "cancel" or "reassemble"')
    return project_view(await client.call("POST", f"/api/{_kind(kind)}/{project_id}/{action}"))


def _scene_strip(ffmpeg: str, clip: Path, duration: float):
    """1 シーンの動画から PREVIEW_FRAMES コマを切り出し、横に並べた画像(Pillow)にする。コマは等分した区間の真ん中の時刻
    (6 秒なら 0.75・2.25・3.75・5.25 秒)。1 コマずつ時刻を指定して取る(fps フィルタでまとめて取ると、端数でコマが 1 つ欠けた)。
    長さは ffprobe で読み、読めなければ duration(シーンの区間)を使う。"""
    from PIL import Image as PILImage
    length = media.probe(clip).get("duration_s") or max(float(duration or 0.0), 0.5)
    frames = []
    for i in range(PREVIEW_FRAMES):
        cmd = [ffmpeg, "-v", "error", "-ss", f"{length * (i + 0.5) / PREVIEW_FRAMES:.3f}", "-i", str(clip), "-frames:v", "1",
               "-vf", f"scale={PREVIEW_BOX}:{PREVIEW_BOX}:force_original_aspect_ratio=decrease", "-f", "image2pipe", "-vcodec", "png", "-"]
        out = subprocess.run(cmd, capture_output=True, timeout=60)
        if out.returncode != 0 or not out.stdout:
            raise StudioError(f"ffmpeg could not read {clip.name}: {out.stderr.decode(errors='replace').strip()[:200]}")
        frames.append(PILImage.open(io.BytesIO(out.stdout)).convert("RGB"))
    strip = PILImage.new("RGB", (sum(f.width for f in frames), max(f.height for f in frames)))
    x = 0
    for f in frames:
        strip.paste(f, (x, 0))
        x += f.width
    return strip


def _contact_sheet(rows: list) -> bytes:
    """[(見出し, 画像), ...] を縦に積み、各行の上に見出しを書いた JPEG にする。"""
    from PIL import Image as PILImage, ImageDraw, ImageFont
    font = ImageFont.load_default(size=16)
    width = max(img.width for _, img in rows)
    sheet = PILImage.new("RGB", (width, sum(PREVIEW_LABEL_PX + img.height for _, img in rows)), (24, 24, 24))
    draw, y = ImageDraw.Draw(sheet), 0
    for label, img in rows:
        draw.text((6, y + 3), label, fill=(240, 240, 240), font=font)
        sheet.paste(img, (0, y + PREVIEW_LABEL_PX))
        y += PREVIEW_LABEL_PX + img.height
    buf = io.BytesIO()
    sheet.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


async def preview_scenes(project_id: str, kind: str = "mv", scenes: Optional[list[int]] = None) -> list[Union[str, Image]]:
    """Look at a music video ("mv") or short video ("short"): returns one image with a row per finished scene (labelled
    "Scene N", 4 frames left to right in time) and a short note. Use it before calling the video done and after retry_scene.
    Check: the same face and clothes in every scene, singing / speaking scenes move the mouth, scenes without singing keep
    it closed, the framing is not the same in every scene, no garbled faces or hands, no text in the picture.
    scenes: scene numbers to show (default all finished ones, at most 8 per call)."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise StudioError("ffmpeg is not installed on this PC, so the scenes cannot be previewed")
    proj = await client.call("GET", f"/api/{_kind(kind)}/{project_id}")
    all_scenes = [s for s in proj.get("scenes") or [] if isinstance(s, dict)]
    wanted = {int(i) for i in scenes} if scenes else None
    chosen = [s for s in all_scenes if wanted is None or s.get("index") in wanted]
    ready = [s for s in chosen if s.get("clip_url")]
    waiting = [s.get("index") for s in chosen if not s.get("clip_url")]
    if not ready:
        raise StudioError("No finished scenes to show yet" + (f" (not rendered: {waiting})" if waiting else ""))
    shown, later = ready[:PREVIEW_MAX_SCENES], [s.get("index") for s in ready[PREVIEW_MAX_SCENES:]]
    rows = []
    with tempfile.TemporaryDirectory(prefix="ycs_preview_") as tmp:
        for s in shown:
            clip = local_file(s["clip_url"])
            if clip is None:          # 別の場所のスタジオ: HTTP で取ってくる
                path = Path(tmp) / f"scene_{s['index']}.mp4"
                path.write_bytes(await client.download(s["clip_url"]))
                clip = str(path)
            duration = float(s.get("end") or 0) - float(s.get("start") or 0)
            strip = await asyncio.to_thread(_scene_strip, ffmpeg, Path(clip), duration)
            label = f"Scene {s['index']}  {s.get('kind') or ''}  {float(s.get('start') or 0):.1f}-{float(s.get('end') or 0):.1f} s"
            rows.append((label, strip))
    note = (f"Project {project_id}: rows top to bottom are scenes {[s['index'] for s in shown]}, "
            f"{PREVIEW_FRAMES} frames each, left to right in time.")
    if waiting:
        note += f" Not rendered yet: {waiting}."
    if later:
        note += f" More scenes: call again with scenes={later[:PREVIEW_MAX_SCENES]}."
    return [note, Image(data=await asyncio.to_thread(_contact_sheet, rows), format="jpeg")]


TOOLS = (studio_status, list_media, upload_media, generate_image, generate_music, get_job, create_music_video, create_short_video,
         list_projects, get_project, wait_for_project, preview_scenes, retry_scene, control_project)


def build_server():
    from mcp.server.mcpserver import MCPServer
    server = MCPServer(name="yuzu-creator-studio", title="YUZU Creator Studio", version=__version__, instructions=INSTRUCTIONS)
    for fn in TOOLS:
        server.add_tool(fn)
    logging.getLogger("httpx").setLevel(logging.WARNING)     # MCPServer が INFO で出すようにするので、問い合わせのたびの行を止める
    return server


def main() -> None:
    build_server().run("stdio")


if __name__ == "__main__":
    main()
