#!/usr/bin/env bash
# Standard verification entry point (macOS / Linux / Git Bash).
#
#   bash ./init.sh            # compile + docs + tests
#   PYTHON=py bash ./init.sh  # force a specific interpreter
#
# No cloud services, audio device, game, or model. Tests may use loopback.
set -euo pipefail
cd "$(dirname "$0")"

if [ -n "${PYTHON:-}" ]; then
  PY="$PYTHON"
elif command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "init: no python found on PATH" >&2
  exit 127
fi

# Git Bash also accepts a Windows-style interpreter path via PYTHON.
if command -v cygpath >/dev/null 2>&1; then
  case "$PY" in
    [a-zA-Z]:[\\/]*) PY="$(cygpath -u "$PY")" ;;
  esac
fi
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "init: Python executable not found: $PY" >&2
  exit 127
fi

# Set encoding only for each child; leave the calling shell's values intact.
echo "== python =="
PYTHONUTF8=1 PYTHONIOENCODING=utf-8 "$PY" --version
echo "== compile =="
PYTHONUTF8=1 PYTHONIOENCODING=utf-8 "$PY" -m compileall -q .

echo "== docs =="
PYTHONUTF8=1 PYTHONIOENCODING=utf-8 "$PY" scripts/check_docs.py

echo "== tests =="
PYTHONUTF8=1 PYTHONIOENCODING=utf-8 "$PY" -m unittest discover -s tests

echo "== all checks passed =="
echo "Continue the agreed task; use AGENTS.md and docs/README.md for relevant guidance."
