# AI エージェントから使う(MCP サーバ、試作)

**日本語** | [English](README.en.md)

Claude などの AI エージェントから、言葉の指示で YUZU Creator Studio を操作するための部品([MCP](https://modelcontextprotocol.io/) サーバ)と、エージェント用の手順書(スキル)。**スタジオを画面で使う人には要らない**(setup も入れない。このフォルダが無くてもスタジオは動く)。

エージェントは動いているスタジオの API を呼ぶだけで、画面で直す場所(人物の説明、シーンの演出、やり直し、結合し直し、編集タブ)はそのまま残る。下書き(曲・人物の画像・MV の制作)と繰り返しはエージェントに任せ、気になるところは画面で、またはエージェントへの指示で直す、という使い方を想定している。

## 用意(一度だけ)

**いちばん簡単な方法**: スタジオのフォルダで次を実行すると、この PC のパスを入れた手順(MCP の部品を入れるコマンド、Claude Code に登録する 1 行、Claude Desktop の設定の例)が表示される。表示されたとおりに進めればよい。

Linux:

```bash
.venv/bin/python agent/mcp_server.py --setup
```

Windows(PowerShell):

```powershell
.venv\Scripts\python.exe agent\mcp_server.py --setup
```

手で進めるときは、次のとおり。

1. スタジオを入れる(本体の [README](../README.md) のインストール)。
2. MCP の部品(`mcp`)を、スタジオの環境に入れる。スタジオのフォルダで:

   Linux:

   ```bash
   uv pip install --python .venv/bin/python -r agent/requirements.txt
   ```

   Windows(PowerShell):

   ```powershell
   uv pip install --python .venv\Scripts\python.exe -r agent\requirements.txt
   ```

3. エージェントに登録する(パスは自分の置き場所に変える)。

   Claude Code(Linux):

   ```bash
   claude mcp add -s user yuzu-studio -- ~/h3-creator-studio/.venv/bin/python ~/h3-creator-studio/agent/mcp_server.py
   ```

   Claude Code(Windows、PowerShell):

   ```powershell
   claude mcp add -s user yuzu-studio -- C:\h3-creator-studio\.venv\Scripts\python.exe C:\h3-creator-studio\agent\mcp_server.py
   ```

   Claude Desktop など、設定ファイルに書く利用側(`claude_desktop_config.json` の例。Windows のパスは `\\` で書く):

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

4. 登録できたかは `claude mcp list` で確かめる(`yuzu-studio … Connected`)。登録した後に始めた会話から使える。外すときは `claude mcp remove yuzu-studio -s user`。
5. 使うときは、**先にスタジオを起動しておく**(MCP サーバはスタジオを起動しない)。Claude と普通に話せばよい(「ギャラリーの最新の画像を見せて」「この曲とこの画像で MV を作って」)。道具を使う前に、許可を求める確認が出る。

`mcp` を入れずに登録すると、エージェントの MCP の画面に、入れ方のコマンドが出て止まる。

## 道具

| 道具 | すること |
|---|---|
| `studio_status` | 版・GPU とメモリ・エンジンの状態・動いている / 待っているジョブ |
| `list_media` / `upload_media` | ギャラリーの一覧(id と、この PC のファイルの場所)/ PC のファイルをギャラリーに足す |
| `enhance_prompt` | 短い思いつきを、エンジンの公式ルールでプロンプトに書き直す(画像: MiniMax-H3-Image の公式ルール、動画: 公式の h3-prompt-writing。画面の「強化」と同じ) |
| `generate_image` | 画像(MiniMax-H3-Image。参照画像の id があれば edit)。できた画像そのものも返し、エージェントが見て確かめられる |
| `generate_video` | 5〜15 秒の動画 1 本(MiniMax-H3。文章から / 最初と最後のコマから / 参照から)。4 コマの画像も返す |
| `generate_music` | 曲(ACE-Step。テーマだけでも、歌詞とキャプションは LLM が書く)。使った歌詞・キャプション・BPM・調を返す |
| `listen_song` | 同梱 LLM が曲を聞いて、言語・ジャンル・雰囲気・楽器・ボーカル・テンポを答える(エージェントは曲を聞けないので、確かめる用。歌詞は聞き取らない) |
| `get_job` | 画像・動画・曲のジョブの様子 |
| `create_music_video` / `create_short_video` | MV / ショート動画の制作を始める |
| `preview_scenes` | 制作の各シーンから 4 コマずつ切り出して並べた画像を返す(エージェントが結果を見て確かめる。ffmpeg を使う) |
| `list_projects` / `get_project` / `wait_for_project` | 制作の一覧 / 様子(`include_prompts` でシーンごとの演出)/ 終わるまで待つ |
| `retry_scene` | 1 シーンをやり直す(そのシーンの演出の文だけを直して、または seed を変えて。公式の形への組み立ては管理役がする)。全部そろうと自動で結合し直す |
| `control_project` | 一時停止・再開・中止・結合し直す |

指示の例: 「ギャラリーの最新の曲と、カフェの女性の画像で 16:9 の MV を作って。できたら 3 番目のシーンだけ、カメラをもっとゆっくりにしてやり直して」

## エージェント用の手順書(スキル)

[`skills/yuzu-creator-studio/SKILL.md`](skills/yuzu-creator-studio/SKILL.md) に、制作の流れ(素材 → 制作 → `preview_scenes` で全シーンを見る → 利用者に見せる → 演出を直してやり直し)と、このスタジオの実用規則(歌わないシーンの構図など)をまとめた。Claude Code なら、`skills/yuzu-creator-studio` のフォルダを `~/.claude/skills/` に写すと使える。

## プロンプトの書き方の公式の規則

モデルの作り手が公開している規則に従う(どれもこのリポジトリには写さない。MiniMax の 2 つはライセンスの表記が標準のものではないため)。

| モデル | 公式の手引き | 使う場所 |
|---|---|---|
| MiniMax-H3(動画) | [`h3-prompt-writing`](https://github.com/MiniMax-AI/MiniMax-H3/tree/main/skills/h3-prompt-writing) | MV・ショート動画の各シーン(管理役が必ずこの形で書く)、`enhance_prompt`(動画) |
| MiniMax-H3-Image | [`h3-image-prompt-writing`](https://github.com/MiniMax-AI/MiniMax-H3-Image/tree/main/skills/h3-image-prompt-writing) と [MiniMax-H3-Image-PE](https://huggingface.co/MiniMaxAI/MiniMax-H3-Image-PE) のルール | `enhance_prompt`(画像) |
| ACE-Step 1.5(音楽) | [Tutorial](https://github.com/ace-step/ACE-Step-1.5/blob/main/docs/en/Tutorial.md) と [musician's guide](https://github.com/ace-step/ACE-Step-1.5/blob/main/docs/en/ace_step_musicians_guide.md) | `generate_music`(歌詞とキャプションを同梱 LLM がこの規則で書く) |

MV・ショート動画のシーンは、管理役が公式の形(参照画像を使うシーンは Ref2VA の 6 項目、使わないシーンは 3 項目)で書くので、エージェントが渡すのはシーンの演出の文だけ。プロンプト全文を書き換えると、項目・`[Shot 1]`・参照の記号(`<Subject 1>` など)が崩れていないかを確かめ、崩れていれば断る。

## 注意

- 生成は時間がかかる(30 秒の MV で、PRO 6000 は約 6 分、8〜12GB の GPU は約 20 分)。エージェントは制作を始めて、あとで様子を見る。
- 環境変数: `YCS_URL`(スタジオの URL。既定は `studio.toml` / `studio.local.toml` の host・port)、`YCS_LANG=en`(スタジオの返す文言を英語に)。Claude Code なら `claude mcp add -s user yuzu-studio -e YCS_LANG=en -- …`。
- 同じ PC の中で使う前提(エージェントに返すファイルの場所は、この PC のパス。別の場所のスタジオを `YCS_URL` で指したときは、画像・動画をスタジオから取り寄せて見せる)。スタジオの画面と同じく、認証はない。
- 編集(切る・つなぐ・曲の差し替えなど)の道具は用意しない。コマンドを実行できるエージェントなら、自分の道具(ffmpeg など)で、`list_media` が返すファイルの場所に対して編集できる。結果は `upload_media` でギャラリーに戻せば、画面で見て、編集タブで続きを直せる(元のファイルは書き換えない)。コマンドを実行できないエージェントなら、編集は画面の編集タブで。
- 試作: プリセット・サンプルの道具はまだない。

## このフォルダ

```
agent/
├── README.md / README.en.md   この説明(日本語 / 英語)
├── mcp_server.py              起動口(エージェントの MCP の設定から、これを Python で起こす。--setup で登録の手引き)
├── server.py                  MCP サーバの本体(動いているスタジオの API を呼ぶ)
├── requirements.txt           MCP の部品(mcp)。使う人だけが入れる
└── skills/yuzu-creator-studio/SKILL.md   エージェント用の手順書
```
