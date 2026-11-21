#!/usr/bin/env bash
# 管理役を起動する(.venv が無ければ uv、無ければ python -m venv で作る)。
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  if command -v uv >/dev/null 2>&1; then
    uv venv .venv && uv pip install --python .venv/bin/python -r requirements.txt
  else
    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
  fi
fi
exec .venv/bin/python -m studio.main "$@"
