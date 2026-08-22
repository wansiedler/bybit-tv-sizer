"""Read relayed alerts out loud on a Google Nest speaker.

A Cast device never receives audio directly — it is handed a URL and fetches
the file itself. So this module does two things: it serves the generated
speech over HTTP on the LAN, and it tells the speaker where to find it.

That is also why `TTS_HOST` must be the machine's LAN address, not localhost:
the URL is resolved by the speaker, not by us.

    CAST_HOST=192.168.1.177     # the Nest, from `dns-sd -B _googlecast._tcp`
    TTS_HOST=192.168.1.178      # this machine, as the speaker sees it
    TTS_PORT=8422               # published to the LAN in docker-compose.yml
    SPEAK_ALERTS=1              # 0 to keep the speaker quiet
"""

import asyncio
import logging
import os
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import NAMESPACE_DNS, UUID, uuid5

from dotenv import load_dotenv

log = logging.getLogger("relay.speaker")

# Imported by relay.py ahead of its own load_dotenv(), so read the file here
# as well — without it CAST_HOST/TTS_HOST are empty and speaking stays off.
load_dotenv()

CAST_HOST = os.getenv("CAST_HOST", "")
CAST_PORT = int(os.getenv("CAST_PORT", "8009"))
# pychromecast identifies a device by UUID. The real one comes from mDNS
# (`dns-sd -B _googlecast._tcp`); without it a stable stand-in derived from
# the address does just as well, since we address the speaker by host.
CAST_UUID = os.getenv("CAST_UUID", "")
TTS_HOST = os.getenv("TTS_HOST", "")
TTS_PORT = int(os.getenv("TTS_PORT", "8422"))
# Not /tmp: a predictable path in a world-writable directory is a
# swap-the-file-under-us invitation. The directory is created 0700.
TTS_DIR = Path(os.getenv("TTS_DIR") or Path.home() / ".cache/lexx-relay/tts")
SPEAK_ALERTS = os.getenv("SPEAK_ALERTS", "1").lower() not in ("0", "false", "no", "")
# A speaker already running an app (YouTube Music, radio) hands our URL to that
# app, which ignores it — the alert is silently swallowed. Quitting first is the
# only way to be heard, at the cost of stopping whatever was playing. Set
# SPEAK_INTERRUPT=0 to stay quiet instead of interrupting.
SPEAK_INTERRUPT = os.getenv("SPEAK_INTERRUPT", "1").lower() not in ("0", "false", "no", "")
# Google's default media receiver: the app that plays a plain URL.
MEDIA_RECEIVER = "CC1AD845"

# 📈 and 📉 carry the whole meaning of the line and are unpronounceable.
TREND_WORDS = {"📈": "up", "📉": "down"}


def enabled() -> bool:
    """Speaking needs both endpoints; without them the relay just stays quiet."""
    return bool(SPEAK_ALERTS and CAST_HOST and TTS_HOST)


def spoken(compact: str) -> str | None:
    """Turn a relayed line into something a speaker can pronounce.

    "OP 📈 0.10277" -> "OP up, 0.10277". Anything not in that shape returns
    None rather than guessing.
    """
    parts = compact.split()
    if len(parts) != 3:
        return None
    symbol, trend, price = parts
    word = TREND_WORDS.get(trend)
    if word is None:
        return None
    return f"{symbol} {word}, {price}"


class _QuietHandler(SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler, minus a request line per fetch on stderr."""

    def log_message(self, format: str, *args: object) -> None:
        log.debug("tts http: " + format, *args)


def ensure_dir() -> None:
    """Create the audio directory and hold it at 0700.

    mkdir's `mode` applies only when it creates the directory, so a directory
    that already exists — pre-created in the image, or left from an earlier
    run — would keep whatever permissions it had. chmod every time instead.
    """
    TTS_DIR.mkdir(parents=True, exist_ok=True)
    TTS_DIR.chmod(0o700)


def serve_forever() -> ThreadingHTTPServer:
    """Start the audio file server the speaker will fetch from."""
    ensure_dir()
    handler = partial(_QuietHandler, directory=str(TTS_DIR))
    httpd = ThreadingHTTPServer(("0.0.0.0", TTS_PORT), handler)  # noqa: S104
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    log.info("tts server on 0.0.0.0:%s serving %s", TTS_PORT, TTS_DIR)
    return httpd


def write_speech(text: str, name: str) -> Path:
    """Render text to an mp3 in the served directory and return its path."""
    from gtts import gTTS

    ensure_dir()
    path = TTS_DIR / name
    gTTS(text=text, lang="en").save(str(path))
    return path


def cast_url(url: str) -> None:
    """Point the speaker at a URL and wait for it to accept the media.

    Blocking: pychromecast is a synchronous library. Call it off the loop.
    """
    import pychromecast

    uuid = UUID(CAST_UUID) if CAST_UUID else uuid5(NAMESPACE_DNS, CAST_HOST)
    cast = pychromecast.get_chromecast_from_host(
        (CAST_HOST, CAST_PORT, uuid, None, None), timeout=10
    )
    try:
        cast.wait(timeout=10)
        if cast.app_id not in (None, MEDIA_RECEIVER):
            if not SPEAK_INTERRUPT:
                log.info("speaker busy with %s, staying quiet", cast.status.display_name)
                return
            log.info("interrupting %s", cast.status.display_name)
            cast.quit_app()
            deadline = time.monotonic() + 10
            while cast.app_id not in (None, MEDIA_RECEIVER) and time.monotonic() < deadline:
                time.sleep(0.5)
        controller = cast.media_controller
        # audio/mpeg is the registered MP3 type; audio/mp3 is not, and some
        # Cast receivers refuse it.
        controller.play_media(url, "audio/mpeg")
        controller.block_until_active(timeout=15)
    finally:
        cast.disconnect()


async def announce(compact: str, counter: int) -> bool:
    """Speak one relayed line. Never raises: a mute speaker is not an outage."""
    if not enabled():
        return False
    text = spoken(compact)
    if text is None:
        log.debug("not speakable: %s", compact)
        return False
    name = f"alert-{counter % 20}.mp3"
    try:
        await asyncio.to_thread(write_speech, text, name)
        await asyncio.to_thread(cast_url, f"http://{TTS_HOST}:{TTS_PORT}/{name}")
    # Deliberately broad: speaking is best-effort and must never propagate.
    except Exception:  # noqa: BLE001
        log.exception("could not speak %r", text)
        return False
    log.info("spoke: %s", text)
    return True
