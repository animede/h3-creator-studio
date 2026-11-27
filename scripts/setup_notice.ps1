# setup.bat のお知らせ(日本語と英語)。setup.bat は ASCII だけで書く(chcp は Windows Terminal で画面を消す。
# microsoft/terminal#9446)ので、日本語はここから出す。Write-Host はコンソールへ Unicode で書くので、コードページに左右されない。
# Windows PowerShell 5.1 が UTF-8 と分かるように、このファイルは BOM 付きの UTF-8 で保存する。
param([string]$Key)
$root = Split-Path -Parent $PSScriptRoot
$messages = @{
    "uv_reopen" = @(
        " uv を入れましたが、この画面からはまだ見えません。",
        " この画面を閉じて、PowerShell を開き直し、",
        "     cd $root; .\setup.bat",
        " をもう一度実行してください。",
        " ここまでで入れたのは uv だけです。本番の準備は 2 回目の実行で始まります。",
        " (uv was installed. Close this window, open PowerShell again and run .\setup.bat once more.)"
    )
    "venv_failed" = @(
        " 管理役の環境を作れませんでした。上の出力を確認して、.\setup.bat をもう一度実行してください。",
        " (Could not create the studio environment. Check the output above and run .\setup.bat again.)"
    )
    "uv_new" = @(
        " 今回 uv を新しく入れました。",
        " この画面で uv コマンドを自分で使うなら、PowerShell を開き直してください。",
        " .\run.bat だけなら、このままで動きます。",
        " (uv was newly installed. Reopen PowerShell to use uv yourself; .\run.bat works as it is.)"
    )
}
if (-not $messages.ContainsKey($Key)) { exit 1 }
$rule = "=" * 60
Write-Host ""
Write-Host $rule
foreach ($line in $messages[$Key]) { Write-Host $line }
Write-Host $rule
exit 0
