@echo off
rem Start the studio (creates .venv with uv, or python -m venv if uv is missing).
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  where uv >nul 2>nul
  if not errorlevel 1 (
    uv venv --python 3.12 .venv && uv pip install --python .venv\Scripts\python.exe -r requirements.txt
  ) else (
    python -m venv .venv && .venv\Scripts\pip install -r requirements.txt
  )
)
.venv\Scripts\python.exe -m studio.main %*
if errorlevel 1 pause
exit /b %errorlevel%
