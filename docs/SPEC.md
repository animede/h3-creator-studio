# h3-creator-studio 仕様(第 1 版)

誰でも使える簡易な画像・動画・音楽の制作スタジオ。MiniMax-H3(動画+音声、静止画)、MiniMax-H3-Image(画像)、ACE-Step(音楽)、AivisSpeech(TTS)を 1 つの画面から使う。GPU には常に 1 つの部品だけを載せ、VRAM と CPU メモリを見ながら載せ替えるのがこのアプリの役目。

## 決めたこと

- 対象: ComfyUI の利用者。Windows を主に、Linux でも動く。NVIDIA GPU。
- 要件: VRAM の下限は設けない(6GB でも画角を絞れば動く)。推奨 12GB 以上。**CPU メモリ 32GB 前提**。NVMe 推奨。
- 部品(すべて既存のリポジトリをそのまま子プロセスとして使う。中身には手を入れない):

| 部品 | 役目 | ポート | 準備確認 | GPU を空ける方法 | 起動(作業ディレクトリは部品のフォルダ) |
|---|---|---|---|---|---|
| `minimax-h3` | 動画(T2VA / FL2VA / Ref2VA)と静止画(T2I / Ref2I) | 8611 | `GET /api/status`(`busy` を含む JSON) | `POST /api/admin/unload`(実行中は 409)、戻すのは `POST /api/admin/reload` | `<venv>/python -u -m uvicorn app:app --host 127.0.0.1 --port 8611`。venv は `venv/` か `.venv/` |
| `minimax-h3-image-server` | 画像(T2I、参照編集) | 8633 | `GET /api/health`(`loaded`) | `POST /api/admin/unload`(409)、`POST /api/admin/load` | `.venv/python -m uvicorn app.main:app --host 127.0.0.1 --port 8633` |
| `ACE-Step-1.5-fast` | 音楽 | 8001 | `GET /health`(`data.models_initialized`) | **無い。プロセスを止める** | Linux `run_api_server_fast.sh`、Windows `run_api_server_fast.bat`。環境変数 `ACESTEP_API_HOST=127.0.0.1 ACESTEP_API_PORT=8001` |
| `AivisSpeech-Engine` | TTS(**CPU で動かす。GPU は使わない**) | 10101 | `GET /version` | 不要 | 配布版の実行ファイル `run`(Windows は `run.exe`)に `--host 127.0.0.1 --port 10101`。`--use_gpu` は付けない |

- 部品の置き場所: 既定は `components/<名前>/`。`setup` が取り込む。既に別の場所にあれば、設定ファイルでそのフォルダを指すだけで使える(取り込みもモデル取得も飛ばす)。各部品は普通の git リポジトリのままなので、個別に起動も `git pull` もできる。
- 画面: 上段にタブ(画像・動画・音楽)。各タブは左に操作、右上に生成物を大きく、その下にそのタブで作ったもの。右側はギャラリーに切り替えられる(全て・画像・動画・音楽で絞り込み)。入力はすべてドラッグ&ドロップで、ファイルも、ギャラリーやタブの生成物も同じ操作で入力欄に落とせる。
- LLM(プロンプト強化、歌詞): 管理役が Gemma 4 E4B(llama.cpp の CUDA 版 `llama-server`)を 1 つだけ持つ。GPU で生成する直前に止め、生成中は起こさない(Easy Music の方式)。各部品の内蔵 LLM は使わない。
- 第 1 版で後回し: 認証、LAN 公開、複数 GPU、ジョブの同時実行、プリセット編集、リップシンク MV、LTX-2.5。

## 実装方針

1. 管理役は GPU を使わない FastAPI(Python 3.11+、依存は FastAPI / uvicorn / httpx / psutil / tomli 程度)。新規に書く。考え方は `diffusers-movie-server` のゲートウェイ(排他、unload、実行中は 409)を引き継ぐ。
2. 画面から部品を直接呼ばず、**管理役がすべて仲介**する(載せ替えを生成の手前で挟む、生成物を 1 つのギャラリーに集める、1 オリジンで済ませる)。
3. 画面は素の HTML + JavaScript + CSS(ビルド不要、Node 不要)。
4. 生成はすべて非同期ジョブ(投げて、ポーリング)。H3 動画の同期 API は管理役が裏で呼び、`/api/progress` で進捗を取る形に包む。
5. 生成物は `outputs/` に集め、台帳(SQLite 1 ファイル `outputs/studio.db`)に 種類・タブ・パラメータ・日時 を記録。ギャラリーは台帳を見る。
6. メモリ監視: nvidia-smi(2 秒おき)と各子プロセスの RSS(psutil)。Windows では共有 GPU メモリの増加(=VRAM あふれ)を見張って警告。
7. 載せ替えは 1 本の状態機械: 「今 GPU にいる部品を unload(ACE は停止)→ 空きを確認 → 次を起動 or reload → 準備完了を待つ」。
8. `setup.bat` / `setup.sh`: uv 導入 → 部品の取り込み(既存は飛ばす)→ 各部品の setup 呼び出し → AivisSpeech 配布版(約 220MB の 7z)の取得。

## 作業の順序

1. 管理役の骨組み(設定、部品の起動と停止、準備確認、メモリ監視、載せ替えの状態機械)。画面はメモリ計と部品の状態表示だけ。
2. 画像タブ(H3-Image)。生成の流れとギャラリーの土台。
3. 動画タブ(H3: T2VA / FL2VA / Ref2VA / 静止画)と TTS。
4. 音楽タブ(ACE、簡単モード)と LLM。
5. `setup.bat` / `setup.sh`、README(部品の更新手順を含む)、Windows 実機での確認。
