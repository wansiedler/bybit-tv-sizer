"""Ask the Mac for a picture of the user's own TradingView chart.

The bot draws its own chart from Bybit klines and always will — it works in
a slim container with no browser. This is the other picture: the one the user
was actually looking at, with their layout, their levels and their notes on
it, fetched from the capture service running on the host (see tv_capture.py).

    TV_SHOT_SECRET=          # the same secret the host service was given
    TV_SHOT_URL=http://host.docker.internal:8424
    TV_SHOT_DIR=session/tv-entry

Every answer here is best-effort and says so out loud. A shot that does not
happen — the Mac asleep, the profile signed out, the symbol absent from the
layout — costs the journal a picture and earns a line in Telegram saying
which of those it was. It never costs a close notice, and it never attaches
a frame of something else.
"""

import logging
import os
import re
from pathlib import Path

import httpx
from dotenv import load_dotenv

log = logging.getLogger("relay.tvshot")

# relay.py imports this module before its own load_dotenv, same as speaker.
load_dotenv("bipboop")

TV_SHOT_SECRET = os.getenv("TV_SHOT_SECRET", "")
# The host, from inside the container. Docker Desktop points
# host.docker.internal at the Mac, where the service listens on loopback.
TV_SHOT_URL = os.getenv("TV_SHOT_URL", "http://host.docker.internal:8424")
# A warm shot is 8-12s, a symbol the layout has to be driven to about 20.
# Past this the notice is better off with the bot's own chart.
TV_SHOT_WAIT = float(os.getenv("TV_SHOT_WAIT", "40"))
# Where an entry frame waits for its trade to close. Inside the session
# volume, so it survives the rebuild that a code change needs.
TV_SHOT_DIR = os.getenv("TV_SHOT_DIR", "session/tv-entry")
# Bybit's "SOLUSDT" is TradingView's "SOLUSDT.P" on a perpetual.
TV_SHOT_SUFFIX = os.getenv("TV_SHOT_SUFFIX", ".P")

# What may become a file name. The symbol comes from Bybit, not from a
# stranger, but it ends up as a path and that is reason enough.
_SAFE = re.compile(r"[^A-Z0-9.]")


def enabled() -> bool:
    return bool(TV_SHOT_SECRET and TV_SHOT_URL)


def tv_symbol(symbol: str) -> str:
    """The TradingView ticker for a Bybit one."""
    return f"{symbol.upper()}{TV_SHOT_SUFFIX}"


def _file(symbol: str) -> Path:
    """Where `symbol`'s entry frame lives. Always inside TV_SHOT_DIR.

    Separators are dropped rather than escaped, and leading dots with them:
    what is left is a file name, so there is no spelling of a symbol that
    names a file anywhere else.
    """
    return Path(TV_SHOT_DIR) / f"{_SAFE.sub('', symbol.upper()).lstrip('.')}.png"


async def shot(http: httpx.AsyncClient, symbol: str) -> tuple[bytes | None, str]:
    """One frame of `symbol`, or None and the reason there is not one."""
    if not enabled():
        return None, ""
    try:
        response = await http.get(
            f"{TV_SHOT_URL}/shot/{TV_SHOT_SECRET}",
            params={"symbol": tv_symbol(symbol)},
            timeout=TV_SHOT_WAIT,
        )
    # Deliberately broad: a Mac asleep, a service not running and a socket
    # dying mid-frame are one answer here — no picture, and say so.
    except Exception as error:  # noqa: BLE001
        log.warning("tv capture unreachable: %s", error)
        return None, "the capture service did not answer"
    if response.status_code != 200:
        return None, response.text[:200] or f"the capture service said {response.status_code}"
    return response.content, ""


def keep(symbol: str, png: bytes) -> str:
    """Hold an entry frame until the trade closes. Returns why it did not."""
    try:
        path = _file(symbol)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png)
    # Deliberately broad: a full or read-only volume must not cost a notice.
    except OSError as error:
        log.warning("could not keep the entry frame: %s", error)
        return "the entry frame could not be saved"
    return ""


def take(symbol: str) -> bytes | None:
    """The entry frame kept for `symbol`, consumed. None when there is none."""
    path = _file(symbol)
    try:
        png = path.read_bytes()
        path.unlink()
    # Deliberately broad: no frame and an unreadable frame are the same
    # answer, and a trade that opened before this feature has neither.
    except OSError:
        return None
    return png


async def capture_entry(http: httpx.AsyncClient, symbol: str, say) -> None:
    """Shoot the entry and keep it for the close. Never raises.

    Runs behind the entry notice rather than in front of it: a notice that
    waits twelve seconds for a picture is a worse notice.
    """
    png, why = await shot(http, symbol)
    if png is not None:
        why = keep(symbol, png)
    if why:
        await say(f"📷 {symbol}: {why}")
