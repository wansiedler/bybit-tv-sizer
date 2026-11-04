"""Receive your own TradingView alerts over a webhook and announce them.

TradingView POSTs the alert message to a URL; this module listens for it,
relays the text through the bot and speaks it within SPEAK_HOURS. The path
carries a secret, so the port being reachable does not mean anyone can make
your speaker talk:

    TV_WEBHOOK_SECRET=          # empty disables the receiver
    TV_PORT=8423                # published on TV_BIND (loopback by default)

TradingView must reach this from the internet — a tunnel (cloudflared,
Tailscale Funnel) or a router port-forward in front of TV_PORT.
"""

import asyncio
import hmac
import logging
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dotenv import load_dotenv

from coin_names import COIN_NAMES
from speaker import rounded

log = logging.getLogger("relay.tv")

# relay.py imports this module before its own load_dotenv, same as speaker.
load_dotenv("bipboop")

TV_WEBHOOK_SECRET = os.getenv("TV_WEBHOOK_SECRET", "")
TV_PORT = int(os.getenv("TV_PORT", "8423"))
# The trading journal's sheet link, for the up notice and /links only: the
# listener never serves it — the journal is every trade and the deposit.
JOURNAL_URL = os.getenv("JOURNAL_URL", "")
# The public origin the tunnel exposes this webhook on, for the up notice.
TV_PUBLIC_URL = os.getenv("TV_PUBLIC_URL", "")
# The same alert text again within this many seconds is the same alert.
# TradingView sent "Тень Binance спот вылезла за Bybit" twice forty seconds
# apart on 02.11.2026, and both arrived in the chat stamped 13:02, looking
# for all the world like the bot had hiccuped. 0 passes everything through.
TV_REPEAT_SEC = float(os.getenv("TV_REPEAT_SEC", "60"))
# TradingView alert messages are short; anything huge is not an alert.
MAX_BODY = 4096
# At and above this the fraction of a price is noise, and the line reads
# better without it. Below it every digit is kept.
WHOLE_ABOVE = 1000


def enabled() -> bool:
    return bool(TV_WEBHOOK_SECRET)


def _on_secret_path(path: str) -> bool:
    """Whether the request path is exactly the secret one, in constant time."""
    return hmac.compare_digest(path.encode(), f"/tv/{TV_WEBHOOK_SECRET}".encode())


