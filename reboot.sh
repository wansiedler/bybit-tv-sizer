#!/bin/bash
# Say "Rebooting" on the Nest, then restart the machine.
#
# The speech goes through the running relay container: it already holds the
# TTS port and has the cast stack, so the host needs neither. Aliased in
# ~/.zshrc so both `reboot` and `sudo reboot` land here (the `sudo ` alias
# makes zsh expand the word after sudo). Best-effort: a mute speaker or a
# stopped container must never block the reboot itself.
set -u

# Explicit socket: root has no "colima" docker context, and the alias hands
# this script to sudo as-is.
export DOCKER_HOST="unix:///Users/me/.colima/default/docker.sock"

/usr/local/bin/docker exec lexx-relay python -c \
    'import asyncio, speaker; asyncio.run(speaker.say("Rebooting", "reboot.mp3"))' \
    && sleep 2

if [[ "$(id -u)" -eq 0 ]]; then
    exec /sbin/reboot
fi
exec sudo /sbin/reboot
