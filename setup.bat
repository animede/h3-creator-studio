@echo off
for /f "tokens=2 delims=:" %%a in ('chcp') do set "OLDCP=%%a"
set "OLDCP=%OLDCP: =%"
rem UTF-8 first: every line below (Japanese text included) is parsed with this code page
chcp 65001 >nul
rem YUZU Creator Studio のセットアップ(Windows)。uv を用意し、管理役の .venv を作って、scripts\setup.py に渡す。
rem   .\setup.bat                  部品の取り込み + 環境 + モデル(合計 約 150GB、回線次第で数時間)
rem   .\setup.bat --no-models      環境だけ(モデルは後で)
rem   .\setup.bat --only h3,ace    指定した部品だけ
rem   .\setup.bat --llm            同梱 LLM(約 5GB)も先に取得する
rem   .\setup.bat --mv             MV の音声処理の道具(Demucs + librosa、CPU 版 torch。約 1.3GB)も用意する
rem   .\setup.bat --only mvtools   その道具だけ
rem 何度実行してもよい。済んでいる所は飛ばし、途中で止まっても続きから進む。
setlocal
cd /d "%~dp0"
set "UV_NEW=0"
set "PATH=%USERPROFILE%\.local\bin;%LOCALAPPDATA%\Microsoft\WinGet\Links;%PATH%"

where uv >nul 2>nul
if not errorlevel 1 goto have_uv
echo [1/3] uv を入れます
where winget >nul 2>nul
if not errorlevel 1 goto uv_winget
echo winget が見つからないので、公式の PowerShell インストーラで入れます
powershell -ExecutionPolicy ByPass -NoProfile -Command "irm https://astral.sh/uv/install.ps1 | iex"
goto uv_installed
:uv_winget
winget install --id astral-sh.uv -e --source winget --accept-source-agreements --accept-package-agreements
:uv_installed
set "UV_NEW=1"
where uv >nul 2>nul
if errorlevel 1 goto uv_notice
goto uv_ok
:have_uv
echo [1/3] uv
:uv_ok
uv --version

echo [2/3] 管理役の Python 環境 .venv
if not exist ".venv\Scripts\python.exe" (
  uv venv --python 3.12 .venv || goto failed
)
uv pip install --python .venv\Scripts\python.exe -r requirements.txt || goto failed

echo [3/3] 部品の取り込みと準備
.venv\Scripts\python.exe scripts\setup.py %*
set "RC=%ERRORLEVEL%"
if "%UV_NEW%"=="1" call :notice_after
chcp %OLDCP% >nul
endlocal & exit /b %RC%

:uv_notice
echo.
echo ============================================================
echo  uv を入れましたが、この画面からはまだ見えません。
echo  この画面を閉じて、PowerShell を開き直し、
echo      .\setup.bat
echo  をもう一度実行してください。
echo  ここまでで入れたのは uv だけです。本番の準備は 2 回目の実行で始まります。
echo ============================================================
chcp %OLDCP% >nul
endlocal & exit /b 1

:failed
echo.
echo 管理役の環境を作れませんでした。上の出力を確認して、.\setup.bat をもう一度実行してください。
chcp %OLDCP% >nul
endlocal & exit /b 1

:notice_after
echo.
echo ============================================================
echo  今回 uv を新しく入れました。
echo  この画面で uv コマンドを自分で使うなら、PowerShell を
echo  開き直してください。.\run.bat だけなら、このままで動きます。
echo ============================================================
exit /b 0
