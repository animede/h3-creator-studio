---
name: yuzu-creator-studio
description: Make images, songs, lip-synced music videos and narrated short videos with YUZU Creator Studio (a local app running on the user's own GPU) through its MCP server, then refine them scene by scene with the user. Use when the user asks to create, review or fix a music video, short video, song or image with YUZU Creator Studio (h3-creator-studio).
compatibility: Needs the yuzu-studio MCP server from github.com/animede/h3-creator-studio (scripts/mcp_server.py) and a running studio on the same PC (with ffmpeg, which the studio also uses). Reviewing scenes needs an agent that can see images returned by tools.
---

# YUZU Creator Studio

The studio turns a song and one character image into a music video (MiniMax-H3, lip-synced or not), and a story or narration into a short video. It runs on the user's GPU and is slow on small GPUs, so start the work, tell the user, and check back.

The user is the director. You draft, render and repeat; the user decides what is good. Show results and ask before redoing scenes the user has not asked about.

## Prompt rules: the official guide, applied by the studio

MiniMax publishes the official H3 prompt-writing rules as a skill: `h3-prompt-writing` in [github.com/MiniMax-AI/MiniMax-H3](https://github.com/MiniMax-AI/MiniMax-H3/tree/main/skills/h3-prompt-writing) (install: `npx skills add https://github.com/MiniMax-AI/MiniMax-H3 --skill h3-prompt-writing`). This skill does not repeat those rules.

You rarely need them. The studio writes every scene in the official Ref2VA six-section format itself. When you change a scene, pass only its **direction** (`retry_scene direction=...`) and the studio rebuilds the official prompt around it. A whole-prompt edit (`prompt=...`) is checked and refused if it breaks the sections, the single `[Shot 1]` or the reference labels.

How to write a direction (follows the official guide's advice on detail):
- Write concrete English, about 120-180 words, in this order:
  1. composition, and where the character is in the frame
  2. the place, its light sources and colors
  3. what the character does, and how it changes during the clip
  4. camera movement: type, amplitude, speed
- Prefer concrete details (lanterns, wet asphalt, breath in cold air) over words like "cinematic" or "beautiful".
- One continuous shot. No cuts, no timestamps, no on-screen text, no lyrics.
- Change only what the user asked. Start from the current direction (`get_project include_prompts=true`).

## Music video

1. **Song**: `list_media kind=audio`, or `generate_music`.
   - A one-line `theme` is enough; empty `lyrics` and `caption` are written by the studio's LLM.
   - Your own lyrics use section tags (`[verse]`, `[chorus]`, `[bridge]`).
   - `caption` is an English style line: genre, instruments, vocal, tempo.
   - 40-60 seconds is a good first length.
2. **Character image**: `list_media kind=image`, `generate_image` or `upload_media`.
   - For lip sync, the face must be clearly visible: front or three-quarter view, upper body.
   - The studio keeps only the person's appearance from the image, not its place or light, so scenes are free to go elsewhere.
3. **Create**: `create_music_video song_id=... image_id=...`.
   - Use `concept` for the world of the song and how it ends.
   - `quality`: fast is 768px; standard is 1024px and slower.
   - Tell the user how long it will take (see Timing).
4. **Wait**: call `wait_for_project` again and again (up to 1800 s per call; use a shorter `timeout_s` if your MCP client times out long calls). Report progress between calls.
5. **Review** before calling it done:
   - `preview_scenes project_id=...` returns one image: a row per finished scene ("Scene N"), 4 frames left to right in time. It shows up to 8 scenes per call; pass `scenes=[...]` for the rest.
   - Check:
     - the same face and clothes in every scene
     - singing scenes move the mouth; scenes without singing keep it closed
     - scenes are not all the same framing
     - no garbled faces or hands
     - no text in the picture
6. **Show the user** the final `output.file` and your notes. Ask which scenes to change.
7. **Refine**:
   - `retry_scene index=N direction=...` changes one scene. The seed is kept, so the change comes from the direction.
   - When it is done (`wait_for_project`), check it with `preview_scenes scenes=[N]` before showing the user.
   - With no direction and no seed, you get a new take of the same direction.
   - When every scene is done, the video is reassembled automatically. Earlier versions stay in the gallery.

## Short video

`create_short_video` with:
- `story`: what happens, in any language.
- `narration`: words spoken by text-to-speech.
- `song_id`: background music.
- Up to 4 `images` like `{"id": ..., "role": "person" | "scenery"}`. At most one person.

What you get:
- Narration with a person image: the person speaks it with lip sync.
- Narration without a person: an off-screen voice over scenery.
- Neither song nor narration: give `length_s`, 30 seconds or more.
- Review and refine as for music videos (`kind="short"`).

## The studio's own rules (not from the official guide)

The studio adds these from testing on real hardware. Work with them, not against them:
- **Singing / speaking scenes** always use a medium close-up with the mouth visible. Directions asking for a view from behind or a wide shot are rewritten for these scenes.
- **Scenes without singing** (intro, interludes, outro) get rotating framings: wide establishing, side tracking, low angle, from behind. If the song ends without singing, its last scene is a walk away from the camera.
  - The reference image's pose tends to win (front-facing, upper body), so make the framing and a visible change in the place (lights coming on, snow starting) explicit.
  - A direction that names its own framing (`wide shot`, `profile`, `close-up of her hand` ...) keeps it.
- **Lyrics are not written into prompts.** Vocal Lock copies the singing from the song's vocals. This deliberately differs from the official `<d>` rule, because guessed lyrics do more harm than none.
- **No text overlays** (subtitles, captions, logos). Readable background text is only reliable when large and short.
- **Image music video** (`lipsync=false`): nobody sings, and any framing works. If the user wants no stage or microphone, say so in `concept`.

## Timing (30-second music video, 768px)

| GPU | Total |
|---|---|
| RTX PRO 6000 96GB | about 6-8 min |
| RTX 3080 Ti 12GB | about 10 min |
| RTX 3060 12GB / RTX 4060 8GB | about 21 min |

- Time grows with the song's length.
- The first scene also loads the model.
- A retried scene takes about the same time as one scene in the run.

## Do not

- Do not delete projects or gallery items unless the user asks for that item.
- Do not stop or restart the studio or its engines, and do not change its settings. If the studio is not running, ask the user to start it.
- Do not start several large projects at once on a small GPU. They queue and take hours.
