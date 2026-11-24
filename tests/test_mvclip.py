import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from studio import mvclip as C

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def plan(*durations):
    """尺(秒)の並びから、分析の出力と同じ形のシーン計画を作る。"""
    t, out = 0.0, []
    for i, d in enumerate(durations, start=1):
        out.append({"index": i, "start": t, "end": t + d, "duration": d, "kind": "vocal"})
        t += d
    return out


class QuantizeTest(unittest.TestCase):
    def test_boundaries_snap_to_one_frame_grid(self):
        scenes = C.quantize_scene_plan(plan(8.4, 6.37, 9.123, 5.5))
        self.assertEqual([s["start_frame"] for s in scenes], [0, 202, 354, 573])
        self.assertEqual([s["end_frame"] for s in scenes], [202, 354, 573, 705])
        self.assertEqual([s["frame_count"] for s in scenes], [202, 152, 219, 132])
        for s in scenes:
            self.assertAlmostEqual(s["duration"], s["frame_count"] / 24, places=5)
            self.assertAlmostEqual(s["end"] - s["start"], s["duration"], places=5)
            self.assertEqual(s["frame_count"], round(s["duration"] * 24))        # クリップの枚数 = round(尺 × 24)
        self.assertEqual(sum(s["frame_count"] for s in scenes), round(29.393 * 24))      # 丸めの誤差を積み上げない
        self.assertEqual([s["end_frame"] for s in scenes[:-1]], [s["start_frame"] for s in scenes[1:]])
        self.assertEqual(scenes[0]["kind"], "vocal")                  # 他の項目は残る

    def test_empty_and_input_untouched(self):
        self.assertEqual(C.quantize_scene_plan([]), [])
        scenes = plan(8.4)
        C.quantize_scene_plan(scenes)
        self.assertNotIn("start_frame", scenes[0])

    def test_first_start_is_zero_even_if_it_is_not(self):
        scenes = [{"index": 1, "start": 0.01, "end": 5.0, "duration": 4.99}]
        self.assertEqual(C.quantize_scene_plan(scenes)[0]["start_frame"], 0)


class SeedAndPadTest(unittest.TestCase):
    def test_scene_seed(self):
        self.assertEqual(C.scene_seed(1000, 1), 1000 + 7919)
        self.assertEqual(C.scene_seed(1000, 3), 1000 + 3 * 7919)
        self.assertEqual(C.scene_seed(2_147_483_000, 2), (2_147_483_000 + 2 * 7919) % 2_147_483_647)       # 範囲に収める
        self.assertEqual([C.scene_seed(5, i) for i in (1, 2)], [C.scene_seed(5, i) for i in (1, 2)])
        self.assertNotEqual(C.scene_seed(5, 1), C.scene_seed(6, 1))

    def test_pad_seconds(self):
        self.assertAlmostEqual(C.pad_seconds(150, 141 / 24), 9 / 24 + 0.25)         # 短い: 足りない分 + 余白
        self.assertAlmostEqual(C.pad_seconds(130, 141 / 24), 0.25)                  # 長い: 余白だけ(余りは -frames:v が切る)
        self.assertEqual(C.pad_seconds(130, None), C.PAD_UNKNOWN_S)                 # H3 の長さが分からない


class ArgvTest(unittest.TestCase):
    def test_finish_args_exact(self):
        argv = C.finish_args("ffmpeg", "raw.mp4", "out.mp4.part", 201, 1024, 576, 24, 0.5)
        self.assertEqual(argv, ["ffmpeg", "-y", "-v", "error", "-i", "raw.mp4", "-an", "-frames:v", "201", "-r", "24",
                                "-vf", "scale=1024:576:flags=lanczos,tpad=stop_mode=clone:stop_duration=0.500,format=yuv420p",
                                "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-f", "mp4", "out.mp4.part"])

    def test_finish_args_without_pad_and_minimum_one_frame(self):
        argv = C.finish_args("ffmpeg", Path("raw.mp4"), Path("o.part"), 0, 768, 448)
        self.assertEqual(argv[argv.index("-frames:v") + 1], "1")
        self.assertEqual(argv[argv.index("-vf") + 1], "scale=768:448:flags=lanczos,format=yuv420p")
        self.assertNotIn("tpad", " ".join(argv))
        self.assertEqual(argv[argv.index("-r") + 1], "24")

    def test_concat_list_quotes_and_posix(self):
        text = C.concat_list([Path("/w/scene_001.mp4"), "/w/it's/scene_002.mp4"])
        self.assertEqual(text, "file '/w/scene_001.mp4'\nfile '/w/it'\\''s/scene_002.mp4'\n")

    def test_concat_and_mux_args_exact(self):
        self.assertEqual(C.concat_args("ffmpeg", "list.txt", "v.mp4.part"),
                         ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", "list.txt", "-c", "copy", "-f", "mp4", "v.mp4.part"])
        self.assertEqual(C.mux_args("ffmpeg", "v.mp4", "song.wav", "mv.mp4.part", 187.23456),
                         ["ffmpeg", "-y", "-v", "error", "-i", "v.mp4", "-i", "song.wav", "-map", "0:v:0", "-map", "1:a:0", "-t", "187.235",
                          "-c:v", "copy", "-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart", "-f", "mp4", "mv.mp4.part"])


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *map(str, args)], check=True)


