#!/usr/bin/env bash
# Launcher for macOS / Linux. On first run, creates a virtualenv and installs
# dependencies; on every subsequent run, just launches the app.
set -e
cd "$(dirname "$0")"

MIN_MINOR=11

check_python() {
    # Returns 0 and sets PYTHON if $1 is an executable Python 3.MIN_MINOR+.
    local exe="$1"
    [ -x "$exe" ] || return 1
    local major minor
    major=$("$exe" -c 'import sys; print(sys.version_info.major)' 2>/dev/null) || return 1
    minor=$("$exe" -c 'import sys; print(sys.version_info.minor)' 2>/dev/null) || return 1
    [ "$major" = "3" ] || return 1
    [ "$minor" -ge "$MIN_MINOR" ] || return 1
    PYTHON="$exe"
    return 0
}

find_python() {
    # Explicit paths first — on Macs where Homebrew's bin isn't on PATH we
    # still want to find /opt/homebrew/bin/python3.13.
    local candidate
    for candidate in \
        /opt/homebrew/bin/python3.14 /opt/homebrew/bin/python3.13 \
        /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 \
        /usr/local/bin/python3.14    /usr/local/bin/python3.13 \
        /usr/local/bin/python3.12    /usr/local/bin/python3.11 ; do
        check_python "$candidate" && return 0
    done
    # Then whatever's on PATH.
    for candidate in python3.14 python3.13 python3.12 python3.11 python3 ; do
        local resolved
        resolved=$(command -v "$candidate" 2>/dev/null) || continue
        check_python "$resolved" && return 0
    done
    return 1
}

PYTHON=""
if ! find_python ; then
    cat >&2 <<EOF
Python 3.${MIN_MINOR} or newer is required and was not found.

Install one of:
  • Homebrew:      brew install python@3.13
  • python.org:    https://www.python.org/downloads/

If you installed Homebrew Python but this script still can't find it,
make sure /opt/homebrew/bin (Apple Silicon) or /usr/local/bin (Intel)
is on your PATH — or just re-run ./run.sh, which searches those
locations directly.
EOF
    exit 1
fi

echo "Using: $PYTHON  ($($PYTHON --version 2>&1))"

# If the existing .venv was built with an incompatible interpreter, throw it
# away so we don't try to install modern PySide6 against Python 3.9 again.
if [ -x ".venv/bin/python" ]; then
    venv_minor=$(.venv/bin/python -c 'import sys; print(sys.version_info.minor)' 2>/dev/null || echo 0)
    if [ "$venv_minor" -lt "$MIN_MINOR" ]; then
        echo "Removing stale .venv (was built with Python 3.${venv_minor}, need >= 3.${MIN_MINOR})…"
        rm -rf .venv
    fi
fi

if [ ! -x ".venv/bin/python" ]; then
    echo "Creating virtual environment…"
    "$PYTHON" -m venv .venv
    .venv/bin/python -m pip install --upgrade pip --quiet
    echo "Installing dependencies (~200 MB, one-time)…"
    .venv/bin/pip install -r requirements.txt
fi

exec .venv/bin/python -m src.main "$@"
