"""サーバが画面に返す文言の英訳(日本語・英語対応の 3 段目。2026-11-25)。

方針:
- 文言はコードの中では日本語のまま(テストや SQLite の jobs 表、ログはこれまでと同じ)。英語にするのは応答を返すときだけ。
- 画面(web/i18n.js)は毎回 `Accept-Language: ja|en` を送る。main.py のミドルウェアがそれを読んで LANG に入れ、
  /api/ の JSON 応答のうち、文言の入る項目(detail・message・error など、MESSAGE_KEYS)だけを tr() で訳す。
  ジョブや制作の進み具合も、問い合わせた画面の言語で訳す(ジョブには言語を記録しない。2 つの画面が違う言語でも、それぞれの言語で見える)。
- EN のキーは日本語の原文。f-string で作る文は、差し込む値を `{}` にした型(例: "シーン {} / {}: {}")をキーにし、英文側は `{0}` `{1}` で
  受ける(順番を入れ替えてよい)。tr() は完全一致を先に探し、無ければ型に当てはめて、差し込まれた値も再帰的に訳す
  (「シーン 2 / 5: 生成中 50%」→ "Scene 2 / 5: Generating 50%")。
- 値を名前で持つ文は msg("シーン {i} / {n}", i=2, n=5) で作ってもよい。str としては日本語のまま(Msg は str の子)で、
  tr(m, "en") は英文の型に同じ値を入れる。
- 訳の無い文は日本語のまま返す(壊れるよりよい)。語は web/i18n.js と docs/USAGE.en.md に合わせる
  (生成物 = output、制作 = project、画角 = aspect、画質 = quality、演出 = direction、結合 = assemble、境目 = transition、
  テロップ = captions、管理役 = manager、利用枠 = VRAM allocation)。
"""
import contextvars
import functools
import re
import string

LANG = contextvars.ContextVar("studio_lang", default="ja")      # いまの要求の言語(ミドルウェアが入れる)

# 応答の JSON で、訳す項目の名前(値が文字か、文字の配列のもの)。それ以外(題・プロンプト・歌詞など、利用者のデータ)は訳さない
MESSAGE_KEYS = frozenset({"detail", "message", "error", "note", "hint", "warning", "wait_reason", "reason", "steps",
                          "character_note", "label"})

_JP = re.compile(r"[぀-ヿ一-鿿]")
_FMT = string.Formatter()


def parse_accept_language(header) -> str:
    """Accept-Language の先頭が en なら "en"、それ以外(空・ja・その他)は "ja"。"""
    first = (header or "").split(",")[0].strip().lower()
    return "en" if first.startswith("en") else "ja"


class Msg(str):
    """名前付きの値を持つ文。str としては日本語の文(これまでどおり比べられる)。tr() で英文に差し替わる。"""

    def __new__(cls, template: str, **kwargs):
        self = super().__new__(cls, template.format(**kwargs))
        self.template, self.kwargs = template, kwargs
        return self


def msg(template: str, **kwargs) -> Msg:
    return Msg(template, **kwargs)


def _fields(template: str) -> list:
    return [f for _, f, _, _ in _FMT.parse(template) if f is not None]


def _literal(template: str) -> str:
    return "".join(lit for lit, _, _, _ in _FMT.parse(template))


def _norm(template: str) -> str:
    """{name} や {0:.2f} を {} にそろえる(msg() の型と f-string 由来の型を同じキーで引く)。"""
    out = ""
    for lit, f, _, _ in _FMT.parse(template):
        out += lit.replace("{", "{{").replace("}", "}}")
        if f is not None:
            out += "{}"
    return out


