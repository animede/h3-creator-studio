"""RTX Video Super Resolution(NVIDIA の nvidia-vfx)で動画・画像を 2〜4 倍に拡大する(動画は映像だけ。音声は呼び出し側の ffmpeg が元の動画から付ける)。

管理役(studio/upscale.py の run_rtxvsr)が、components/upscale/rtxvsr/.venv の Python(torch cu128 + nvidia-vfx + av + numpy)で子プロセスとして
動かす。管理役の venv には torch も nvidia-vfx も入れないので、ここは studio を import しない(その venv だけで動く)。
NVIDIA の MIT ライセンスのサンプル https://github.com/NVIDIA-Maxine/nvidia-vfx-python-samples(video_super_resolution.py・video_io_utils.py)を元に、
8 ビットの RGB に変えて渡し(10 ビットなどの入力も 8 ビットになる)、H.264 で書き、進みを「progress 完了数/総数」の行で出すようにしたもの。
拡大後の寸法が奇数なら(奇数の寸法のアップロードを ×3 にした時など)、yuv420p で書けるよう右と下を 1px 削って偶数にする。
nvidia-vfx 自体は NVIDIA の利用条件のもとで利用者が入れる(同梱しない)。

  python upscale_rtxvsr.py <入力.mp4> <出力.mp4> --scale 2 [--quality ULTRA] [--device 0]
  python upscale_rtxvsr.py --image <入力.png/jpg/webp> <出力.png> --scale 2 [--quality ULTRA]   (画像 1 枚。透過は捨てて RGB の PNG に書く)
標準出力: 10 コマごとに「progress i/n」、終わったら「done」。失敗は標準エラーに理由を 1 行出して終了コード 1。
"""
import argparse
import sys
import time
from fractions import Fraction
from pathlib import Path

ENCODERS = (("h264_nvenc", "yuv420p"), ("libx264", "yuv420p"), ("mpeg4", "yuv420p"))     # 先に開けたものを使う(サンプルと同じ流儀)
BIT_RATE = 16_000_000
MAX_EDGE = 8192
COLOR_FIELDS = ("color_range", "colorspace", "color_trc", "color_primaries")
PROGRESS_EVERY = 10


def fail(reason: str) -> None:
    print(f"error: {reason}", file=sys.stderr, flush=True)
    sys.exit(1)


def parse_args():
    p = argparse.ArgumentParser(description="RTX Video Super Resolution で動画を拡大する(映像だけ)。--image なら画像 1 枚")
    p.add_argument("--image", action="store_true", help="入力を画像 1 枚として拡大し、PNG に書く")
    p.add_argument("input")
    p.add_argument("output")
    p.add_argument("--scale", type=int, choices=(2, 3, 4), default=2)
    p.add_argument("--quality", default="ULTRA", help="nvvfx の QualityLevel の名前(ULTRA / HIGH / MEDIUM / LOW など)")
    p.add_argument("--device", type=int, default=0, help="CUDA の装置番号(管理役は CUDA_VISIBLE_DEVICES で寄せるので、ふだん 0)")
    return p.parse_args()


def copy_color(src, dst) -> None:
    """色の解釈(範囲・色空間など)を入力から写す(写さないと再生側が別の色で解釈することがある)。"""
    for f in COLOR_FIELDS:
        v = getattr(src, f, None)
        if v is not None:
            try:
                setattr(dst, f, v)
            except (AttributeError, ValueError):
                pass


def open_output(av, path: Path, width: int, height: int, rate: Fraction, src_ctx):
    """出力を開く。エンコーダは h264_nvenc → libx264 → mpeg4 の順(無い・開けないものは次へ)。"""
    for name, pix_fmt in ENCODERS:
        container = av.open(str(path), mode="w")
        try:
            stream = container.add_stream(name, rate=rate)
            stream.width, stream.height, stream.pix_fmt, stream.bit_rate = width, height, pix_fmt, BIT_RATE
            copy_color(src_ctx, stream.codec_context)
            stream.codec_context.open()
        except Exception:  # noqa: BLE001  そのエンコーダが無い・開けない
            container.close()
            continue
        return container, stream, name
    return None, None, None


def to_tensor(np, torch, frame, device: str):
    """デコードしたコマ → CUDA の (3, H, W) float32 [0, 1](nvvfx の RGB8 の入力)。"""
    arr = frame.to_ndarray(format="rgb24")
    t = torch.from_numpy(np.ascontiguousarray(arr)).to(device)
    return (t.permute(2, 0, 1).float() / 255.0).contiguous()


def to_frame(av, output):
    """nvvfx の出力 (3, H, W) float32 → PyAV のコマ(rgb24。エンコーダが yuv420p に変える)。"""
    arr = (output.clamp(0.0, 1.0) * 255.0).round().byte().permute(1, 2, 0).contiguous().cpu().numpy()
    return av.VideoFrame.from_ndarray(arr, format="rgb24")


def load_sr(VideoSuperRes, args, quality, w: int, h: int, out_w: int, out_h: int):
    """VideoSuperRes を入力・出力の寸法(出力 = 入力 × 倍率でないと nvvfx が受け付けない)で読み込む。"""
    sr = VideoSuperRes(device=args.device, quality=quality, strength=1.0, image_encoding=VideoSuperRes.ImageEncoding.RGB8)
    sr.input_width, sr.input_height, sr.output_width, sr.output_height = w, h, out_w, out_h
    try:
        sr.load()
    except Exception as e:  # noqa: BLE001  ドライバが古い・対応しない GPU など
        fail(f"RTX Video Super Resolution を読み込めません: {e}")
    return sr