def probe_video(path) -> dict:
    """映像ストリームの (読んだコマ数, fps, 幅, 高さ, コーデック, ピクセル形式)。"""
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames", "-show_entries",
                        "stream=nb_read_frames,r_frame_rate,width,height,codec_name,pix_fmt:format=duration", "-of", "json", str(path)],
                       capture_output=True, check=True)
    info = json.loads(r.stdout)
    st = info["streams"][0]
    return {"frames": int(st["nb_read_frames"]), "fps": st["r_frame_rate"], "size": (st["width"], st["height"]), "codec": st["codec_name"],
            "pix_fmt": st["pix_fmt"], "duration": float(info["format"]["duration"])}


class ShortArgsTest(unittest.TestCase):
    def test_finish_args_default_drops_the_audio_and_keep_audio_keeps_it_aac(self):
        argv = C.finish_args("ffmpeg", "raw.mp4", "out.part", 144, 768, 448)
        self.assertIn("-an", argv)
        self.assertNotIn("-c:a", argv)
        keep = C.finish_args("ffmpeg", "raw.mp4", "out.part", 144, 768, 448, C.FPS, 0.5, keep_audio=True)
        self.assertNotIn("-an", keep)
        self.assertEqual((keep[keep.index("-c:a") + 1], keep[keep.index("-ar") + 1], keep[keep.index("-ac") + 1]), ("aac", "48000", "2"))
        self.assertEqual(keep[keep.index("-af") + 1], "apad=whole_dur=6.000")                         # 映像(144 コマ = 6 秒)の長さにそろえる
        self.assertEqual(keep[keep.index("-t") + 1], "6.000")
        self.assertEqual(keep[keep.index("-frames:v") + 1], "144")
        self.assertIn("tpad=stop_mode=clone:stop_duration=0.500", keep[keep.index("-vf") + 1])
        self.assertEqual(keep[-3:], ["-f", "mp4", "out.part"])

    def test_concat_args_keeps_the_audio_unless_asked(self):
        base = C.concat_args("ffmpeg", "list.txt", "out.part")
        self.assertEqual(base, ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", "list.txt", "-c", "copy", "-f", "mp4", "out.part"])
        none = C.concat_args("ffmpeg", "list.txt", "out.part", drop_audio=True, faststart=True)
        self.assertEqual(none[none.index("-c") + 1:], ["copy", "-an", "-movflags", "+faststart", "-f", "mp4", "out.part"])

    def test_mix_args_puts_the_song_14_db_under_the_narration(self):
        argv = C.mix_args("ffmpeg", "v.mp4", "nar.wav", "song.wav", "out.part", 20.5, 30.0)
        self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == "-i"], ["v.mp4", "nar.wav", "song.wav"])
        graph = argv[argv.index("-filter_complex") + 1]
        self.assertEqual(graph, "[1:a]aformat=channel_layouts=stereo[nar];"
                                "[2:a]aformat=channel_layouts=stereo,atrim=duration=20.500,asetpts=PTS-STARTPTS,volume=-14dB,afade=t=out:st=18.500:d=2.000[bgm];"
                                "[nar][bgm]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]")
        self.assertEqual((argv[argv.index("-t") + 1], argv[argv.index("-c:v") + 1], argv[argv.index("-c:a") + 1], argv[argv.index("-b:a") + 1]), ("20.500", "copy", "aac", "256k"))
        self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == "-map"], ["0:v:0", "[a]"])
        self.assertEqual(argv[-3:], ["-f", "mp4", "out.part"])

    def test_mix_args_fades_at_the_end_of_a_short_song_and_handles_a_tiny_one(self):
        graph = C.mix_args("ffmpeg", "v", "n", "s", "o", 40.0, 12.0)[11]
        self.assertIn("atrim=duration=12.000", graph)
        self.assertIn("afade=t=out:st=10.000:d=2.000", graph)
        graph = C.mix_args("ffmpeg", "v", "n", "s", "o", 40.0, None)[11]            # 曲の長さが分からなければ、読み上げの長さまで
        self.assertIn("atrim=duration=40.000", graph)
        graph = C.mix_args("ffmpeg", "v", "n", "s", "o", 40.0, 1.0)[11]
        self.assertIn("afade=t=out:st=0.000:d=1.000", graph)