EN = {
    # ---- ジョブ(jobs.py) ----
    "順番を待っています": "Waiting in line",
    "エンジンを準備しています": "Preparing the engine",
    "動画(MiniMax-H3)": "Video (MiniMax-H3)",
    "画像(MiniMax-H3-Image)": "Image (MiniMax-H3-Image)",
    "音楽(ACE-Step)": "Music (ACE-Step)",
    "{} が終了していました(RAM 不足の可能性があります)。ログの最後: {}": "{0} had exited (possibly out of RAM). Last log lines: {1}",
    "(ログがありません)": "(no log)",
    "エンジンが終了していたので、起動し直してやり直します": "The engine had exited; restarting it and trying again",
    "メモリを空けています(読み上げ・同梱 LLM を止めます)": "Freeing memory (stopping text-to-speech and the bundled LLM)",
    "同梱 LLM は使用中です。終わってからもう一度押してください": "The bundled LLM is in use. Press again when it is done",
    "同梱 LLM": "Bundled LLM",
    "未対応のジョブの種類です: {}": "Unsupported job kind: {0}",
    "{}のエンジンは無効になっているため使えません(studio.toml の [components.{}] の enabled)":
        "The {0} engine is disabled and cannot be used (enabled in [components.{1}] of studio.toml)",
    "中止しました": "Cancelled",
    "中止しています": "Cancelling",
    "生成を始めます": "Starting generation",
    "完了しました": "Completed",
    "失敗しました": "Failed",
    "中止しました(管理役を停止)": "Cancelled (the manager was stopped)",
    "失敗: {}": "Failed: {0}",

    # ---- エンジンの切り替え(scheduler.py・components.py・gpu.py) ----
    "MiniMax-H3(動画・静止画)": "MiniMax-H3 (video / stills)",
    "MiniMax-H3-Image(画像)": "MiniMax-H3-Image (images)",
    "ACE-Step(音楽)": "ACE-Step (music)",
    "AivisSpeech(音声合成)": "AivisSpeech (speech synthesis)",
    "未知のエンジン: {}": "Unknown engine: {0}",
    "フォルダ {} がありません。setup を実行するか、studio.local.toml の [components.{}] の dir に実際の場所を書いてください。":
        "Folder {0} does not exist. Run setup, or write the real location in dir of [components.{1}] in studio.local.toml.",
    "{} に仮想環境({})の python がありません。そのエンジンの setup を実行してください。":
        "{0} has no virtual-environment python ({1}). Run that engine's setup.",
    "{} に {} がありません。setup を実行するか、studio.local.toml で dir を指定してください。":
        "{1} is missing in {0}. Run setup, or set dir in studio.local.toml.",
    "{} に {} がありません(配布版が必要です)。setup を実行するか、studio.local.toml で配布版のフォルダを dir に指定してください。":
        "{1} is missing in {0} (the release build is required). Run setup, or set dir in studio.local.toml to the release folder.",
    "VRAM があふれて共有 GPU メモリに退避しています(極端に遅くなります)。画角を小さくするか、他の GPU 利用アプリを閉じてください":
        "VRAM is overflowing into shared GPU memory (this is extremely slow). Use a smaller aspect or close other GPU apps",
    "nvidia-smi が見つかりません": "nvidia-smi was not found",
    "準備完了": "Ready",
    "({} が生成中です)": " ({0} is generating)",
    "{} を載せる GPU の空きがありません{}。「準備」で切り替えてください":
        "Not enough free GPU memory to load {0}{1}. Switch with \"Prepare\"",
    "{} が GPU を使っています。「準備」で切り替えてください": "{0} is using the GPU. Switch with \"Prepare\"",
    "起動しました": "Started",
    "{} は外部で動いているため、ここからは停止しません": "{0} is running externally, so it is not stopped from here",
    "停止しました": "Stopped",
    "GPU の空きを待っています({} が生成中)": "Waiting for free GPU memory ({0} generating)",
    "(GPU の空き {}GiB では同居できません)": " (cannot share with only {0} GiB of GPU memory free)",
    "{} は外部で動いていて止められません{}。先に自分で停止するか、studio.local.toml の [components.{}] に別の port を書いて管理役に起こさせてください":
        "{0} is running externally and cannot be stopped{1}. Stop it yourself first, or write a different port in "
        "[components.{2}] of studio.local.toml and let the manager start it",
    "{} は外部で動いていますが、GPU に {}GiB の空きがあるので止めずに同居します":
        "{0} is running externally, but {1} GiB of GPU memory is free, so it stays and shares the GPU",
    "{} を GPU から降ろしています": "Unloading {0} from the GPU",
    "生成中のエンジンがあるので切り替えられません": "Cannot switch while an engine is generating",
    "{} の unload に失敗しました(HTTP {})": "Unload of {0} failed (HTTP {1})",
    "{} はもう動いていないので、止まったものとして扱います": "{0} is no longer running; treating it as stopped",
    "{} を停止しています": "Stopping {0}",
    "RAM が少ないので {} を止めます(次に使うときに起動し直します)": "This PC has little RAM, so {0} is stopped (it starts again when next needed)",
    "RAM が少ないので、GPU から降ろしてある {} を止めます": "This PC has little RAM, so {0}, already off the GPU, is stopped",
    "GPU メモリが空くのを待っています": "Waiting for GPU memory to free up",
    "GPU メモリが完全には空きませんでした(続行します)": "GPU memory did not fully free up (continuing)",
    "{} を GPU に戻しています(モデルの読み込みに数分かかります)": "Reloading {0} onto the GPU (loading the model takes a few minutes)",
    "reload に失敗(HTTP {})": "Reload failed (HTTP {0})",
    "{} の reload に失敗しました(HTTP {})": "Reload of {0} failed (HTTP {1})",
    "{} は既に動いているので、そのまま使います": "{0} is already running; using it as is",
    "VRAM の利用枠 {}GiB。": "VRAM allocation {0} GiB. ",
    "{} を起動しています({}モデルの読み込みに数分かかります)": "Starting {0} ({1}loading the model takes a few minutes)",
    "{} が途中で終了しました(終了コード {})": "{0} exited unexpectedly (exit code {1})",
    "{} の Python 環境が不完全です(モジュール {} がありません)。スタジオを止めてから setup.bat --only {}(Linux は ./setup.sh --only {})を実行してください":
        "The Python environment of {0} is incomplete (module {1} is missing). Stop the studio, then run setup.bat --only {2} (Linux: ./setup.sh --only {3})",
    "{} が {} 秒以内に準備できませんでした": "{0} did not become ready within {1} seconds",
    "画像のエンジン(MiniMax-H3-Image)のモデルの準備が終わっていません(テキストエンコーダがありません)。スタジオを止めてから setup.bat --only h3image(Linux は ./setup.sh --only h3image)を最後まで実行してください(テキストエンコーダの準備で約 67GB を取得します)":
        "The image engine (MiniMax-H3-Image) has not finished preparing its models (the text encoder is missing). Stop the studio, then run setup.bat --only h3image (Linux: ./setup.sh --only h3image) to the end (preparing the text encoder downloads about 67GB)",
    "別のエンジン": "another engine",

    # ---- 管理役の API(main.py) ----
    "未知のエンジンです: {}": "Unknown engine: {0}",
    "start / stop / ensure のいずれかです": "Must be one of start / stop / ensure",
    "その操作は見つかりません": "That operation was not found",
    "{} を GPU に載せられません({} が生成中のため)。生成が終わってからもう一度押してください":
        "Cannot load {0} onto the GPU ({1} is generating). Press again after generation finishes",
    "このジョブの種類は受け付けていません: {}(画像・動画・音楽のジョブだけを入れられます)":
        "This job kind is not accepted: {0} (only image, video and music jobs can be submitted)",
    "そのジョブは見つかりません": "That job was not found",
    "そのジョブは見つかりません(完了済みで一覧から消えた可能性があります)":
        "That job was not found (it may have finished and dropped off the list)",
    "caption は {} 文字以内にしてください": "caption must be at most {0} characters",
    "音楽のエンジン(ACE-Step)は無効になっています": "The music engine (ACE-Step) is disabled",
    "ffmpeg が見つかりません(PATH に入れてください)": "ffmpeg was not found (add it to PATH)",
    "その MV の制作は見つかりません(削除されたかもしれません)": "That MV project was not found (it may have been deleted)",
    "そのショート動画の制作は見つかりません(削除されたかもしれません)": "That short video project was not found (it may have been deleted)",
    "その編集プロジェクトは見つかりません(削除されたかもしれません)": "That edit project was not found (it may have been deleted)",
    "JSON として読めません": "Cannot read it as JSON",
    "使い方の説明(docs/USAGE.md)が見つかりません": "The usage guide (docs/USAGE.md) was not found",
    "kind は all / image / video / audio のいずれかです": "kind must be one of all / image / video / audio",
    "実行中・順番待ちのジョブで使っています": "In use by a running or queued job",
    "編集プロジェクトで使っています。先にクリップ・曲から外すか、プロジェクトを削除してください":
        "In use by an edit project. Remove it from the clips / song first, or delete the project",
    "{}制作{}で使っています。先に制作を削除してください": "In use by the {0} project {1}. Delete the project first",
    "その生成物は見つかりません": "That output was not found",
    "画像・動画・音声のファイルだけ取り込めます: {}": "Only image, video and audio files can be imported: {0}",
    "{} を読み込めません: {}": "Cannot read {0}: {1}",

    # ---- 動画・静止画(handlers/h3.py・common.py) ----
    "生成を始めています": "Starting generation",
    "テキストエンコーダを読み込んでいます": "Loading the text encoder",
    "プロンプトと参照をエンコードしています": "Encoding the prompt and references",
    "モデルを読み込んでいます": "Loading the model",
    "生成中": "Generating",
    "動画に書き出しています": "Writing the video",
    "仕上げています": "Finishing",
    "{} を指定してください": "Please specify {0}",
    "{} は整数で指定してください": "{0} must be an integer",
    "解像度は 768x768 のような形で指定してください": "Specify the resolution like 768x768",
    "幅": "width",
    "高さ": "height",
    "{}の画像が見つかりません(削除されたかもしれません)": "The {0} image was not found (it may have been deleted)",
    "プロンプトは 1〜8000 文字で入力してください": "The prompt must be 1–8000 characters",
    "秒数は数字で指定してください": "Seconds must be a number",
    "秒数は {}〜{} で指定してください(0 なら音声の長さに合わせます)": "Seconds must be {0}–{1} (0 matches the audio length)",
    "秒数は {}〜{} で指定してください": "Seconds must be {0}–{1}",
    "先頭フレームか最後のフレームのどちらかは画像を入れてください": "Put an image in either the first frame or the last frame",
    "参照を 1 つ以上入れてください(画像・動画・音声)": "Add at least one reference (image, video or audio)",
    "参照が見つかりません(削除されたかもしれません): {}": "Reference not found (it may have been deleted): {0}",
    "画像": "images",
    "動画": "videos",
    "音声": "audio",
    "参照の{}は最大 {} 個です({} 個入っています)": "At most {1} reference {0} are allowed ({2} added)",
    "音声の長さに合わせられるのは、音声の参照がちょうど 1 つのときだけです":
        "Matching the audio length works only with exactly one audio reference",
    "{}が見つかりません(削除されたかもしれません): {}": "{0} not found (it may have been deleted): {1}",
    "先頭フレーム": "First frame",
    "最後のフレーム": "Last frame",
    "参照": "Reference",
    "H3 の応答が 1 時間以内に返りませんでした": "H3 did not respond within 1 hour",
    "H3 と通信できません: {}: {}": "Cannot reach H3: {0}: {1}",
    "H3 は別の生成を実行中です。終わってからもう一度試してください": "H3 is running another generation. Try again after it finishes",
    "H3 が生成に失敗しました: {}": "H3 failed to generate: {0}",
    "H3 に送っています": "Sending to H3",
    "生成物を取り込んでいます": "Importing the output",
    "H3 が静止画を返しませんでした": "H3 returned no still image",
    "H3 が動画を返しませんでした": "H3 returned no video",
    "生成物の取得に失敗しました: HTTP {}": "Failed to fetch the output: HTTP {0}",

    # ---- 画像(handlers/h3image.py) ----
    "t2i では参照画像を使えません(edit を使ってください)": "t2i cannot use reference images (use edit)",
    "参照画像は 1〜{} 枚にしてください": "Use 1–{0} reference images",
    "seed は整数で指定してください": "seed must be an integer",
    "steps は {} のいずれかか、未指定にしてください": "steps must be one of {0}, or unset",
    "参照画像のアップロードに失敗しました: {}": "Failed to upload the reference image: {0}",
    "参照画像が見つかりません(削除されたかもしれません): {}": "Reference image not found (it may have been deleted): {0}",
    "参照画像が見つかりません: {}": "Reference image not found: {0}",
    "参照画像 {}/{} を送っています": "Sending reference image {0}/{1}",
    "H3-Image がジョブを受け付けませんでした: {}": "H3-Image did not accept the job: {0}",
    "画像を取り込んでいます": "Importing the image",
    "H3-Image と通信できません: {}": "Cannot reach H3-Image: {0}",
    "H3-Image が失敗を報告しました": "H3-Image reported a failure",
    "H3-Image の順番を待っています": "Waiting in H3-Image's queue",
    "生成中 {}%": "Generating {0}%",
    "生成中 {}/{}": "Generating {0}/{1}",          # H3 の denoising の段(handlers/h3.py の label + step/total)
    "生成が 30 分以内に終わりませんでした": "Generation did not finish within 30 minutes",
    "生成が 4 時間以内に終わりませんでした": "Generation did not finish within 4 hours",
    "本体のキャッシュを作っています(初回だけ。62GB のダウンロードで 1 時間以上かかります)":
        "Building the main-model cache (first time only; a 62GB download that takes over an hour)",

    # ---- 音楽(handlers/ace.py・songplan.py・genres.py) ----
    "{} は {} 文字以内にしてください": "{0} must be at most {1} characters",
    "{}は整数で指定してください": "{0} must be an integer",
    "{}は {}〜{} にしてください": "{0} must be {1}–{2}",
    "shift は 1〜5 の数で指定してください": "shift must be a number from 1 to 5",
    "infer_method は {} のいずれかにしてください": "infer_method must be one of {0}",
    "曲数": "Number of songs",
    "シード": "Seed",
    "言語は {} のいずれかにしてください": "Language must be one of {0}",
    "duration は秒数(整数)か -1(自動)で指定してください": "duration must be seconds (integer) or -1 (auto)",
    "曲の長さは {}〜{} 秒か、自動(-1)にしてください": "Song length must be {0}–{1} seconds, or auto (-1)",
    "model の名前が不正です": "Invalid model name",
    "どんな曲か、一言入力してください": "Describe the song in a few words",
    "LLM が設定されていません": "No LLM is configured",
    "歌詞欄の短い文はテーマとして使い、歌詞は AI が作ります": "The short text in the lyrics field is used as the theme; AI writes the lyrics",
    "テーマ・歌詞・キャプションを作っています": "Writing the theme, lyrics and caption",
    "歌詞を作れませんでした: {}": "Could not write the lyrics: {0}",
    "キャプションを作っています": "Writing the caption",
    "ACE が起動していません": "ACE is not running",
    "{} は ACE-Step の checkpoints にありません。setup で取得してください": "{0} is not in ACE-Step's checkpoints. Download it with setup",
    "{} はこの GPU には載りません(約 {}GB 必要)。2B 系のモデルを選んでください":
        "{0} does not fit on this GPU (about {1} GB needed). Choose a 2B model",
    "この ACE-Step はスロットが 1 本だけなので、起動後のモデル切り替えはできません。起動時の設定(ACESTEP_CONFIG_PATH)で指定してください":
        "This ACE-Step has only one slot, so the model cannot be switched after start. Set it at startup (ACESTEP_CONFIG_PATH)",
    "モデル {} を読み込んでいます(数十秒かかります)": "Loading model {0} (takes tens of seconds)",
    "モデル {} を読み込めませんでした: {}: {}": "Could not load model {0}: {1}: {2}",
    "モデル {} を読み込めませんでした: {}": "Could not load model {0}: {1}",
    "ACE-Step の LM で BPM と調を決めています(初回はモデルの読み込みで時間がかかります)":
        "ACE-Step's LM is choosing the BPM and key (the first time takes a while to load the model)",
    "ACE-Step と通信できません: {}": "Cannot reach ACE-Step: {0}",
    "ACE-Step が音声を返しませんでした": "ACE-Step returned no audio",
    "原因不明": "unknown cause",
    "ACE-Step が失敗を報告しました: {}": "ACE-Step reported a failure: {0}",
    "ACE-Step で生成しています": "Generating with ACE-Step",
    "ACE-Step に生成を頼んでいます": "Requesting generation from ACE-Step",
    "ACE-Step に生成を頼めませんでした: {}: {}": "Could not request generation from ACE-Step: {0}: {1}",
    "ACE-Step が生成を受け付けませんでした: {}": "ACE-Step did not accept the generation: {0}",
    "音楽を取り込んでいます": "Importing the music",
    "LLM が曲のプラン(テーマと歌詞)をうまく作れませんでした。もう一度試すか、歌詞を自分で入力してください":
        "The LLM could not produce a song plan (theme and lyrics). Try again, or enter the lyrics yourself",
    "LLM がテーマを作れませんでした。もう一度試してください": "The LLM could not produce a theme. Please try again",
    "LLM がキャプションを作れませんでした。もう一度試してください": "The LLM could not produce a caption. Please try again",
    "同梱のジャンルは変更できません": "Built-in genres cannot be changed",
    "そのジャンルは見つかりません(削除されたかもしれません)": "That genre was not found (it may have been deleted)",
    "genre は {} 文字以内にしてください": "genre must be at most {0} characters",
    "名前は 1〜{} 文字で入力してください": "The name must be 1–{0} characters",
    "名前に使えない文字があります": "The name contains characters that cannot be used",
    "説明は {} 文字以内にしてください": "The description must be at most {0} characters",
    "世界観(vibe)は 1〜{} 文字で入力してください": "The vibe must be 1–{0} characters",
    "{}は文字の配列にしてください": "{0} must be an array of strings",
    "{}は 1〜{} 個にしてください": "{0} must have 1–{1} items",
    "{}は 1 つ {} 文字以内にしてください": "Each {0} item must be at most {1} characters",
    "viz は {} のいずれかにしてください": "viz must be one of {0}",
    "「{}」という名前のジャンルがすでにあります": "A genre named \"{0}\" already exists",
    "ジャンルの定義を作れませんでした(LLM が使えません): {}": "Could not create the genre definition (the LLM is unavailable): {0}",
    "LLM がジャンルの定義をうまく作れませんでした。もう一度試してください": "The LLM could not produce a genre definition. Please try again",
    "場面": "Scenes",
    "感情": "Emotions",
    "変更する項目を指定してください": "Specify the fields to change",

    # ---- プロンプトの強化(enhance.py) ----
    "{} に管理役の LLM が設定されていません。管理役から起動した {} でないと強化できません(外部で動いている場合は、いったん停止して管理役から起動し直してください)":
        "{0} has no manager LLM configured. Enhancement works only with a {1} started by the manager "
        "(if it is running externally, stop it and start it again from the manager)",
    "{} がプロンプトを強化できませんでした: {}": "{0} could not enhance the prompt: {1}",
    "mode は {}、task は {} のいずれかです": "mode must be one of {0}, and task one of {1}",
    "tab は image か video です": "tab must be image or video",
    "プロンプトを入力してください": "Enter a prompt",
    "{} が無効になっています(studio.toml の [components.{}] enabled を確認してください)":
        "{0} is disabled (check enabled in [components.{1}] of studio.toml)",
    "{} を準備できませんでした: {}": "Could not prepare {0}: {1}",
    "LLM を起動できませんでした: {}": "Could not start the LLM: {0}",
    "{} と通信できませんでした: {}: {}": "Could not communicate with {0}: {1}: {2}",

    # ---- LLM(llm.py) ----
    "約 4.4GB": "about 4.4 GB",
    "約 0.56GB": "about 0.56 GB",
    "この環境({}/{})用の llama-server がありません。studio.local.toml の [llm] llama_server に実行ファイルのパスを書いてください":
        "There is no llama-server for this environment ({0}/{1}). Write the executable's path in llama_server of [llm] in studio.local.toml",
    "展開しても {} がありません(アーカイブの構成が変わったかもしれません)": "{0} is missing after extraction (the archive layout may have changed)",
    "[llm] llama_server のファイルがありません: {}": "The [llm] llama_server file does not exist: {0}",
    "[llm] gguf のファイルがありません: {}": "The [llm] gguf file does not exist: {0}",
    "[llm] mmproj のファイルがありません: {}": "The [llm] mmproj file does not exist: {0}",
    "huggingface_hub がありません。`pip install huggingface_hub` するか、studio.local.toml の [llm] {} にファイルのパスを書いてください":
        "huggingface_hub is not installed. Run `pip install huggingface_hub`, or write the file path in {0} of [llm] in studio.local.toml",
    "LLM の用意に失敗しました: {}: {}(studio.local.toml の [llm] に llama_server と gguf のパスを書けば、取得は不要です)":
        "Failed to set up the LLM: {0}: {1} (writing the llama_server and gguf paths in [llm] of studio.local.toml makes the download unnecessary)",
    "llama-server が途中で終了しました(終了コード {})。{}": "llama-server exited unexpectedly (exit code {0}). {1}",
    "llama-server が {} 秒以内に準備できませんでした": "llama-server did not become ready within {0} seconds",
    "LLM と通信できません: {}: {}": "Cannot reach the LLM: {0}: {1}",
    "LLM がエラーを返しました(HTTP {}): {}": "The LLM returned an error (HTTP {0}): {1}",
    "LLM の応答を読めませんでした": "Could not read the LLM's response",
    "画像・音声を読む投影ファイル({}、{})を取得できませんでした: {}。ネットワークを確かめて `setup --llm` で取得するか、studio.local.toml の [llm] mmproj にファイルのパスを書いてください":
        "Could not download the projector file for images / audio ({0}, {1}): {2}. Check the network and fetch it with "
        "`setup --llm`, or write the file path in mmproj of [llm] in studio.local.toml",
    "この LLM は{}を読めません(投影ファイルを読み込めなかったので、画像・音声なしで起動しています)。logs/llm.log を確かめて、投影ファイルを取得し直す(`setup --llm`)か、[llm] mmproj を直してから、管理役を再起動してください":
        "This LLM cannot read {0} (the projector file failed to load, so it started without images / audio). Check logs/llm.log, "
        "re-download the projector file (`setup --llm`) or fix [llm] mmproj, then restart the manager",
    "この LLM は{}を読めません(投影ファイル mmproj が読み込まれていません)。他の仕事が終わってからもう一度試すか、llama-server を止めて(管理役の再起動でも)投影ファイルつきで起動し直してください":
        "This LLM cannot read {0} (the mmproj projector file is not loaded). Try again after other work finishes, or stop "
        "llama-server (restarting the manager also works) and start it again with the projector file",

    # ---- 音声合成(tts.py) ----
    "AivisSpeech の設定がありません(studio.toml の [components.aivis])": "AivisSpeech is not configured ([components.aivis] in studio.toml)",
    "AivisSpeech を起動できません: {}": "Cannot start AivisSpeech: {0}",
    "AivisSpeech から話者の一覧を取れません: {}": "Cannot get the voice list from AivisSpeech: {0}",
    "読み上げる文を入力してください": "Enter the text to read aloud",
    "読み上げる文は {} 文字までです(いま {} 文字)": "The text to read aloud is limited to {0} characters (currently {1})",
    "AivisSpeech が合成に失敗しました(HTTP {})。話者の指定を確かめてください": "AivisSpeech failed to synthesize (HTTP {0}). Check the voice setting",
    "AivisSpeech と通信できません: {}": "Cannot reach AivisSpeech: {0}",

    # ---- MV(mv.py・handlers/mv.py・mv_audio.py・mvdescribe.py・mvscript.py・mvtools) ----
    "曲の分析": "song analysis",
    "曲を聞く処理": "listening to the song",
    "シーン割り": "scene split",
    "演出の作成": "writing the direction",
    "シーンの生成": "scene generation",
    "結合": "assembly",
    "管理役の再起動で一時停止しました": "Paused because the manager restarted",
    "{} は数字で指定してください": "{0} must be a number",
    "{}を選んでください": "Choose {0}",
    "{}が見つかりません(ギャラリーから削除されたかもしれません)": "{0} was not found (it may have been deleted from the Gallery)",
    "{}には{}を使ってください": "Use {1} for {0}",
    "{}のファイルが見つかりません(削除されたかもしれません)": "The {0} file was not found (it may have been deleted)",
    "曲": "the song",
    "画角が手動入力のときは、{}を数字で指定してください(256〜{}、32 の倍数)":
        "With a custom aspect, {0} must be a number (256–{1}, a multiple of 32)",
    "{}は {}〜{} の範囲で指定してください(32 の倍数に丸めます)": "{0} must be in the range {1}–{2} (rounded to a multiple of 32)",
    "画角は {} のいずれかです": "Aspect must be one of {0}",
    "画質は {} のいずれかです": "Quality must be one of {0}",
    "画風は {} のいずれかです": "Visual style must be one of {0}",
    "アングルは {} のいずれかです": "Camera angle must be one of {0}",
    "シーンの最短": "Shortest scene",
    "シーンの最長": "Longest scene",
    "シーンの長さは {}〜{} 秒で指定してください": "Scene length must be {0}–{1} seconds",
    "シーンの最短は最長より長くできません": "The shortest scene cannot be longer than the longest",
    "background_text は auto / large / none のいずれかです": "background_text must be one of auto / large / none",
    "lipsync は true / false で指定してください": "lipsync must be true / false",
    "曲を分析します": "Analysing the song",
    "曲を聞いて、曲の内容を考えます": "Listening to the song to work out its content",
    "物語からシーンに分けます": "Splitting the story into scenes",
    "演出を書きます": "Writing the direction",
    "シーンを結合します": "Assembling the scenes",
    "MV ができました": "The MV is ready",
    "一時停止できるのは、制作の途中だけです": "Only a project in progress can be paused",
    "一時停止しました": "Paused",
    "一時停止します(いまの仕事が終わると止まります)": "Pausing (stops when the current task finishes)",
    "一時停止します(始まっていない仕事を取り下げています)": "Pausing (withdrawing tasks that have not started)",
    "再開できるのは、一時停止・失敗・中止した制作だけです": "Only paused, failed or cancelled projects can be resumed",
    "終わった制作は中止できません(削除はできます)": "A finished project cannot be cancelled (it can be deleted)",
    "まだ演出ができていないので、シーンをやり直せません": "The direction is not written yet, so scenes cannot be redone",
    "シーンの番号": "Scene number",
    "シーン {} はありません": "There is no scene {0}",
    "結合しています。終わってからやり直してください": "Assembling now. Redo after it finishes",
    "そのシーンは生成中です": "That scene is being generated",
    "プロンプトは 1〜{} 文字で入力してください": "The prompt must be 1–{0} characters",
    "プロンプト全文と演出は、どちらか一方だけ渡してください": "Pass either the whole prompt or the direction, not both",
    "演出は 1〜{} 文字で入力してください": "The direction must be 1–{0} characters",
    "全部のシーンができてから結合できます": "You can assemble once all scenes are done",
    "制作中です。終わってからテイクを採用してください": "Wait for the current job to finish before selecting a take.",
    "テイクの動画が見つかりません": "The saved take video was not found.",
    "制作中です。終わってから結合し直してください": "The project is in progress. Re-assemble after it finishes",
    "その{}の制作は見つかりません(削除されたかもしれません)": "That {0} project was not found (it may have been deleted)",
    "シーン": "Scene",
    "シーン {} / {}: {}": "Scene {0} / {1}: {2}",
    "シーン {} / {}": "Scene {0} / {1}",
    "再開しました": "Resumed",
    "シーン {} / {} を生成します": "Generating scene {0} / {1}",
    "ジョブを始められませんでした": "Could not start the job",
    "{}の結果を記録できませんでした": "Could not record the result of {0}",
    "ジョブは完了しましたが、結果が制作に書かれていません": "The job completed, but its result was not written to the project",
    "{}に失敗しました": "{0} failed",
    "{}が中止されたので一時停止しました": "Paused because {0} was cancelled",
    "MV の": "MV",
    "ショート動画の": "short video",
    "ショート動画": "Short video",
    "{}制作が見つかりません(削除されたかもしれません)": "The {0} project was not found (it may have been deleted)",
    "シーンの番号を整数で指定してください": "The scene number must be an integer",
    "ボーカルを分離しています {}%": "Separating the vocals {0}%",
    "音声を 44.1kHz にそろえています": "Converting the audio to 44.1 kHz",
    "分離済みのボーカルを使います": "Using the already separated vocals",
    "ボーカルを分離しています(CPU では曲の長さの 0.5〜3 倍ほどかかります)":
        "Separating the vocals (on CPU this takes about 0.5–3× the song length)",
    "曲をシーンに分けています": "Splitting the song into scenes",
    "曲をシーンに分けられませんでした": "Could not split the song into scenes",
    "曲を聞いています": "Listening to the song",
    "曲の内容を考えています": "Working out the song's content",
    "画像を見て、人物の見た目を調べています": "Looking at the image to work out the character's appearance",
    "演出を書いています {} / {} シーン": "Writing the direction {0} / {1} scenes",
    "演出を書いています 0 / {} シーン": "Writing the direction 0 / {0} scenes",
    "人物の見た目を整理しています": "Organising the character's appearance",
    "シーン {} がありません": "Scene {0} does not exist",
    "このシーンの演出がまだありません(演出の作成からやり直してください)":
        "This scene has no direction yet (redo from writing the direction)",
    "分離したボーカルが見つかりません(分析からやり直してください)": "The separated vocals were not found (redo from the analysis)",
    "{}: ボーカルを切り出しています": "{0}: Cutting out the vocals",
    "キャラクター画像": "Character image",
    "{}: 準備しています": "{0}: Preparing",
    "{}: H3 に送っています": "{0}: Sending to H3",
    "{}: 動画を取り込んでいます": "{0}: Importing the video",
    "{}: 尺をそろえています": "{0}: Trimming to length",
    "{} のクリップの整形": "shaping the clip of {0}",
    "まだ生成できていないシーンがあります": "Some scenes have not been generated yet",
    "シーンのクリップが見つかりません: {}(そのシーンをやり直してください)": "Scene clips not found: {0} (redo those scenes)",
    "曲のファイルが見つかりません": "The song file was not found",
    "シーンをつないでいます": "Joining the scenes",
    "シーンの結合": "assembling the scenes",
    "元の曲を載せています": "Adding the original song",
    "曲の差し替え": "replacing the audio with the song",
    "ギャラリーに登録しています": "Adding to the Gallery",
    "曲が見つかりません(ギャラリーから削除されたかもしれません)": "The song was not found (it may have been deleted from the Gallery)",
    "MV タブの「道具を入れる」ボタンか setup --only mvtools で入れてください(または studio.local.toml の [mv] tools_python に Demucs と librosa の入った Python を指定)":
        "Install them with the Music Video tab's Install tools button or setup --only mvtools (or set tools_python in [mv] of studio.local.toml to a Python with Demucs and librosa)",
    "[mv] tools_python の Python が見つかりません: {}。{}": "The [mv] tools_python Python was not found: {0}. {1}",
    "MV の音声処理の道具(Demucs と librosa)が見つかりません: {}。{}": "The MV audio tools (Demucs and librosa) were not found: {0}. {1}",
    # 画面から道具を入れる(mvinstall.py)
    "MV の音声処理の道具を入れている最中です。終わるまでお待ちください": "The MV audio tools are being installed. Please wait until it finishes",
    "MV の音声処理の道具を入れられませんでした(終了コード {})。logs/mvtools-install.log を見てください":
        "Could not install the MV audio tools (exit code {0}). See logs/mvtools-install.log",
    "MV の音声処理の道具の導入を始められません: {}": "Cannot start installing the MV audio tools: {0}",
    # 参照モード用の H3 のキャッシュ(h3cache.py。{} は作るコマンドで、訳さずにそのまま入る)
    "MiniMax-H3 の参照モード用のキャッシュ(transformer_ref_int8、約 32GB)がありません。MV・参照画像を使うショート動画・"
    "動画タブの参照モードに要ります。スタジオを止めてから、次を実行して作ってください(数時間。途中で止めると、作りかけの分は"
    "再実行で最初から作り直します): {}。または setup.bat --only h3(Linux は ./setup.sh --only h3)でも作れます":
        "The MiniMax-H3 cache for the reference modes (transformer_ref_int8, about 32GB) is missing. Music videos, short videos that use "
        "reference images and the Video tab's reference modes need it. Stop the studio, then run the following to build it (it takes hours; "
        "if stopped partway, the unfinished part is rebuilt from scratch on the next run): {0}. You can also build it with "
        "setup.bat --only h3 (./setup.sh --only h3 on Linux)",
    "{}が {} 分を超えたので止めました(ffmpeg)": "Stopped {0} because it exceeded {1} minutes (ffmpeg)",
    "{}を中止しました": "Cancelled {0}",
    "{}に失敗しました(ffmpeg): {}": "{0} failed (ffmpeg): {1}",
    "{}に失敗しました: ffmpeg を起動できません({})": "{0} failed: cannot start ffmpeg ({1})",
    "音声の変換": "audio conversion",
    "切り出す範囲が正しくありません: {} 〜 {} 秒": "Invalid cut range: {0} – {1} seconds",
    "音声の切り出し": "cutting the audio",
    "中止されました": "Cancelled",
    "道具の Python を起動できません: {}({})": "Cannot start the tools Python: {0} ({1})",
    "出力が空です": "The output is empty",
    "結果が JSON のオブジェクトではありません": "The result is not a JSON object",
    "道具の Python が見つかりません: {}。{}": "The tools Python was not found: {0}. {1}",
    "{} 秒": "{0} seconds",
    "{} 分": "{0} minutes",
    "{}が {}を超えたので止めました": "Stopped {0} because it exceeded {1}",
    "{}に失敗しました: {}": "{0} failed: {1}",
    "{}の結果を読めません({})": "Cannot read the result of {0} ({1})",
    "ボーカルの分離": "vocal separation",
    "ボーカルの分離の結果を読めません({})": "Cannot read the vocal separation result ({0})",
    "ボーカルの分離の結果のファイルがありません: {}": "The vocal separation result file is missing: {0}",
    "曲のシーン分割": "splitting the song into scenes",
    "曲のシーン分割の結果を読めません(scenes / duration がありません)": "Cannot read the scene split result (no scenes / duration)",
    "エラー: {}": "Error: {0}",
    "エラー: {}: {}": "Error: {0}: {1}",
    "エラー: 中断されました": "Error: interrupted",
    "画像の解析の答えがオブジェクトではありません": "The image analysis answer is not an object",
    "画像から人物の見た目を読み取れませんでした(人物が写っていないかもしれません)":
        "Could not read a character's appearance from the image (there may be no person in it)",
    "画像の解析の答えを読めませんでした": "Could not read the image analysis answer",
    "画像を読めませんでした({})": "Could not read the image ({0})",
    "{}。画像のプロンプトから見た目を抜き出しました": "{0}. Took the appearance from the image's prompt",
    "{}。画像のプロンプトも無いので、空のままにします(演出は、画像の人物としか書きません)":
        "{0}. The image has no prompt either, so it is left empty (the direction just refers to the person in the image)",
    "曲の聞き取りの答えがオブジェクトではありません": "The song listening answer is not an object",
    "曲から何も聞き取れませんでした": "Could not hear anything in the song",
    "曲の聞き取りの答えを読めませんでした": "Could not read the song listening answer",
    "曲を聞き取れませんでした({})": "Could not listen to the song ({0})",
    "曲の情報が無いので、曲の内容を作れません": "There is no song information, so the song content cannot be written",
    "曲の内容を作れませんでした(LLM が日本語で答えませんでした)": "Could not write the song content (the LLM did not answer in Japanese)",
    "LLM の応答に JSON がありません": "The LLM's response contains no JSON",
    "LLM の応答の JSON を読めません": "Cannot read the JSON in the LLM's response",
    "シーン {} 以降の演出の数が合いません: 期待 {} 個、LLM の答え {} 個":
        "The number of directions from scene {0} on does not match: expected {1}, the LLM gave {2}",
    "シーンの番号が合いません: 期待 {}、LLM の答え {}": "Scene numbers do not match: expected {0}, the LLM gave {1}",
    "{}〜{} 秒の範囲で曲全体({} 秒)を分割できませんでした": "Could not split the whole song ({2} s) into scenes of {0}–{1} seconds",
    "ボーカル": "vocal",
    "{}の音声がありません: {}": "The {0} audio is missing: {1}",
    "シーンの長さの範囲が正しくありません: --min {} --max {}": "Invalid scene length range: --min {0} --max {1}",
    "必要なパッケージがありません({}): この Python には librosa が入っていません。MV タブの「道具を入れる」ボタンか setup --only mvtools で入れてください":
        "A required package is missing ({0}): this Python has no librosa. Install them with the Music Video tab's Install tools button or setup --only mvtools",
    "音声を読めません(44.1kHz の WAV にしてから渡してください): {}": "Cannot read the audio (pass a 44.1 kHz WAV): {0}",
    "入力の音声がありません: {}": "The input audio is missing: {0}",
    "必要なパッケージがありません({}): この Python には Demucs / torch が入っていません。MV タブの「道具を入れる」ボタンか setup --only mvtools で入れてください":
        "A required package is missing ({0}): this Python has no Demucs / torch. Install them with the Music Video tab's Install tools button or setup --only mvtools",
    "Demucs のモデル {} を読み込めません(初回はネットワークが要ります): {}":
        "Cannot load the Demucs model {0} (the first run needs the network): {1}",
    "このモデルにはボーカルのステムがありません: {}": "This model has no vocals stem: {0}",

    # ---- ショート動画(short.py・handlers/short.py・shortscript.py) ----
    "人物": "Person",
    "景色・物": "Scenery / object",
    "音声の準備": "audio preparation",
    "音声を準備します": "Preparing the audio",
    "ショート動画ができました": "The short video is ready",
    "参照画像は {{id, role}} の並びで指定してください": "Reference images must be a list of {{id, role}}",
    "参照画像は {} 枚までです(いま {} 枚)": "Up to {0} reference images (currently {1})",
    "参照画像 {} の役割は「{}」(person)か「{}」(scenery)で指定してください":
        "The role of reference image {0} must be \"{1}\" (person) or \"{2}\" (scenery)",
    "参照画像 {}": "Reference image {0}",
    "参照画像": "Reference image",
    "人物の画像は 1 枚までです(複数の出演者は扱いません)": "Only one person image is allowed (multiple cast members are not supported)",
    "セリフ・ナレーションは {} 文字までです(いま {} 文字)": "Narration is limited to {0} characters (currently {1})",
    "セリフ・ナレーションがあるときは、口を合わせる人物の画像(役割: 人物)を 1 枚入れてください":
        "With narration, add one person image (role: Person) to lip-sync",
    "セリフ・ナレーションを読み上げる話者を選んでください": "Choose a voice to read the narration",
    "話者は整数(AivisSpeech のスタイル ID)で指定してください": "The voice must be an integer (an AivisSpeech style ID)",
    "物語・映像のイメージを書いてください(曲もセリフも無いときは必須です)":
        "Describe the story / visuals (required when there is neither a song nor narration)",
    "長さ": "Length",
    "長さは {}〜{} 秒で指定してください": "Length must be {0}–{1} seconds",
    "最終の音声は {} のいずれかです": "The final audio must be one of {0}",
    "subtitles は true / false で指定してください": "subtitles must be true / false",
    "物語の文章を書いてください(ナレーションの元になります)": "Write the story text (it becomes the basis of the narration)",
    "ナレーションを作れませんでした(LLM が日本語で答えませんでした)": "Could not write the narration (the LLM did not answer in Japanese)",
    "AivisSpeech から不正な WAV データが返されました": "AivisSpeech returned invalid WAV data",
    "合成済みの読み上げを使います": "Using the already synthesized narration",
    "音声合成(AivisSpeech)が設定されていません": "Speech synthesis (AivisSpeech) is not configured",
    "読み上げる文章がありません": "There is no text to read aloud",
    "読み上げを合成しています {} / {} 文": "Synthesizing the narration {0} / {1} sentences",
    "文ごとの合成音声の形式が一致しません": "The synthesized audio formats of the sentences do not match",
    "読み上げの音声を整えています": "Preparing the narration audio",
    "読み上げをシーンに分けています": "Splitting the narration into scenes",
    "シーンに分けられませんでした": "Could not split into scenes",
    "物語の筋を考えています": "Working out the story",
    "シーンの長さと場面を決めています": "Deciding scene lengths and settings",
    "このシーンの参照画像の指定が正しくありません(演出の作成からやり直してください)":
        "This scene's reference images are invalid (redo from writing the direction)",
    "読み上げの音声が見つかりません(分析からやり直してください)": "The narration audio was not found (redo from the analysis)",
    "{}: 読み上げを切り出しています": "{0}: Cutting out the narration",
    "読み上げの文の時刻が見つかりません(分析からやり直してください)": "The narration sentence timings were not found (redo from the analysis)",
    "(テロップを焼き込みながら)": " (burning in captions)",
    "読み上げに曲(BGM)を混ぜて載せています{}": "Mixing the song (BGM) under the narration{0}",
    "読み上げと曲の混合": "mixing the narration and song",
    "読み上げを載せています{}": "Adding the narration{0}",
    "読み上げの差し替え": "replacing the audio with the narration",
    "テロップを焼き込んでいます": "Burning in captions",
    "テロップの焼き込み": "burning in captions",
    "シーンの長さの指定が正しくありません": "Invalid scene length setting",
    "シーン割りの数が合いません: 期待 {} 個、LLM の答え {} 個": "The number of scenes does not match: expected {0}, the LLM gave {1}",
    "時間軸の指定が正しくありません: {}": "Invalid timeline: {0}",
    "セリフのある動画には、人物の画像が必要です(口を合わせる相手がいません)":
        "A video with narration needs a person image (there is no one to lip-sync)",

    # ---- 編集(edit.py・editclip.py・handlers/edit.py) ----
    "state は辞書で指定してください": "state must be an object",
    "書き出しの途中です。終わってからもう一度押してください": "Export is in progress. Press again after it finishes",
    "その生成物は見つかりません(ギャラリーから削除されたかもしれません)": "That output was not found (it may have been deleted from the Gallery)",
    "クリップがありません": "There are no clips",
    "クリップ {} のファイルが見つかりません(ギャラリーから削除されたかもしれません)":
        "The file of clip {0} was not found (it may have been deleted from the Gallery)",
    "コマの抜き出し": "frame extraction",
    "クリップがありません。ギャラリーの動画・画像を入れてください": "There are no clips. Add videos / images from the Gallery",
    "クリップ {} のファイルが見つかりません(削除されたかもしれません)。外してください":
        "The file of clip {0} was not found (it may have been deleted). Remove it",
    "名前を入力してください": "Enter a name",
    "名前は {} 文字までです": "The name is limited to {0} characters",
    "{}を数字で指定してください": "{0} must be a number",
    "{}は数字で指定してください": "{0} must be a number",
    "クリップ {}": "Clip {0}",
    "{}の指定が正しくありません": "{0} is invalid",
    "{}の生成物が見つかりません(ギャラリーから削除されたかもしれません)": "The output for {0} was not found (it may have been deleted from the Gallery)",
    "{}には動画か画像を使ってください(曲は音声の欄に入れます)": "Use a video or image for {0} (songs go in the audio section)",
    "{}(画像)の秒数は {}〜{} 秒で指定してください": "The duration of {0} (image) must be {1}–{2} seconds",
    "{}の開始": "{0} start",
    "{}の終了": "{0} end",
    "{}の開始は 0 秒以上にしてください": "The start of {0} must be 0 seconds or more",
    "{}の終了は開始より後にしてください": "The end of {0} must be after its start",
    "{}の終了が元の動画の長さ({} 秒)を超えています": "The end of {0} exceeds the source video length ({1} s)",
    "{}は短すぎます({} 秒以上にしてください)": "{0} is too short (make it {1} seconds or more)",
    "境目の指定が正しくありません": "Invalid transitions",
    "境目 {} の指定が正しくありません": "Transition {0} is invalid",
    "境目 {} の位置(after)を整数で指定してください": "The position (after) of transition {0} must be an integer",
    "境目 {} の位置が正しくありません(クリップ {} の後ろには境目がありません)":
        "The position of transition {0} is invalid (there is no cut after clip {1})",
    "クリップ {} の後ろの境目が重複しています": "The transition after clip {0} is duplicated",
    "境目の効果は cut / crossfade / fadeblack のいずれかです": "A transition must be one of cut / crossfade / fadeblack",
    "境目 {} の長さ": "Length of transition {0}",
    "クリップ {} の後ろの境目の長さは {}〜{} 秒で指定してください": "The transition after clip {0} must be {1}–{2} seconds",
    "クリップ {} の後ろの境目の長さ({} 秒)が、前後のクリップの半分({} 秒)を超えています":
        "The transition after clip {0} ({1} s) is longer than half of the neighbouring clips ({2} s)",
    "効果の指定が正しくありません": "Invalid effects",
    "効果 {}": "Effect {0}",
    "{}の種類は {} のいずれかです": "The type of {0} must be one of {1}",
    "{}の値": "{0} value",
    "{}の値({})は {}〜{} の範囲で指定してください": "The value of {0} ({1}) must be in the range {2}–{3}",
    "{}({})は区間(開始と終了)を指定してください": "{0} ({1}) needs a range (start and end)",
    "{}の区間がプロジェクトの長さ({} 秒)の外です": "The range of {0} is outside the project length ({1} s)",
    "音声は original / music / mix / none のいずれかです": "Audio must be one of original / music / mix / none",
    "曲の音量": "Song volume",
    "曲の音量は {}〜{} dB で指定してください": "Song volume must be {0}–{1} dB",
    "音声に曲を使うときは、曲を選んでください": "Choose a song to use it as the audio",
    "曲には音声を使ってください": "Use audio for the song",
    "clips は配列で指定してください": "clips must be an array",
    "先頭のフェード": "Fade in",
    "末尾のフェード": "Fade out",
    "フェードの秒数は 0 以上にしてください": "Fade seconds must be 0 or more",
    "先頭と末尾のフェードの合計が、動画の長さ({} 秒)を超えています": "Fade in plus fade out exceeds the video length ({0} s)",
    "挿入するのは、動画か画像の生成物です": "Only video or image outputs can be inserted",
    "画像の秒数": "Image duration",
    "挿入する動画の開始": "Start of the inserted video",
    "挿入する動画の終了": "End of the inserted video",
    "挿入するクリップは短すぎます({} 秒以上にしてください)": "The inserted clip is too short (make it {0} seconds or more)",
    "生成物 {} のファイルが分かりません": "Cannot find the file of output {0}",
    "編集プロジェクトが見つかりません(削除されたかもしれません)": "The edit project was not found (it may have been deleted)",
    "プレビューの番号を整数で指定してください": "The preview number must be an integer",
    "{}が {} 分以上進まないので止めました(ffmpeg)": "Stopped {0} because it made no progress for {1}+ minutes (ffmpeg)",
    "終了コード {}": "exit code {0}",
    "曲のファイルが見つかりません(ギャラリーから削除されたかもしれません)": "The song file was not found (it may have been deleted from the Gallery)",
    "プレビューを作っています": "Making the preview",
    "書き出しています": "Exporting",
    "outputs の外は扱えません: {}": "Paths outside outputs cannot be handled: {0}",
    "kind は {} のいずれかです: {}": "kind must be one of {0}: {1}",
    "MV の制作に無い項目です: {}": "Fields not in an MV project: {0}",
    "編集プロジェクトに無い項目です: {}": "Fields not in an edit project: {0}",
    "これ以上戻せません": "Nothing more to undo",
    "これ以上やり直せません": "Nothing more to redo",

    # ---- プリセット・サンプル(presets.py・samples.py) ----
    "同梱のプリセットは変更できません。複製してから編集してください": "Built-in presets cannot be changed. Duplicate it, then edit",
    "そのプリセットは見つかりません(削除されたかもしれません)": "That preset was not found (it may have been deleted)",
    "tab は {} のいずれかです": "tab must be one of {0}",
    "values は入力欄の値の辞書(JSON のオブジェクト)にしてください": "values must be an object of field values (a JSON object)",
    "values の項目名は空でない文字列にしてください": "Field names in values must be non-empty strings",
    "values に入れられるのは文字・数・真偽値と、それらの配列だけです: {}":
        "values may hold only strings, numbers, booleans and arrays of them: {0}",
    "プリセットの値が大きすぎます({} KB まで)": "The preset values are too large (up to {0} KB)",
    "プリセットの配列か、{{\"presets\": [...]}} の形にしてください": "Use an array of presets, or the form {{\"presets\": [...]}}",
    "プリセットは {{tab, name, values}} の形にしてください": "A preset must have the form {{tab, name, values}}",
    "名前か値のどちらかを指定してください": "Specify either a name or values",
    "「{}」という名前のプリセットがすでにあります": "A preset named \"{0}\" already exists",
    "読み込めるプリセットがありません": "There are no presets to import",
    "一度に読み込めるのは {} 件までです": "Up to {0} can be imported at once",
    "{} 件目: {}": "Item {0}: {1}",
    "そのサンプルは見つかりません": "That sample was not found",
    "{} のファイル名が不正です: {}": "Invalid file name for {0}: {1}",
    "{} のファイルがありません: {}": "The {0} file does not exist: {1}",
    "{} は文字にしてください": "{0} must be a string",
    "sample.json は JSON のオブジェクトにしてください": "sample.json must be a JSON object",
    "id がフォルダ名({})と合いません: {}": "id does not match the folder name ({0}): {1}",
    "tab は {} のいずれかです: {}": "tab must be one of {0}: {1}",
    "assets の {} 件目: role は {} のいずれかです": "assets item {0}: role must be one of {1}",
    "assets の {} 件目: role {} の kind は {} です": "assets item {0}: the kind of role {1} is {2}",
    "assets の {} 件目: params は辞書にしてください": "assets item {0}: params must be an object",
    "assets の {} 件目": "assets item {0}",
    "id が重なっています": "Duplicate id",
    "{} をギャラリーにコピーできません: {}": "Cannot copy {0} to the Gallery: {1}",
}

