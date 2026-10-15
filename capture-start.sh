#!/bin/bash
# Launchd entry point for com.bybit.tv-capture: the TradingView capture
# service. Builds its own virtualenv the first time, then runs the service in
# the foreground so launchd's KeepAlive can watch it.
#
# Deliberately separate from the relay: this one needs a browser and a
# logged-in profile, which is exactly what the container cannot have.
set -eu

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${TV_SHOT_VENV:-$HOME/.bybit-tv-sizer/capture-venv}"
# The oldest the pinned dependencies install on.
MIN="3.12"

# launchd hands this job a minimal PATH, where python3 is the one Xcode ships
# — old enough that pip resolves python-dotenv down to 0.x and the service
# never starts. A login shell finds Homebrew's instead, which is why this
# works by hand and failed under launchd. Look for a real interpreter rather
# than trust whatever PATH happens to hold.
pick_python() {
    local candidate
    for candidate in \
        "${TV_SHOT_PYTHON:-}" \
        /opt/homebrew/bin/python3.14 /opt/homebrew/bin/python3.13 \
        /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3 \
        /usr/local/bin/python3.14 /usr/local/bin/python3.13 \
        /usr/local/bin/python3.12 /usr/local/bin/python3 \
        "$(command -v python3 2>/dev/null || true)"; do
        [[ -n "$candidate" && -x "$candidate" ]] || continue
        if "$candidate" - <<'PY' 2>/dev/null
import sys
raise SystemExit(0 if sys.version_info >= (3, 12) else 1)
PY
        then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

if ! PYTHON="$(pick_python)"; then
    echo "no python >= $MIN on this machine; set TV_SHOT_PYTHON to one" >&2
    exit 1
fi

# A venv built by an older python is worse than none: it resolves the
# dependencies to versions that predate the code and fails somewhere further
# in. Rebuild it rather than limp.
if [[ -x "$VENV/bin/python" ]] && ! "$VENV/bin/python" - <<'PY' 2>/dev/null
import sys
raise SystemExit(0 if sys.version_info >= (3, 12) else 1)
PY
then
    echo "rebuilding the capture venv: $VENV was built by an older python"
    rm -rf "$VENV"
fi

if [[ ! -x "$VENV/bin/python" ]]; then
    echo "building the capture venv in $VENV with $PYTHON"
    "$PYTHON" -m venv "$VENV"
fi
# Cheap when nothing changed, and it heals a venv left half-built.
"$VENV/bin/python" -m pip install -q --upgrade pip
"$VENV/bin/python" -m pip install -q -r "$HERE/requirements-capture.txt"

cd "$HERE"
exec "$VENV/bin/python" tv_capture.py