def has_audio(path) -> bool:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(path)],
                       capture_output=True, check=True)
    return bool(r.stdout.strip())


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg / ffprobe が PATH に無い")
class RealFfmpegTest(unittest.TestCase):
    """作った引数を、本物の ffmpeg で動かして確かめる(小さい動画)。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        # H3 の出力に似せた生のクリップ: 24fps・映像 141 コマ(17n+5)+ 音声つき
        cls.raw = cls.root / "raw_141.mp4"
        ffmpeg("-f", "lavfi", "-i", "testsrc=size=96x64:rate=24", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000",
               "-frames:v", 141, "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", cls.raw)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def finish(self, frames, size=(96, 64), raw_s=141 / 24, name=None):
        out = self.root / (name or f"clip_{frames}.mp4")
        tmp = out.with_name(out.name + ".part")
        argv = C.finish_args("ffmpeg", self.raw, tmp, frames, size[0], size[1], C.FPS, C.pad_seconds(frames, raw_s))
        subprocess.run(argv, check=True, capture_output=True)
        tmp.replace(out)
        return out

    def test_clip_has_exactly_the_requested_number_of_frames(self):
        for frames in (150, 141, 130, 24, 1):         # 足りない(最後のコマを複製して足す)/ ちょうど / 長すぎる(切る)
            info = probe_video(self.finish(frames))
            self.assertEqual(info["frames"], frames, frames)
            self.assertEqual((info["fps"], info["codec"], info["pix_fmt"]), ("24/1", "h264", "yuv420p"))

    def test_clip_is_scaled_and_has_no_audio(self):
        out = self.finish(100, size=(80, 48), name="scaled.mp4")
        self.assertEqual(probe_video(out)["size"], (80, 48))
        self.assertTrue(has_audio(self.raw))
        self.assertFalse(has_audio(out))

    def test_padding_clones_the_last_frame(self):
        """足した分のコマは、最後のコマと同じ絵(止まる)。元の動きの中のコマとは違う絵。"""
        from PIL import Image, ImageChops, ImageStat
        out = self.finish(150, name="pad.mp4")

        def shot(n):
            png = self.root / f"f{n}.png"
            ffmpeg("-i", out, "-vf", f"select=eq(n\\,{n})", "-frames:v", 1, png)
            with Image.open(png) as im:
                return im.convert("RGB")

        def diff(a, b):
            return sum(ImageStat.Stat(ImageChops.difference(a, b)).mean)
        last, padded_end, earlier = shot(140), shot(149), shot(120)       # 元の最後(141 コマ目)/ 足した最後 / 元の途中
        self.assertLess(diff(last, padded_end), 3.0)
        self.assertGreater(diff(last, earlier), 3 * diff(last, padded_end) + 1.0)

    def test_concat_and_mux_gives_the_exact_total_and_the_song(self):
        frames = [150, 130, 141]
        clips = [self.finish(n) for n in frames]
        listing, video, final = self.root / "list.txt", self.root / "video.mp4", self.root / "final.mp4"
        listing.write_text(C.concat_list(clips), encoding="utf-8")
        subprocess.run(C.concat_args("ffmpeg", listing, video), check=True, capture_output=True)
        self.assertEqual(probe_video(video)["frames"], sum(frames))
        song = self.root / "song.wav"
        ffmpeg("-f", "lavfi", "-i", "sine=frequency=330:sample_rate=44100:duration=20", "-ac", 2, song)
        duration = sum(frames) / 24
        subprocess.run(C.mux_args("ffmpeg", video, song, final, duration), check=True, capture_output=True)
        info = probe_video(final)
        self.assertEqual(info["frames"], sum(frames))
        self.assertAlmostEqual(info["duration"], duration, delta=0.05)
        self.assertTrue(has_audio(final))
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name,duration", "-of", "json", str(final)],
                           capture_output=True, check=True)
        audio = json.loads(r.stdout)["streams"][0]
        self.assertEqual(audio["codec_name"], "aac")
        self.assertAlmostEqual(float(audio["duration"]), duration, delta=0.1)       # 曲は -t で映像の長さに切られる


    def test_keep_audio_clip_has_exact_frames_audio_of_the_same_length_and_survives_concat(self):
        clips = []
        for frames in (150, 130):                                                     # H3 の 141 コマ(5.875 秒)より長い・短い
            out = self.root / f"keep_{frames}.mp4"
            tmp = out.with_name(out.name + ".part")
            subprocess.run(C.finish_args("ffmpeg", self.raw, tmp, frames, 96, 64, C.FPS, C.pad_seconds(frames, 141 / 24), keep_audio=True), check=True, capture_output=True)
            tmp.replace(out)
            clips.append(out)
            self.assertEqual(probe_video(out)["frames"], frames)
            self.assertTrue(has_audio(out))
            r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name,sample_rate,channels,duration", "-of", "json", str(out)],
                               capture_output=True, check=True)
            audio = json.loads(r.stdout)["streams"][0]
            self.assertEqual((audio["codec_name"], audio["sample_rate"], audio["channels"]), ("aac", "48000", 2))
            self.assertAlmostEqual(float(audio["duration"]), frames / 24, delta=0.1)
        listing, kept, silent = self.root / "keep_list.txt", self.root / "kept.mp4", self.root / "silent.mp4"
        listing.write_text(C.concat_list(clips), encoding="utf-8")
        subprocess.run(C.concat_args("ffmpeg", listing, kept, faststart=True), check=True, capture_output=True)
        self.assertEqual(probe_video(kept)["frames"], 280)
        self.assertTrue(has_audio(kept))                                              # つないでも音声は残る
        subprocess.run(C.concat_args("ffmpeg", listing, silent, drop_audio=True), check=True, capture_output=True)
        self.assertEqual(probe_video(silent)["frames"], 280)
        self.assertFalse(has_audio(silent))

    def test_mix_gives_the_narration_length_with_both_voices_and_never_loops_a_short_song(self):
        frames = [150, 130, 141]
        clips = [self.finish(n) for n in frames]
        listing, video = self.root / "mix_list.txt", self.root / "mix_video.mp4"
        listing.write_text(C.concat_list(clips), encoding="utf-8")
        subprocess.run(C.concat_args("ffmpeg", listing, video), check=True, capture_output=True)
        duration = sum(frames) / 24                                                   # 17.5 秒
        narration, song = self.root / "nar.wav", self.root / "bgm.wav"
        ffmpeg("-f", "lavfi", "-i", f"sine=frequency=500:sample_rate=44100:duration={duration}", "-ac", 2, narration)
        ffmpeg("-f", "lavfi", "-i", "sine=frequency=200:sample_rate=44100:duration=8", "-ac", 1, song)         # 読み上げより短い、モノラルの曲
        final = self.root / "mix_final.mp4"
        subprocess.run(C.mix_args("ffmpeg", video, narration, song, final, duration, 8.0), check=True, capture_output=True)
        info = probe_video(final)
        self.assertEqual(info["frames"], sum(frames))
        self.assertAlmostEqual(info["duration"], duration, delta=0.05)

        def level(start, length):
            """音声の start〜start+length 秒の平均の音量(dB)。"""
            r = subprocess.run(["ffmpeg", "-v", "info", "-ss", str(start), "-t", str(length), "-i", str(final), "-vn", "-af", "volumedetect", "-f", "null", "-"],
                               capture_output=True, check=True)
            return float(re.search(r"mean_volume: (-?[\d.]+) dB", r.stderr.decode()).group(1))
        under_bgm, narration_only = level(2.0, 3.0), level(12.0, 3.0)
        self.assertGreater(under_bgm, narration_only - 1.0)                           # 曲がある所は、読み上げだけの所より小さくならない(混ざっている)
        self.assertLess(abs(under_bgm - narration_only), 3.0)                         # -14 dB の BGM は、読み上げを少し持ち上げるだけ
        r = subprocess.run(["ffmpeg", "-v", "info", "-i", str(final), "-vn", "-af", "silencedetect=n=-60dB:d=0.5", "-f", "null", "-"], capture_output=True, check=True)
        self.assertNotIn("silence_start", r.stderr.decode())                          # 曲が終わっても、読み上げは最後まで続く(無音にならない)


if __name__ == "__main__":
    unittest.main()