# 型に当てはめる表: (正規表現, 英文)。決まった文字の多い型から試す(「{}の指定が…」より「境目 {} の指定が…」を先に)
_EXACT, _TEMPLATES = {}, []
for _k, _v in EN.items():
    if _fields(_k):
        _rx = "".join(re.escape(lit) + ("(.*?)" if f is not None else "") for lit, f, _, _ in _FMT.parse(_k))
        _TEMPLATES.append((len(_literal(_k)), re.compile(_rx, re.S), _v))
    else:
        _EXACT[_literal(_k)] = _v.format()      # {{ }} をほどく
_TEMPLATES.sort(key=lambda t: -t[0])
_TEMPLATES = [(rx, en) for n, rx, en in _TEMPLATES if n > 0]
_NORM = {_norm(k): v for k, v in EN.items()}


@functools.lru_cache(maxsize=4096)
def _tr_en(text: str, depth: int = 0) -> str:
    hit = _EXACT.get(text)
    if hit is not None:
        return hit
    if depth > 4 or not _JP.search(text):
        return text
    for rx, en in _TEMPLATES:
        m = rx.fullmatch(text)
        if m:
            return en.format(*(_tr_en(g, depth + 1) for g in m.groups()))
    return text


def tr(text, lang=None):
    """text を lang(省略時は LANG)の文にする。ja ならそのまま。訳が無ければ元のまま。"""
    lang = lang or LANG.get()
    if lang != "en" or not isinstance(text, str) or not text:
        return text
    if isinstance(text, Msg):
        en = _NORM.get(_norm(text.template))
        if en is not None:
            names = _fields(text.template)
            vals = {k: tr(v, lang) for k, v in text.kwargs.items()}
            return en.format(*(vals[n] for n in names), **vals)
        text = str(text)
    return _tr_en(str(text))


def translate_payload(data, lang=None, key=None):
    """応答の JSON を巡って、MESSAGE_KEYS の項目の文だけを訳す(利用者のデータは触らない)。"""
    lang = lang or LANG.get()
    if lang != "en":
        return data
    if isinstance(data, dict):
        return {k: translate_payload(v, lang, k) for k, v in data.items()}
    if isinstance(data, list):
        return [translate_payload(v, lang, key) for v in data]
    if isinstance(data, str) and key in MESSAGE_KEYS:
        return tr(data, lang)
    return data