class _Handler(BaseHTTPRequestHandler):
    """Accepts POST /tv/<secret>, hands the body to the asyncio side."""

    # Injected by serve(): the running loop and the queue living on it.
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue

    def _refuse(self) -> None:
        """Hang up without a single byte of answer.

        To anyone off the secret path the server does not exist: a browser
        shows its "page unavailable" error, Cloudflare shows a bad gateway.
        """
        self.close_connection = True
        self.connection.close()

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        if not _on_secret_path(self.path):
            self._refuse()
            return
        length = min(int(self.headers.get("Content-Length") or 0), MAX_BODY)
        text = self.rfile.read(length).decode("utf-8", errors="replace").strip()
        if text:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, text)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        """The alive page on the exact secret path; dead silence elsewhere."""
        if not _on_secret_path(self.path):
            self._refuse()
            return
        body = (
            "bybit-tv-sizer · TradingView webhook\nAlive. Alerts arrive as POST from TradingView.\n"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        log.debug("tv http: " + format, *args)


def serve(loop: asyncio.AbstractEventLoop, queue: asyncio.Queue) -> ThreadingHTTPServer:
    """Start the webhook listener; alerts land on `queue` in the given loop."""
    handler = type("BoundHandler", (_Handler,), {"loop": loop, "queue": queue})
    httpd = ThreadingHTTPServer(("0.0.0.0", TV_PORT), handler)  # noqa: S104
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    log.info("tv webhook on 0.0.0.0:%s", TV_PORT)
    return httpd


# "ETHUSDT.P Crossing 2,440.85" and friends, with an optional direction word.
_CROSSING = re.compile(
    r"^(?P<symbol>[A-Z0-9]+?)(?:USDT)?(?:\.P)?\s+Crossing(?:\s+(?P<dir>Up|Down))?"
    r"\s+(?P<level>[\d,]+(?:\.\d+)?)\s*$",
    re.IGNORECASE,
)


def _whole(level: str) -> str:
    """The level without its fraction, once the fraction stops being news.

    BTC crossing "78,787.90" is BTC crossing 78,787: ninety cents on eighty
    thousand is a thousandth of a percent, and the two digits only cost the
    eye. Cheap coins keep every digit they came with — 0.1027 without its
    fraction is 0.
    """
    whole, _, fraction = level.partition(".")
    if not fraction:
        return level
    digits = whole.replace(",", "")
    return whole if digits.isdigit() and int(digits) >= WHOLE_ABOVE else level


def format_alert(text: str, came_from: float | None = None) -> tuple[str, str]:
    """The (sent, spoken) pair for one alert.

    A TradingView crossing becomes the relay's own compact shape —
    "ETH 📉 2,440.85". The direction comes from the alert when it names
    one, else from `came_from` — where the market was just before the cross:
    arriving from above means it crossed downward. With neither the arrow is
    dropped. Anything unrecognised passes through raw.
    """
    match = _CROSSING.match(text.strip())
    if match is None:
        return f"🔔 TV: {text[:1000]}", text[:200]
    symbol = match["symbol"].upper()
    level = match["level"]
    arrow = ""
    if match["dir"]:
        arrow = "📈" if match["dir"].lower() == "up" else "📉"
    elif came_from is not None:
        arrow = "📉" if came_from > float(level.replace(",", "")) else "📈"
    middle = f" {arrow} " if arrow else " "
    spoken_dir = {"📈": "up", "📉": "down"}.get(arrow, "")
    # Read the same way channel alerts are: coin name and a price for the ear,
    # "Ethereum down, 2440" rather than "ETH down, 2,440.85".
    name = COIN_NAMES.get(symbol, symbol)
    spoken = f"{name}{f' {spoken_dir}' if spoken_dir else ''}, {rounded(level.replace(',', ''))}"
    return f"{symbol}{middle}{_whole(level)}", spoken


def _already_said(said_at: dict[str, float], text: str, now: float) -> bool:
    """Whether this exact text was already announced inside the window.

    Exact, deliberately: "USDT.D Crossing 6.708%" and "6.730%" are two
    different things happening, and only a character-for-character repeat
    is the same alert arriving twice.

    A pure question. Remembering is _mark_said's job and happens only once
    the alert has actually gone out — marking first would mean a failed
    send suppresses the retry, which is precisely the delivery this exists
    to collapse.
    """
    if TV_REPEAT_SEC <= 0 or not text:
        return False
    before = said_at.get(text)
    return before is not None and now - before < TV_REPEAT_SEC


def _mark_said(said_at: dict[str, float], text: str, now: float) -> None:
    """Remember an alert that reached the chat, so its echo does not."""
    if TV_REPEAT_SEC <= 0 or not text:
        return
    # Texts older than the window can never suppress anything again, and a
    # relay that runs for weeks should not remember every alert it ever saw.
    if len(said_at) > 256:
        stale = [k for k, v in said_at.items() if now - v >= TV_REPEAT_SEC]
        for key in stale:
            del said_at[key]
    said_at[text] = now


async def pump(queue: asyncio.Queue, send, speak, price_of=None) -> None:
    """Announce queued alerts until cancelled. One bad alert never ends it.

    `price_of` (async, bare ticker -> float | None) supplies the pre-cross
    price that orients the arrow when the alert itself names no direction.
    """
    # The last text announced and when, so a repeat can be recognised. Kept
    # here rather than in a module global: it is this pump's memory, and a
    # restart should start deaf to whatever came before it.
    said_at: dict[str, float] = {}
    while True:
        text = await queue.get()
        try:
            seen = text.strip()
            if _already_said(said_at, seen, time.monotonic()):
                log.info("same alert within %gs, not repeating: %.60s", TV_REPEAT_SEC, text)
                continue
            price = None
            match = _CROSSING.match(text.strip())
            if price_of is not None and match is not None and not match["dir"]:
                try:
                    price = await price_of(match["symbol"].upper())
                # Deliberately broad: no price just means no arrow.
                except Exception:  # noqa: BLE001
                    log.exception("price lookup failed")
            line, spoken = format_alert(text, price)
            await send(line)
            # Only now: an alert that never reached the chat must not
            # silence the retry that would have carried it there.
            _mark_said(said_at, seen, time.monotonic())
            await speak(spoken)
        except asyncio.CancelledError:
            raise
        # Deliberately broad: announcing is best-effort, the queue must live.
        except Exception:  # noqa: BLE001
            log.exception("could not announce tv alert")
