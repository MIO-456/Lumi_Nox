#!/usr/bin/env bash
# Standard verification entry point (macOS / Linux / Git Bash).
#
#   ./init.sh            # compile + docs + tests
#   PYTHON=py ./init.sh  # force a specific interpreter
#
# Every check here is offline: no network, no audio device, no game, no model.
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

echo "== python: $($PY --version 2>&1) =="
echo "== compile =="
"$PY" -m compileall -q .

echo "== docs =="
"$PY" scripts/check_docs.py

echo "== tests =="
"$PY" -m unittest discover -s tests

echo "== all checks passed =="
echo ""
echo "Next steps:"
echo "  1. Read feature_list.json and pick ONE not-started feature"
echo "  2. Read the doc routed by AGENTS.md section 4"
echo "  3. Re-run this script before claiming done"
