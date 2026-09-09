"""Receive your own TradingView alerts over a webhook and announce them.

TradingView POSTs the alert message to a URL; this module listens for it,
relays the text through the bot and speaks it within SPEAK_HOURS. The path
carries a secret, so the port being reachable does not mean anyone can make
your speaker talk:

    TV_WEBHOOK_SECRET=          # empty disables the receiver
    TV_PORT=8423                # published to the LAN in docker-compose.yml

TradingView must reach this from the internet — a tunnel (cloudflared,
Tailscale Funnel) or a router port-forward in front of TV_PORT.
"""

import asyncio
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dotenv import load_dotenv

log = logging.getLogger("relay.tv")

# relay.py imports this module before its own load_dotenv, same as speaker.
load_dotenv("bipboop")

TV_WEBHOOK_SECRET = os.getenv("TV_WEBHOOK_SECRET", "")
TV_PORT = int(os.getenv("TV_PORT", "8423"))
# GET /j answers with a redirect to the trading journal, so the sheet has a
# short address on the own domain. Empty keeps the path dead silent.
JOURNAL_URL = os.getenv("JOURNAL_URL", "")
# The public origin the tunnel exposes this webhook on, for the up notice.
TV_PUBLIC_URL = os.getenv("TV_PUBLIC_URL", "")
# TradingView alert messages are short; anything huge is not an alert.
MAX_BODY = 4096


def enabled() -> bool:
    return bool(TV_WEBHOOK_SECRET)


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
        if self.path != f"/tv/{TV_WEBHOOK_SECRET}":
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
        if JOURNAL_URL and self.path == "/j":
            self.send_response(302)
            self.send_header("Location", JOURNAL_URL)
            self.end_headers()
            return
        if self.path != f"/tv/{TV_WEBHOOK_SECRET}":
            self._refuse()
            return
        body = (
            "lexx-relay · TradingView webhook\nAlive. Alerts arrive as POST from TradingView.\n"
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


async def pump(queue: asyncio.Queue, send, speak) -> None:
    """Announce queued alerts until cancelled. One bad alert never ends it."""
    while True:
        text = await queue.get()
        try:
            await send(f"🔔 TV: {text[:1000]}")
            await speak(text[:200])
        except asyncio.CancelledError:
            raise
        # Deliberately broad: announcing is best-effort, the queue must live.
        except Exception:  # noqa: BLE001
            log.exception("could not announce tv alert")
