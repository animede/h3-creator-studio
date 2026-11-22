@echo off
rem UTF-8 first: every line below (Japanese text included) is parsed with this code page
chcp 65001 >nul
rem 管理役を起動する(.venv が無ければ uv、無ければ python -m venv で作る)。
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  where uv >nul 2>nul
  if not errorlevel 1 (
    uv venv .venv && uv pip install --python .venv\Scripts\python.exe -r requirements.txt
  ) else (
    python -m venv .venv && .venv\Scripts\pip install -r requirements.txt
  )
)
.venv\Scripts\python.exe -m studio.main %*