def upscale_image(av, np, torch, VideoSuperRes, args, quality, device: str, stream_ptr) -> None:
    """画像 1 枚(PyAV の image2 で png・jpg・webp を読む)を拡大して PNG(rgb24)に書く。"""
    src, dst = Path(args.input), Path(args.output)
    try:
        with av.open(str(src)) as inp:
            frame = next(inp.decode(video=0), None)
    except Exception as e:  # noqa: BLE001  壊れた・対応しない画像
        fail(f"画像を読めません: {e}")
    if frame is None:
        fail("画像を読めませんでした")
    w, h = frame.width, frame.height
    out_w, out_h = w * args.scale, h * args.scale
    if max(out_w, out_h) > MAX_EDGE:
        fail(f"拡大後の寸法が大きすぎます: {out_w}×{out_h}(上限 {MAX_EDGE})")
    started = time.time()
    sr = load_sr(VideoSuperRes, args, quality, w, h, out_w, out_h)
    try:
        result = sr.run(to_tensor(np, torch, frame, device), stream_ptr=stream_ptr)       # to_tensor が rgb24 に変える(透過は捨てる)
        out_frame = to_frame(av, torch.from_dlpack(result.image).clone())
    finally:
        sr.close()
    dst.parent.mkdir(parents=True, exist_ok=True)
    out = av.open(str(dst), mode="w", format="image2", options={"update": "1"})       # update: 連番でない 1 枚の名前に書く
    try:
        stream = out.add_stream("png", rate=1)
        stream.width, stream.height, stream.pix_fmt = out_w, out_h, "rgb24"
        for packet in stream.encode(out_frame):
            out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)
    finally:
        out.close()
    print("progress 1/1", flush=True)
    print(f"done {w}x{h} -> {out_w}x{out_h} in {time.time() - started:.1f}s", flush=True)


def main() -> None:
    args = parse_args()
    src, dst = Path(args.input), Path(args.output)
    if not src.is_file():
        fail(f"入力の{'画像' if args.image else '動画'}がありません: {src}")
    try:
        import av
        import numpy as np
        import torch
        from nvvfx import VideoSuperRes
    except ImportError as e:
        fail(f"RTX Video Super Resolution の環境が壊れています({e})。編集タブの「入れる」でもう一度入れてください")
    if not torch.cuda.is_available():
        fail("CUDA の GPU が見えません(NVIDIA のドライバを確かめてください)")
    try:
        quality = VideoSuperRes.QualityLevel[args.quality.strip().upper()]
    except KeyError:
        fail(f"--quality が不正です: {args.quality}({' / '.join(VideoSuperRes.QualityLevel.__members__)})")
    torch.cuda.set_device(args.device)
    device = f"cuda:{args.device}"
    stream_ptr = torch.cuda.current_stream().cuda_stream
    if args.image:
        upscale_image(av, np, torch, VideoSuperRes, args, quality, device, stream_ptr)
        return

    inp = av.open(str(src))
    vs = inp.streams.video[0]
    vs.thread_type = "AUTO"
    w, h = vs.codec_context.width, vs.codec_context.height
    total = vs.frames or 0
    if not total and vs.duration and vs.time_base and vs.average_rate:
        total = round(float(vs.duration * vs.time_base) * float(vs.average_rate))
    rate = Fraction(vs.average_rate if vs.average_rate else 30).limit_denominator(10000)
    out_w, out_h = w * args.scale, h * args.scale
    if max(out_w, out_h) > MAX_EDGE:
        fail(f"拡大後の寸法が大きすぎます: {out_w}×{out_h}(上限 {MAX_EDGE})")
    enc_w, enc_h = out_w - out_w % 2, out_h - out_h % 2         # yuv420p は偶数の寸法だけ。奇数なら右と下の 1px を削る

    sr = load_sr(VideoSuperRes, args, quality, w, h, out_w, out_h)
    dst.parent.mkdir(parents=True, exist_ok=True)
    container, stream, encoder = open_output(av, dst, enc_w, enc_h, rate, vs.codec_context)
    if stream is None:
        sr.close()
        fail(f"使える H.264 のエンコーダがありません({' / '.join(n for n, _ in ENCODERS)} を試しました)")
    print(f"encoder {encoder} {w}x{h} -> {enc_w}x{enc_h} @ {float(rate):.3f}fps, {total} frames", flush=True)

    started, processed = time.time(), 0
    try:
        for frame in inp.decode(vs):
            result = sr.run(to_tensor(np, torch, frame, device), stream_ptr=stream_ptr)
            rgb = torch.from_dlpack(result.image).clone()
            if (enc_w, enc_h) != (out_w, out_h):
                rgb = rgb[:, :enc_h, :enc_w]
            out_frame = to_frame(av, rgb)
            copy_color(vs.codec_context, out_frame)
            for packet in stream.encode(out_frame):
                container.mux(packet)
            processed += 1
            if processed % PROGRESS_EVERY == 0:
                print(f"progress {processed}/{max(total, processed)}", flush=True)
        for packet in stream.encode(None):
            container.mux(packet)
    finally:
        container.close()
        inp.close()
        sr.close()
    if processed == 0:
        fail("動画からコマを読めませんでした")
    print(f"progress {processed}/{processed}", flush=True)
    print(f"done {processed} frames in {time.time() - started:.1f}s", flush=True)


if __name__ == "__main__":
    main()
