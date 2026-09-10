#!/bin/bash
# Launchd entry point for com.lexx.relay: heal colima if a reboot left it
# wedged, wait for its docker daemon, then bring the relay container up.
#
# The wedge: an unclean shutdown kills the vz driver before colima removes
# ~/.colima/_lima/colima/vz.pid. After reboot the recorded PID can be reused
# by an unrelated process, so lima reports "vz driver is running but host
# agent is not" and refuses to start. Removing the stale pid file lets the
# com.abiosoft.colima KeepAlive job start cleanly.
set -u

VZ_PIDFILE="$HOME/.colima/_lima/colima/vz.pid"

if [[ -f "$VZ_PIDFILE" ]]; then
    pid=$(cat "$VZ_PIDFILE" 2>/dev/null || true)
    if [[ -n "$pid" ]] && ! ps -p "$pid" -o command= 2>/dev/null | grep -qiE 'lima|colima|vz'; then
        echo "removing stale vz.pid (pid $pid is not a colima process)"
        rm -f "$VZ_PIDFILE"
        # Colima's own KeepAlive would retry within its 30s throttle; kick it
        # now so the docker wait below doesn't burn that time.
        launchctl kickstart -k "gui/$(id -u)/com.abiosoft.colima" || true
    fi
fi

for _ in $(seq 1 60); do
    /usr/local/bin/docker info >/dev/null 2>&1 && break
    sleep 5
done

exec /usr/local/bin/docker compose up -d
