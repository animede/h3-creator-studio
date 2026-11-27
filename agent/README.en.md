# Using it from an AI agent (MCP server, prototype)

[日本語](README.md) | **English**

A server ([MCP](https://modelcontextprotocol.io/)) that lets AI agents such as Claude drive YUZU Creator Studio with plain-language instructions, plus a guide for agents (skill). **People who use the studio through its UI do not need any of this** (setup does not install it, and the studio runs without this folder).

The agent only calls the running studio's API, so every place where you adjust things by hand stays (the character description, scene directions, retries, reassembling, the Edit tab). The intended use: let the agent do the drafts (songs, character images, Music Video projects) and the repetitive work, and fix what you care about in the UI or by telling the agent.

## Setup (once)

**The easiest way**: run this in the studio folder. It prints the steps with this PC's paths filled in (the command that installs the MCP package, the one line that registers it with Claude Code, and an example Claude Desktop setting). Follow what it shows.

Linux:

```bash
.venv/bin/python agent/mcp_server.py --setup
```

Windows (PowerShell):

```powershell
.venv\Scripts\python.exe agent\mcp_server.py --setup
```

To do it by hand:

1. Install the studio (Installation in the main [README](../README.en.md)).
2. Install the MCP package (`mcp`) into the studio's environment. In the studio folder:

   Linux:

   ```bash
   uv pip install --python .venv/bin/python -r agent/requirements.txt
   ```

   Windows (PowerShell):

   ```powershell
   uv pip install --python .venv\Scripts\python.exe -r agent\requirements.txt
   ```

3. Register it with your agent (change the paths to where you put the studio).

   Claude Code (Linux):

   ```bash
   claude mcp add -s user yuzu-studio -- ~/h3-creator-studio/.venv/bin/python ~/h3-creator-studio/agent/mcp_server.py
   ```

   Claude Code (Windows, PowerShell):

   ```powershell
   claude mcp add -s user yuzu-studio -- C:\h3-creator-studio\.venv\Scripts\python.exe C:\h3-creator-studio\agent\mcp_server.py
   ```

   Clients configured with a file, such as Claude Desktop (example `claude_desktop_config.json`; write Windows paths with `\\`):

   ```json
   {
     "mcpServers": {
       "yuzu-studio": {
         "command": "C:\\h3-creator-studio\\.venv\\Scripts\\python.exe",
         "args": ["C:\\h3-creator-studio\\agent\\mcp_server.py"]
       }
     }
   }
   ```

4. Check it with `claude mcp list` (`yuzu-studio … Connected`). It works in conversations started after registering. To remove it: `claude mcp remove yuzu-studio -s user`.
5. When you use it, **start the studio first** (the MCP server does not start it). Then just talk to Claude ("show me the newest image in the gallery", "make a music video from this song and this image"). Claude asks for permission before using a tool.

If `mcp` is not installed, the server stops and the agent's MCP log shows the command to install it.

## Tools

| Tool | What it does |
|---|---|
| `studio_status` | Version, GPU and memory use, engine states, running / waiting jobs |
| `list_media` / `upload_media` | List the gallery (ids and the local file paths) / add a file from the PC to the gallery |
| `enhance_prompt` | Rewrites a short idea into a prompt with the engine's official rules (images: MiniMax-H3-Image's rules, videos: the official h3-prompt-writing; same as Enhance in the UI) |
| `generate_image` | An image (MiniMax-H3-Image; with reference image ids it edits). Also returns the image itself, so the agent can look at it |
| `generate_video` | One 5-15 second clip (MiniMax-H3; from text / from first and last frames / from references). Also returns 4 frames |
| `generate_music` | A song (ACE-Step; a theme is enough, the LLM writes the lyrics and caption). Returns the lyrics, caption, BPM and key used |
| `listen_song` | The bundled LLM listens to a song and reports language, genre, mood, instruments, vocal and tempo (agents cannot hear; not a lyrics transcription) |
| `get_job` | State of an image, video or music job |
| `create_music_video` / `create_short_video` | Start a Music Video / Short Video project |
| `preview_scenes` | Returns one image with 4 frames from each scene of a project (so the agent can look at the result; uses ffmpeg) |
| `list_projects` / `get_project` / `wait_for_project` | List projects / show one (`include_prompts` adds each scene's direction) / wait until it finishes |
| `retry_scene` | Re-render one scene (with a new direction for that scene, or a new seed; the supervisor rebuilds the official prompt around the direction); the video is reassembled automatically when all scenes are done |
| `control_project` | Pause, resume, cancel or reassemble |

Example instruction: "Make a 16:9 music video from the newest song in the gallery and the image of the woman in the cafe. When it is done, redo only scene 3 with a slower camera."

## A guide for agents (skill)

[`skills/yuzu-creator-studio/SKILL.md`](skills/yuzu-creator-studio/SKILL.md) describes the workflow (material → project → reviewing every scene with `preview_scenes` → showing the user → retrying with a new direction) and this studio's practical rules (such as the framing of scenes without singing). In Claude Code, copy the `skills/yuzu-creator-studio` folder to `~/.claude/skills/`.

## The official prompt rules

The studio follows the rules published by the model makers (none are copied here; the two MiniMax repositories do not use a standard license notice).

| Model | Official guide | Used by |
|---|---|---|
| MiniMax-H3 (video) | [`h3-prompt-writing`](https://github.com/MiniMax-AI/MiniMax-H3/tree/main/skills/h3-prompt-writing) | every Music Video / Short Video scene (always written in this form), `enhance_prompt` (video) |
| MiniMax-H3-Image | [`h3-image-prompt-writing`](https://github.com/MiniMax-AI/MiniMax-H3-Image/tree/main/skills/h3-image-prompt-writing) and the rules in [MiniMax-H3-Image-PE](https://huggingface.co/MiniMaxAI/MiniMax-H3-Image-PE) | `enhance_prompt` (image) |
| ACE-Step 1.5 (music) | [Tutorial](https://github.com/ace-step/ACE-Step-1.5/blob/main/docs/en/Tutorial.md) and the [musician's guide](https://github.com/ace-step/ACE-Step-1.5/blob/main/docs/en/ace_step_musicians_guide.md) | `generate_music` (the bundled LLM writes lyrics and caption by these rules) |

The supervisor writes Music Video / Short Video scenes in the official form (six Ref2VA sections for scenes with reference images, three sections otherwise), so an agent only passes the scene's direction. A whole-prompt edit is checked for the sections, the single `[Shot 1]` and the reference labels (`<Subject 1>` ...), and refused if they are broken.

## Notes

- Generation takes time (a 30-second Music Video takes about 6 minutes on a PRO 6000 and about 20 minutes on an 8-12GB GPU). The agent starts the project and checks back later.
- Environment variables: `YCS_URL` (the studio's URL; defaults to the host / port in `studio.toml` / `studio.local.toml`), `YCS_LANG=en` (English messages from the studio). With Claude Code: `claude mcp add -s user yuzu-studio -e YCS_LANG=en -- ...`.
- Meant for use on the same PC (the file paths returned to the agent are paths on this PC; when `YCS_URL` points to a studio elsewhere, images and videos are fetched from it to show them). Like the studio's UI, there is no authentication.
- There are no editing tools (trimming, joining, replacing the song ...). An agent that can run commands can edit with its own tools (ffmpeg and so on) on the file paths `list_media` returns; adding the result back with `upload_media` puts it in the gallery, where you can look at it and keep adjusting it in the Edit tab (never overwrite the original files). With an agent that cannot run commands, use the Edit tab.
- Prototype: there are no tools yet for presets or samples.

## This folder

```
agent/
├── README.md / README.en.md   This document (Japanese / English)
├── mcp_server.py              Launcher (your agent's MCP settings start this with Python; --setup prints the setup steps)
├── server.py                  The MCP server (calls the running studio's API)
├── requirements.txt           The MCP package (mcp), installed only by people who use it
└── skills/yuzu-creator-studio/SKILL.md   The guide for agents
```
