@echo off
rem YUZU Creator Studio setup for Windows: prepares uv and the studio's .venv, then hands over to scripts\setup.py.
rem   .\setup.bat                  engines + environments + models + MV audio tools (about 175GB in total; hours, depending on the line)
rem   .\setup.bat --no-models      environments only (models later)
rem   .\setup.bat --only h3,ace    only the given engines
rem   .\setup.bat --llm            also fetch the bundled LLM (about 5GB) up front
rem   .\setup.bat --no-mv          skip the MV audio tools (Demucs + librosa, CPU torch, about 1.3GB; installed by default)
rem   .\setup.bat --only mvtools   only those tools
rem   .\setup.bat --shortcut       only the desktop launcher icon
rem Safe to run again: finished parts are skipped and an interrupted run continues where it stopped.
rem
rem This file is ASCII only and never changes the code page. chcp clears the screen when it switches to or from 932
rem (microsoft/terminal#9446): in Windows Terminal it wiped the last messages of setup.py ("failed ..." / "run setup again")
rem and left only a prompt at the top (2026-11-28, Windows 11). Japanese text is printed by scripts\setup.py and
rem scripts\setup_notice.ps1, which write Unicode to the console themselves.
setlocal
cd /d "%~dp0"
set "UV_NEW=0"
set "PATH=%USERPROFILE%\.local\bin;%LOCALAPPDATA%\Microsoft\WinGet\Links;%PATH%"

where uv >nul 2>nul
if not errorlevel 1 goto have_uv
echo [1/3] Installing uv
where winget >nul 2>nul
if not errorlevel 1 goto uv_winget
echo winget was not found, so uv is installed with the official PowerShell installer
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

echo [2/3] Studio Python environment (.venv)
if not exist ".venv\Scripts\python.exe" (
  uv venv --python 3.12 .venv || goto failed
)
uv pip install --python .venv\Scripts\python.exe -r requirements.txt || goto failed

echo [3/3] Engines: download and prepare
.venv\Scripts\python.exe scripts\setup.py %*
set "RC=%ERRORLEVEL%"
if "%UV_NEW%"=="1" call :notice uv_new "uv was newly installed. Reopen PowerShell to use uv yourself; .\run.bat works as it is."
endlocal & exit /b %RC%

:uv_notice
call :notice uv_reopen "uv was installed. Close this window, open PowerShell again and run .\setup.bat once more."
endlocal & exit /b 1

:failed
call :notice venv_failed "Could not create the studio environment. Check the output above and run .\setup.bat again."
endlocal & exit /b 1

:notice
rem %1 = message key in scripts\setup_notice.ps1 (Japanese + English), %2 = the English text if PowerShell cannot run
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup_notice.ps1" %1
if errorlevel 1 echo %~2
exit /b 0
