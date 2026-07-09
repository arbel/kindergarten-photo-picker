#!/usr/bin/env bash
# Launcher for macOS / Linux. On first run, creates a virtualenv and installs
# dependencies; on every subsequent run, just launches the app.
set -e
cd "$(dirname "$0")"

# --- Locate a usable Python (3.11+) ---
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    PYTHON="$candidate"
    break
  fi
done

if [ -z "$PYTHON" ]; then
  cat >&2 <<'EOF'
Python 3.11 or newer is required and was not found on this machine.

Install one of:
  • python.org installer:   https://www.python.org/downloads/
  • Homebrew:               brew install python@3.13
  • Xcode CLT (bundled):    xcode-select --install

Then re-run ./run.sh.
EOF
  exit 1
fi

# --- First-run venv + deps setup ---
if [ ! -x ".venv/bin/python" ]; then
  echo "Creating virtual environment with $PYTHON …"
  "$PYTHON" -m venv .venv
  .venv/bin/python -m pip install --upgrade pip --quiet
  echo "Installing dependencies (~200 MB, one-time)…"
  .venv/bin/pip install -r requirements.txt
fi

exec .venv/bin/python -m src.main "$@"
