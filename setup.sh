#!/usr/bin/env bash
# YUZU Creator Studio のセットアップ(Linux)。uv を用意し、管理役の .venv を作って、scripts/setup.py に渡す。
#
#   ./setup.sh                  # 部品の取り込み + 環境 + モデル(合計 約 150GB、回線次第で数時間)
#   ./setup.sh --no-models      # 環境だけ(モデルは後で)
#   ./setup.sh --only h3,ace    # 指定した部品だけ
#   ./setup.sh --llm            # 同梱 LLM(約 5GB)も先に取得する
#   ./setup.sh --mv             # MV の音声処理の道具(Demucs + librosa、CPU 版 torch。約 1.3GB)も用意する
#   ./setup.sh --only mvtools   # その道具だけ
# 何度実行してもよい。済んでいる所は飛ばし、途中で止まっても続きから進む。
set -e
cd "$(dirname "$0")"

UV_NEW=0
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  echo "[1/3] uv を入れます"
  if command -v curl >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- https://astral.sh/uv/install.sh | sh
  else
    echo "curl も wget も見つかりません。どちらかを入れてからやり直してください(例: sudo apt install curl)" >&2
    exit 1
  fi
  UV_NEW=1
else
  echo "[1/3] uv: $(uv --version)"
fi
if ! command -v uv >/dev/null 2>&1; then
  echo "uv を入れましたが見つかりません。ターミナルを開き直して ./setup.sh をもう一度実行してください。" >&2
  exit 1
fi

echo "[2/3] 管理役の Python 環境(.venv)"
if [ ! -x .venv/bin/python ]; then
  uv venv --python 3.12 .venv
fi
uv pip install --python .venv/bin/python -r requirements.txt

echo "[3/3] 部品の取り込みと準備"
rc=0
.venv/bin/python scripts/setup.py "$@" || rc=$?

if [ "$UV_NEW" = "1" ]; then
  echo
  echo "============================================================"
  echo " 今回 uv を新しく入れました。"
  echo " このターミナルで uv コマンドを自分で使うなら、ターミナルを"
  echo " 開き直してください(./run.sh だけなら、このままで動きます)。"
  echo "============================================================"
fi
exit "$rc"
