---
name: yuzu-creator-studio
description: Make images, video clips, songs, lip-synced music videos and narrated short videos with YUZU Creator Studio (a local app running on the user's own GPU) through its MCP server, then refine them scene by scene with the user. Use when the user asks to create, review or fix a music video, short video, song or image with YUZU Creator Studio (h3-creator-studio).
compatibility: Needs the yuzu-studio MCP server from github.com/animede/h3-creator-studio (agent/mcp_server.py; install its `mcp` package from agent/requirements.txt) and a running studio on the same PC (with ffmpeg, which the studio also uses). Reviewing scenes needs an agent that can see images returned by tools.
---

# YUZU Creator Studio

The studio turns a song and one character image into a music video (MiniMax-H3, lip-synced or not), and a story or narration into a short video. It runs on the user's GPU and is slow on small GPUs, so start the work, tell the user, and check back.

The user is the director. You draft, render and repeat; the user decides what is good. Show results and ask before redoing scenes the user has not asked about.

## Prompt rules: the official guides, applied by the studio

This skill does not repeat the official prompt rules. They are published by the model makers, and the studio applies them for you:

| Model | Official guide | Applied by |
|---|---|---|
| MiniMax-H3 (video) | the `h3-prompt-writing` skill in [MiniMax-AI/MiniMax-H3](https://github.com/MiniMax-AI/MiniMax-H3/tree/main/skills/h3-prompt-writing) (install: `npx skills add https://github.com/MiniMax-AI/MiniMax-H3 --skill h3-prompt-writing`) | music video / short video scenes (always); `enhance_prompt tab="video"` |
| MiniMax-H3-Image | the `h3-image-prompt-writing` skill in [MiniMax-AI/MiniMax-H3-Image](https://github.com/MiniMax-AI/MiniMax-H3-Image/tree/main/skills/h3-image-prompt-writing) and the rules in [MiniMax-H3-Image-PE](https://huggingface.co/MiniMaxAI/MiniMax-H3-Image-PE) | `enhance_prompt tab="image"` |
| ACE-Step 1.5 (music) | [Tutorial](https://github.com/ace-step/ACE-Step-1.5/blob/main/docs/en/Tutorial.md) and the [musician's guide](https://github.com/ace-step/ACE-Step-1.5/blob/main/docs/en/ace_step_musicians_guide.md) | `generate_music` (the studio's LLM writes lyrics and caption by these rules) |

For single images and clips, turn a short idea into the official form with `enhance_prompt`, show the result to the user, and generate from what they approve.

For music videos and short videos you rarely need the video rules: the studio writes every scene in the official Ref2VA six-section format itself. When you change a scene, pass only its **direction** (`retry_scene direction=...`) and the studio rebuilds the official prompt around it. A whole-prompt edit (`prompt=...`) is checked and refused if it breaks the sections, the single `[Shot 1]` or the reference labels.

How to write a direction (follows the official guide's advice on detail):
- Write concrete English, about 120-180 words, in this order:
  1. composition, and where the character is in the frame
  2. the place, its light sources and colors
  3. what the character does, and how it changes during the clip
  4. camera movement: type, amplitude, speed
- Prefer concrete details (lanterns, wet asphalt, breath in cold air) over words like "cinematic" or "beautiful".
- One continuous shot. No cuts, no timestamps, no on-screen text, no lyrics.
- Change only what the user asked. Start from the current direction (`get_project include_prompts=true`).

## Image

1. Write the idea, or run `enhance_prompt tab="image"` on a short idea (any language). It returns an English prompt and a suggested aspect ratio (`suggested_width` / `suggested_height`). Show it to the user.
2. `generate_image prompt=... width=... height=...`.
   - With `reference_ids` it edits or combines gallery images; refer to them as `<Picture 1>`, `<Picture 2>` ... in the order given.
   - `steps`: 6 by default (best). 2-3 is faster, but wide flat areas can show a grid-like texture.
3. The finished image comes back as a picture. Look at it (faces, hands, text, the user's request) before showing the user.

For a music video character, the face must be clearly visible: front or three-quarter view, upper body.

## Video clip

One 5-15 second clip with sound (for a whole song, use a music video).
1. Write the idea, or run `enhance_prompt tab="video" task=... seconds=...` to get the official H3 form. Show it to the user.
2. `generate_video prompt=... mode=...`:
   - `t2va`: from text.
   - `fl2va`: from a first and/or last frame (`first_image_id`, `last_image_id`).
   - `ref2va`: from references (`reference_ids`: images, videos, audio). In the prompt, write `<Picture 1>`, `<Video 1>`, `<Audio 1>` ..., numbered per kind in the order given. To make a person speak or sing a given audio, add `vocal_lock=true` and `seconds=0` (follows the audio's length).
3. The result comes back as 4 frames of the clip. Look at them before showing the user.

## Song

1. `generate_music theme=...`. A one-line theme is enough; the studio's LLM writes the lyrics and an English caption by ACE-Step's rules.
   - Your own lyrics: section tags in square brackets (`[verse]`, `[chorus]`, `[bridge]`, `[outro]` ...), no timestamps, no romanization.
   - `caption`: genre, instruments, vocal, tempo, in English.
2. The result lists the lyrics and caption actually used, the BPM and the key. Check them against the request.
3. You cannot hear the song. Run `listen_song song_id=...`: the studio's LLM listens to two excerpts and reports language, genre, mood, instruments, vocal and tempo (not the lyrics). Tell the user what it heard and let them listen.

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
   - Things can change between the frames you see. For scenes with vehicles, crowds or large motion, look again with `frames=8`.
   - Check the people:
     - the same face and clothes in every scene, and only one copy of each person
     - singing scenes move the mouth; scenes without singing keep it closed
     - no garbled faces or hands
   - Check the world, not only the people:
     - vehicles, buildings and objects keep their shape through the scene (a train must not melt or flatten into a wall after someone steps off)
     - people stand only where people can be: not on rails, between trains, in the road in front of a car, or in mid-air
     - doors, windows and paths lead somewhere plausible
   - Check the film: scenes are not all the same framing, the story beats asked for are actually there, no text in the picture.
6. **Show the user** the final `output.file` and your notes. Ask which scenes to change.
7. **Refine**:
   - `retry_scene index=N direction=...` changes one scene. The seed is kept, so the change comes from the direction.
   - When it is done (`wait_for_project`), check it with `preview_scenes scenes=[N]` (`frames=8` if it has vehicles or large motion) before showing the user.
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

## Editing

The studio has no editing tools; use your own. If you can run commands, edit with ffmpeg (or similar) on the `file` paths from `list_media`:
trim, join, crossfade, replace or mix the song, burn in subtitles.
- Never overwrite files in the studio's `outputs` folder. Write the result to a new file, like the studio's Edit tab, which keeps originals.
- Add the result back with `upload_media`, so the user sees it in the gallery and can keep adjusting it in the Edit tab.
- If you cannot run commands, suggest the Edit tab to the user instead.

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

On a 96GB GPU, an image takes about 20 seconds, a 50-second song about a minute, and a 5-second 768px clip about a minute (more when an engine has to load first). Small GPUs take several times longer.

- Time grows with the song's length.
- The first scene also loads the model.
- A retried scene takes about the same time as one scene in the run.

## Do not

- Do not delete projects or gallery items unless the user asks for that item.
- Do not stop or restart the studio or its engines, and do not change its settings. If the studio is not running, ask the user to start it.
- Do not start several large projects at once on a small GPU. They queue and take hours.
