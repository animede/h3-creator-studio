#!/usr/bin/env python
# Ported from Minimax-H3-lipsync-mv (Apache-2.0, Copyright 2026 YUZUKI): app/services/audio.py の
# _DEMUCS_SCRIPT と separate_vocals。単体のスクリプトにして、伴奏の書き出し、進み具合の行、
# JSON 1 行の結果、失敗時の終了コードを加えた(modified)。
# SPDX-License-Identifier: Apache-2.0
"""Demucs(htdemucs)で曲をボーカルと伴奏に分ける。

管理役の venv には torch を入れないので、道具用の venv(components/mvtools/.venv)の Python で子プロセスとして動かす。
このファイルは studio を import しない(単体で動く)。

  python separate.py <input_audio> <out_dir> [--device auto|cpu|cuda] [--model htdemucs]

入力は 44.1kHz ステレオの WAV(管理役が ffmpeg でそろえたもの)を想定する。out_dir に vocals.wav と
accompaniment.wav(ボーカル以外のステムの合計)を PCM_16 で書く。進み具合は stderr に 1 行ずつ
「progress 完了数/総数」の形で出し、最後に JSON を 1 行だけ stdout に出す:
  {"vocals": "...", "accompaniment": "...", "seconds": 41.2, "device": "cpu"}
失敗したら理由を stderr の最後の行(「エラー: …」)に書いて終了コード 1。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import traceback
import warnings
from pathlib import Path

DEFAULT_MODEL = "htdemucs"
OVERLAP = 0.25            # lipsync-mv と同じ(split=True, overlap=0.25)


class UserError(Exception):
    """原因が利用者に分かる失敗(トレースバックは出さず、理由だけ stderr に書く)。"""


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def pick_device(want: str, torch) -> str:
    cuda = bool(torch.cuda.is_available())
    if want == "cuda" and not cuda:
        log("警告: CUDA が使えないので CPU で動かします(この環境の torch は CPU 版かもしれません)")
    return "cuda" if cuda and want in ("auto", "cuda") else "cpu"


def load_audio(path: Path, samplerate: int):
    """(2, フレーム数)の float32 テンソル。モノラルは 2 チャンネルに複製し、3 チャンネル以上は先頭の 2 つを使う。"""
    import numpy as np
    import soundfile as sf
    import torch
    try:
        data, sr = sf.read(str(path), dtype="float32", always_2d=True)       # (フレーム数, チャンネル数)
    except Exception as e:  # noqa: BLE001
        raise UserError(f"音声を読めません(44.1kHz の WAV にしてから渡してください): {e}") from e
    if data.shape[1] == 1:
        data = np.repeat(data, 2, axis=1)
    elif data.shape[1] > 2:
        data = data[:, :2]
    wav = torch.from_numpy(np.ascontiguousarray(data.T))
    if sr != samplerate:                                                     # 管理役は 44.1kHz にそろえて渡す。単体で使う場合の保険
        import julius
        wav = julius.resample_frac(wav, sr, samplerate)
    return wav


def write_wav(path: Path, stem, samplerate: int) -> None:
    """(チャンネル数, フレーム数)のテンソルを PCM_16 の WAV に書く。書き終えてから置き換える(途中で止まっても壊れたファイルを残さない)。"""
    import numpy as np
    import soundfile as sf
    data = stem.detach().cpu().numpy().T
    peak = float(np.abs(data).max()) if data.size else 0.0
    data = data / max(1.01 * peak, 1.0)                  # 1.0 をはみ出すときだけ全体を縮める(Demucs の clip_mode=rescale と同じ)
    tmp = path.with_name(path.name + ".part")
    sf.write(str(tmp), data, samplerate, subtype="PCM_16", format="WAV")
    os.replace(tmp, path)


def load_model(name: str):
    """Demucs のモデルを読み込む。初回の重みの取得は、\\r で書き換える進捗バーが stderr の行に向かないので、バー無しにする
    (取得の開始は torch が「Downloading: …」の 1 行で出す)。"""
    import torch
    from demucs.pretrained import get_model
    original = torch.hub.load_state_dict_from_url
    torch.hub.load_state_dict_from_url = lambda *a, **kw: original(*a, **{**kw, "progress": False})
    try:
        return get_model(name)
    finally:
        torch.hub.load_state_dict_from_url = original


def report_progress_by_line() -> bool:
    """demucs の分割処理が使う tqdm(\\r で書き換える進捗バー)を、1 行ずつ「progress 完了数/総数」を出す形に差し替える。"""
    try:
        import demucs.apply as da
    except ImportError:
        return False

    class _Lines:
        @staticmethod
        def tqdm(iterable, **_kw):
            items = list(iterable)
            total = len(items)
            log(f"progress 0/{total}")
            for done, item in enumerate(items, start=1):
                yield item                               # 呼び出し側が次を取りに来た時点 = この 1 つの処理が終わった時点
                log(f"progress {done}/{total}")

    if not hasattr(da, "tqdm"):
        return False
    da.tqdm = _Lines
    return True


def separate(src: Path, out_dir: Path, device: str = "auto", model_name: str = DEFAULT_MODEL) -> dict:
    t0 = time.monotonic()
    if not src.is_file():
        raise UserError(f"入力の音声がありません: {src}")
    try:
        import torch
        from demucs.apply import apply_model
    except ImportError as e:
        raise UserError(f"必要なパッケージがありません({e.name}): この Python には Demucs / torch が入っていません。"
                        "setup --mv を実行してください") from e
    dev = pick_device(device, torch)
    log(f"device: {dev}")
    log(f"model: {model_name} を読み込んでいます(初回は重みを取得します)")
    try:
        model = load_model(model_name)
    except Exception as e:  # noqa: BLE001
        raise UserError(f"Demucs のモデル {model_name} を読み込めません(初回はネットワークが要ります): {e}") from e
    model.to(dev).eval()
    sr = int(model.samplerate)
    names = list(model.sources)
    if "vocals" not in names:
        raise UserError(f"このモデルにはボーカルのステムがありません: {names}")

    wav = load_audio(src, sr)
    log(f"audio: {wav.shape[1] / sr:.1f} 秒")
    wav = wav.to(dev)
    ref = wav.mean(0)                                        # モノラルの混合。Demucs の標準の正規化(平均 0・標準偏差 1)の基準
    mean, std = ref.mean(), ref.std() + 1e-8
    x = (wav - mean) / std
    progress = report_progress_by_line()
    random.seed(0)                                           # シフト(apply_model の shifts=1)の乱数を固定して、同じ入力なら同じ結果にする
    log("separating")
    with torch.no_grad():
        sources = apply_model(model, x[None], device=dev, split=True, overlap=OVERLAP, progress=progress)[0]
    sources = sources * std + mean
    vocals = sources[names.index("vocals")]
    accompaniment = sum(s for n, s in zip(names, sources) if n != "vocals")

    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    vocals_path, accompaniment_path = out_dir / "vocals.wav", out_dir / "accompaniment.wav"
    write_wav(vocals_path, vocals, sr)
    write_wav(accompaniment_path, accompaniment, sr)
    return {"vocals": str(vocals_path), "accompaniment": str(accompaniment_path),
            "seconds": round(time.monotonic() - t0, 1), "device": dev}


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    p = argparse.ArgumentParser(description="Demucs でボーカルと伴奏に分ける(結果の JSON を stdout に 1 行)")
    p.add_argument("input", help="入力の音声(44.1kHz ステレオの WAV を想定)")
    p.add_argument("out_dir", help="vocals.wav と accompaniment.wav を書くフォルダ")
    p.add_argument("--device", default="auto", choices=("auto", "cpu", "cuda"))
    p.add_argument("--model", default=DEFAULT_MODEL, help="Demucs のモデル名(既定 htdemucs)")
    args = p.parse_args(argv)
    warnings.filterwarnings("ignore")
    real_stdout, sys.stdout = sys.stdout, sys.stderr         # ライブラリの print が結果の JSON 行に混ざらないように、作業中は stdout を stderr へ向ける
    try:
        result = separate(Path(args.input), Path(args.out_dir), args.device, args.model)
    except UserError as e:
        log(f"エラー: {e}")
        return 1
    except KeyboardInterrupt:
        log("エラー: 中断されました")
        return 130
    except Exception as e:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        log(f"エラー: {type(e).__name__}: {e}")
        return 1
    finally:
        sys.stdout = real_stdout
    print(json.dumps(result, ensure_ascii=True), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
