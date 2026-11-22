# h3-creator-studio

**最新版: 2026-11-23 11:30 JST** — 止められない外部の部品(ACE-Step)が GPU にいても、空きがあれば止めずに同居する(大きな GPU の開発機向け)。断るときは次にやることを示す。変更の履歴は [GitHub の commits](https://github.com/animede/h3-creator-studio/commits/main)。

**mv-V3 や story のシンプル版を統合した、誰でも使える簡易な画像・動画・音楽コンテンツ制作スタジオ。**
ブラウザ 1 枚の画面で、画像・動画(音声つき)・音楽を作る。ComfyUI のようにノードをつなぐ必要はない。

中で動いているのは、次の 4 つの部品。どれも既存のリポジトリを、そのまま使っている。

| 部品 | 役目 |
|---|---|
| [MiniMax-H3](https://github.com/animede/Diffusers_minimax-h3) | 動画+音声(テキスト / 最初・最後のフレーム / 参照から)と静止画 |
| [MiniMax-H3-Image](https://github.com/animede/minimax-h3-image-server) | 画像(テキストから、参照画像で編集) |
| [ACE-Step](https://github.com/animede/ACE-Step-1.5-fast) | 音楽(歌つき、インスト) |
| [AivisSpeech](https://github.com/Aivis-Project/AivisSpeech-Engine) | 読み上げ(動画の声の素材づくり) |

**部品だけではできないこと: VRAM と CPU メモリの管理。** これがこのアプリの役目。

- GPU には **常に 1 つの部品だけ** を載せる。使う部品に自動で載せ替える。
- 画面の上に **VRAM・CPU メモリの計器** がある。
- Windows で VRAM があふれて「共有 GPU メモリ」に逃げ始めたら(極端に遅くなる)、**警告を出す**。
- プロンプト強化と作詞に使う **同梱 LLM** は、空き VRAM に入る分だけ載せ、GPU で生成する前に止める。

## 動作環境

| 項目 | 内容 |
|---|---|
| GPU | NVIDIA。VRAM の下限は設けない。6GB でも画角を絞れば動くが、時間がかかる。**推奨は 12GB 以上** |
| CPU メモリ | **32GB 以上を前提**。これより少ないと、動画の準備(量子化)と音楽の同梱 LLM が厳しくなる |
| ストレージ | **NVMe を推奨**。空きは約 150GB(部品とモデルの合計。動画の参照モード用キャッシュ 32GB を含む。`--no-ref` なら約 120GB) |
| OS | Windows 10 / 11、または Linux(x64) |

## インストール

初回は **モデルのダウンロードで数時間**(約 150GB)かかる。回線が速くても 1 時間以上は見ておく。
途中で止めても、もう一度 setup を実行すれば続きから進む。

### Windows

PowerShell を開く(スタートメニューで「PowerShell」と入力)。次を順に実行する。

```powershell
winget install --id Git.Git -e --source winget
winget install --id Python.Python.3.12 -e --source winget
winget install --id Gyan.FFmpeg -e --source winget
```

入れ終えたら **PowerShell を閉じて、開き直す**(入れたものを認識させるため)。続けて、

```powershell
git clone https://github.com/animede/h3-creator-studio.git
cd h3-creator-studio
.\setup.bat
```

`setup.bat` は uv(Python の道具)を入れ、部品を `components\` に取り込み、環境とモデルを用意する。
uv を入れた直後に「PowerShell を開き直してもう一度実行」と出たら、その通りにする(ここまでで入れたのは uv だけ)。

### Linux

```bash
sudo apt install git ffmpeg curl      # Debian / Ubuntu の例
git clone https://github.com/animede/h3-creator-studio.git
cd h3-creator-studio
./setup.sh
```

### setup の主なオプション

| オプション | 意味 |
|---|---|
| `--no-models` | 環境だけ作る。モデルは後で(もう一度 setup を実行すると取得する) |
| `--only h3,ace` | 指定した部品だけ(`h3` `h3image` `ace` `aivis`) |
| `--llm` | 同梱 LLM(約 5GB)も先に取得する。付けなくても、最初に使うときに取得する |
| `--ace-xl` | 音楽に XL モデルも入れる(約 45GB。既定は 2B のみで約 15GB) |
| `--no-ref` | 動画の参照モード(参照から・Vocal Lock)用のキャッシュ(約 32GB)を作らない。参照モードを使わない人向け |
| `--components-dir <場所>` | 部品の取り込み先を変える |
| `--clone-anyway` | 下の「既に入れている人」のフォルダがあっても、取り込み直す |

目安: MiniMax-H3 のモデル 約 60GB(いちばん時間がかかる)、MiniMax-H3-Image 約 25GB、ACE-Step 約 15GB、AivisSpeech 約 0.3GB。

### 既に minimax-h3 などを入れている人

その場所を使える。コピーも再ダウンロードもしない。`studio.local.toml.example` を `studio.local.toml` という名前でコピーして、
使いたい部品の `dir` にフォルダを書く。

Windows の例(パスは `'...'` で囲むと、`\` をそのまま書ける):

```toml
[components.h3]
dir = 'C:\AI\minimax-h3'
[components.h3image]
dir = 'C:\AI\minimax-h3-image-server'
```

Linux の例:

```toml
[components.h3]
dir = "/home/you/minimax-h3"
[components.ace]
dir = "/home/you/ACE-Step-1.5-fast"
```

- `dir` を書いた部品は、setup が取り込みもモデル取得も飛ばす。そのフォルダに環境とモデルが済んでいることが前提。
- 書かなくても、setup はホームに `~/minimax-h3` などを見つけると、「これを使うなら dir を書いてください」と知らせて、その部品を飛ばす(勝手に取り込み直さない)。

## 起動と使い方

```powershell
.\run.bat        # Linux は ./run.sh
```

ブラウザで **http://127.0.0.1:8700** を開く。止めるときは、起動した画面で `Ctrl+C`(部品もいっしょに止まる)。

**初回は、部品の起動にモデルの読み込みで数分かかる。** 画面の「部品」の行に進み具合が出る。待っていれば自動で使える状態になる。

### 画面

- **上**: メモリ計(VRAM・CPU メモリ。Windows では共有 GPU メモリも)と「部品」の一覧。今どの部品が GPU にいるか、起動中か、エラーかが分かる。
- **タブが 3 つ**: 画像 / 動画 / 音楽。
- **左: 操作**。**右上: 生成物**(できたものを大きく表示、保存ボタン)。**右下: 履歴**(そのタブで作ったもの)。
- 履歴の「ギャラリー」ボタンで、右側が **ギャラリー**(すべての生成物。全て・画像・動画・音楽で絞り込み)に切り替わる。「戻る」で履歴に戻る。
- **ドラッグ&ドロップ**: 入力欄には、PC のファイルも、ギャラリーや履歴の生成物も、**同じ操作で落とせる**。作ったものを次の入力に使える。

### 画像タブ(MiniMax-H3-Image)

上の 2 つのボタンで作り方を選ぶ(名前は MiniMax-H3-Image と同じ)。

- **T2I(テキストから)**: プロンプトとサイズを決めて「生成」。
- **edit(参照画像で編集)**: 参照画像(最大 16 枚)を落とし、プロンプトで `<Picture 1>` のように番号で指して編集する。

「プロンプト強化」を押すと、短い指示を詳しいプロンプトに書き直してくれる(同梱 LLM)。

### 動画タブ(MiniMax-H3)

上の 4 つのボタンで作り方を選ぶ(名前は MiniMax-H3 と同じ略語)。

- **T2VA(テキストから)**: 文章から動画と音声を作る。
- **FL2VA(最初・最後のフレームから)**: 最初の画像(と、あれば最後の画像)を指定して、その間を動かす。どちらか一方だけでもよい。
- **Ref2VA(参照から)**: 画像・動画・音声を参照にして作る。
- **T2I / Ref2I(静止画)**: 動画ではなく、静止画 1 枚を作る。参照があれば Ref2I、無ければ T2I。

解像度のネイティブは短辺 768。6GB のカードは 512²〜768×512 が現実的。長さは 5〜15 秒。

「音声を作る(読み上げ)」で、AivisSpeech が文章を読み上げた音声を作れる。それを**参照音声**として「参照から」に落とせば、声を指定した動画になる。
**Vocal Lock** にチェックすると、参照の音声をそのまま出力の音声にして、口の動きを合わせる。

### 音楽タブ(ACE-Step)

先頭の **作り方** で 2 つから選ぶ。

- **簡単(ジャンルから)**: ジャンルを選ぶだけで作れる。テーマ・歌詞・キャプションは、**空のものだけ**生成のときに AI が作る(自分で書いたものは作り直さない)。
  欄の横の 3 つのボタンで、生成の前に 1 つずつ作って直せる: **✨ テーマを作る** / **✍️ 作詞する**(テーマが空ならテーマも作る)/ **🎵 キャプションを作る**。
  「キャプション自動」(既定でオン)は、キャプションが空のとき生成時に AI が作る設定。ムード・ボーカル(もう一度押すと外れる)、歌の言語、長さ、**楽器のみ**(歌なし)も選べる。
  **✍️ 歌詞を新しくして生成**(歌詞があるときだけ出る)は、歌詞を消して、テーマはそのままで歌詞とキャプションを作り直す。
- **✨ おまかせ(一言から)**: 「雨の夜に聴く切ないバラード」のような一言だけで作る。管理役の LLM は使わず、ACE-Step の LM が歌詞・キャプション・BPM・調まで決める。

**詳細**(閉じてある)に、モデル(ACE の一覧から。未読込のモデルは生成の前に自動で読み込む。VRAM に載らないものは選べない)、STEP(Turbo 8、それ以外 50)、
Thinking、シード(ランダム / 指定 / **同じ骨格で作り直す** = 前回のシード)、ネガティブプロンプト、Shift、Sampler、曲数(1 / 2 / 4。2 曲以上は別々の生成物になる)がある。
作ったプラン(テーマ・キャプション・歌詞の先頭)は、生成中に下に表示される。曲名はテーマ(おまかせは一言)で、生成後に LLM は呼ばない。

## VRAM と CPU メモリの管理(このアプリの特徴)

GPU のメモリは足りない。4 つの部品を同時に載せると、すぐあふれる。そこで:

1. **GPU には常に 1 つだけ。** 画像を作るなら MiniMax-H3-Image、動画なら MiniMax-H3、音楽なら ACE-Step。
2. **載せ替えは自動。** タブで「生成」を押すと、管理役が次の順で進める。
   1. 今 GPU にいる部品を空ける。**画像・動画の部品は退避**(モデルを GPU から降ろす。プロセスは残る)。**音楽は停止**(ACE-Step には降ろす方法が無いので、プロセスを止める)。生成中の部品があると切り替えられない(「生成中の部品があるので…」と出る)。終わってからもう一度押す。
   2. VRAM が空いたのを確かめる。
   3. 次の部品を起動、または再ロードして、準備ができるのを待つ(生成物の画面に手順が出る)。
3. **同梱 LLM(Gemma 4 E4B)** は、プロンプト強化と作詞のために管理役が 1 つだけ持つ。最初に使うときに起こし、**いま GPU にいる部品の隣の空き VRAM に入る分だけ GPU に載せる**(入らない分は CPU)。**GPU で生成する直前に止めて** VRAM を返し、生成中は起こさない。各部品の内蔵 LLM は使わない。
4. **VRAM が 10GB 未満(8GB のカードなど)のときは、音楽に自動で省メモリの設定を入れる。** 次の 5 つ。
   - `ACESTEP_GPU_PROFILE=12gb`(2B モデルのプロファイル)
   - `ACESTEP_LM_OFFLOAD_TO_CPU=1`(言語モデルを CPU に置く)
   - `ACESTEP_RESIDENT_COMPONENTS=`(常駐させる部品を無くす)
   - `ACESTEP_VAE_DECODE_CHUNK_SIZE=128`(最後のデコードを小さく区切る)
   - `ACESTEP_QUANTIZATION=int8_weight_only`(int8 に量子化)

   自分で変えたいときは、`studio.local.toml` の `[components.ace.env]` に書く。
5. **Windows の「共有 GPU メモリ」への退避に警告。** Windows は VRAM があふれると、CPU メモリを GPU メモリの代わりに使い始める。止まりはしないが、極端に遅くなる。画面の上に **「VRAM があふれて共有 GPU メモリに退避しています…」** と出たら、画角を小さくするか、他の GPU を使うアプリを閉じる。

### メモリ計の見方

- **VRAM**: GPU のメモリの使用量(nvidia-smi を 2 秒おきに読む)。
- **共有 GPU メモリ**: Windows だけ。ここが増えたら、VRAM があふれている合図。
- **CPU メモリ**: 部品のプロセスが使っている分も含めた、PC 全体の使用量。32GB に近づくと、量子化や LLM が遅くなる。

## 部品の更新(git pull)

各部品は、`components\` の中の **普通の git のクローン**。更新は、その部品のフォルダで `git pull` して、**その部品のセットアップをもう一度**実行する(済んでいる所は飛ばすので、短く終わる)。

**更新の前に、管理役を止める**(`run.bat` の画面で `Ctrl+C`)。`studio.local.toml` の `dir` で別の場所を指している部品は、その場所のフォルダで行う。

| 部品 | フォルダ | 更新 |
|---|---|---|
| MiniMax-H3 | `components\minimax-h3` | `git pull` → `.\scripts\install.bat`(Linux: `bash scripts/install.sh`)→ モデルの更新があるときだけ `.\venv\Scripts\python.exe scripts\prepare_lowvram.py`(Linux: `venv/bin/python scripts/prepare_lowvram.py`) |
| MiniMax-H3-Image | `components\minimax-h3-image-server` | `git pull` → `uv pip install --python .venv\Scripts\python.exe -r requirements-windows.txt`(Linux: `uv pip install --python .venv/bin/python -r requirements.txt`)→ モデルの更新があるときだけ `.venv\Scripts\python.exe scripts\prepare_models.py`(Linux: `.venv/bin/python scripts/prepare_models.py`) |
| ACE-Step | `components\ACE-Step-1.5-fast` | `git pull` → `.\setup.bat --12gb`(Linux: `./setup.sh --12gb --no-client`)。XL モデルも入れているなら `--12gb` を外す |
| AivisSpeech | `components\AivisSpeech-Engine` | git ではない(配布版)。フォルダを消して、Studio の `setup` をもう一度実行すると、取得し直す |

たとえば Windows で MiniMax-H3 を更新するなら:

```powershell
cd components\minimax-h3
git pull
.\scripts\install.bat
cd ..\..
```

- モデルの更新があるかは、その部品の README / 更新履歴に書いてある。迷ったら、準備のコマンドを実行してよい(済んでいるものは数秒で飛ばす)。
- `git pull` が「手元の変更があるので…」と止まったら、そのフォルダを自分で触った跡。`git status` で確かめてから、`git stash` するか、変更を戻す。

**Studio 自身の更新**:

```powershell
git pull
.\setup.bat --no-models
```

`--no-models` は管理役の環境(`.venv`)の更新と、足りない部品の取り込みだけをする。部品のモデルは触らない。

**部品を入れ直したいとき**は、そのフォルダを消して、`setup` を実行し直す(消したフォルダだけ、取り込みから用意する)。`studio.local.toml` の `dir` で指しているフォルダは、setup は消さない。自分で消す。

## 設定

`studio.toml` が既定の設定(リポジトリに含まれる)。自分の PC の設定は **`studio.local.toml`** に書く(git の管理外。書いた項目だけが上書きされる。見本は `studio.local.toml.example`)。

| 項目 | 意味 |
|---|---|
| `host` / `port` | 管理役の待ち受け。既定は `127.0.0.1` / `8700`。他の PC に公開はしない(第 1 版) |
| `components_dir` | 部品を取り込む場所。既定は `components` |
| `[components.<名前>]` の `dir` | 部品の場所を直接指す。書くと、取り込みもモデル取得も飛ばす |
| 同 `enabled` | `false` でその部品を使わない(例: 音楽は要らない) |
| 同 `port` | 部品のポート(競合したとき変える) |
| 同 `env` | 部品に渡す環境変数。`[components.h3.env]` のように書く |
| `[llm]` の `gpu` | 同梱 LLM を載せる先。`auto`(空きに入る分だけ GPU)/ `off`(CPU のみ)/ `all` / 層数 |
| `[llm]` の `gguf` / `llama_server` | 書くと、取得せずに、そのファイルを使う |

部品の名前は `h3` / `h3image` / `ace` / `aivis`。

## トラブルシューティング

- **部品が「エラー」と出る**: その行の「ログ」を開く。同じ内容は `logs\<名前>.log`(`logs/h3.log` など)にある。モデルが無い、環境が無い、のときは、setup をもう一度実行する。
- **ポートが使われている**: 8700(管理役)、8611(動画)、8633(画像)、8001(音楽)、10101(読み上げ)のどれかを別のアプリが使っている。相手を止めるか、`studio.local.toml` でポートを変える。
- **「VRAM があふれて…」と警告が出る**: 画角(サイズ・解像度)を下げる。他の GPU アプリ(ブラウザの動画再生、ゲーム、別の生成ツール)を閉じる。
- **「中止」を押してもすぐ止まらない**: 中止は **いまのステップの後で止まる**。音楽は、言語モデル(LM)の段と、最後のデコードの最中は止まらないことがある。
- **外部で動いている部品は止めない**: 管理役が起動したものだけを止める。別に起動していた部品がすでにポートで待ち受けていれば、それをそのまま使い、停止はしない(画面にも「外部」と出る)。画像・動画の部品は外部でも GPU から降ろせるが、ACE-Step は降ろす方法が無く、止めるしかない。外部の ACE-Step が GPU にいて載せ替えが必要なときは、GPU に次の部品の分(目安: 動画 12GB、画像 13.5GB、音楽 14GB + 1GB)の空きがあれば止めずに同居し、無ければ「同居できません」と断る。その場合は先に自分で止めるか、`studio.local.toml` の `[components.ace]` に別の `port` を書いて管理役に起こさせる。
- **CPU メモリが足りない**: 動画の準備(量子化)中に落ちるときは、他のアプリを閉じる。32GB 未満では、動画と同梱 LLM が厳しい。
- **setup が途中で失敗した**: 失敗した手順と、それだけをやり直すコマンドが画面に出る。原因を直して、setup をもう一度実行すれば、続きから進む。

## 構成

```
h3-creator-studio/
├── setup.bat / setup.sh   セットアップ(uv を入れ、scripts/setup.py を呼ぶ)
├── run.bat / run.sh       管理役の起動
├── scripts/setup.py       部品の取り込み、環境とモデルの準備(部品自身のスクリプトを呼ぶ)
├── studio/                管理役(FastAPI): 部品の起動と停止、載せ替え、メモリ監視、ジョブ、LLM
├── web/                   画面(素の HTML / JavaScript / CSS。ビルド不要)
├── studio.toml            既定の設定
├── studio.local.toml      自分の設定(git の管理外。見本は studio.local.toml.example)
├── components/            取り込んだ部品(それぞれ独立した git リポジトリ。git の管理外)
├── outputs/               生成物と台帳(studio.db)(git の管理外)
├── logs/                  部品のログ(git の管理外)
├── tests/                 テスト(`python -m unittest discover -s tests -t .`)
└── docs/SPEC.md           仕様
```

## ライセンス

MIT([LICENSE](LICENSE))。部品とモデルは、それぞれのライセンスに従う。

## 謝辞

- [MiniMax-H3 / MiniMax-H3-Image](https://huggingface.co/MiniMaxAI)(MiniMax)のモデルと、diffusers への対応。
- [minimax-h3(Diffusers_minimax-h3)](https://github.com/animede/Diffusers_minimax-h3)、[minimax-h3-image-server](https://github.com/animede/minimax-h3-image-server)、[ACE-Step-1.5-fast](https://github.com/animede/ACE-Step-1.5-fast)(元の ACE-Step の公開モデルを使う)。
- [AivisSpeech-Engine](https://github.com/Aivis-Project/AivisSpeech-Engine)(Aivis Project)。
- [llama.cpp](https://github.com/ggml-org/llama.cpp) と、Google の Gemma 4(同梱 LLM)。
