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

if [[ ! -x "$VENV/bin/python" ]]; then
    echo "building the capture venv in $VENV"
    python3 -m venv "$VENV"
fi
# Cheap when nothing changed, and it heals a venv left half-built.
"$VENV/bin/pip" install -q -r "$HERE/requirements-capture.txt"

cd "$HERE"
exec "$VENV/bin/python" tv_capture.py
