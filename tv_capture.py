"""Shoot the user's own TradingView layout and serve one pane as a PNG.

The journal already carries a chart per closed trade, but it is one the bot
draws itself from Bybit klines. This serves the other one: the chart the user
actually looks at, with their layout, their levels, their annotations.

It runs on the Mac, not in the relay container, because the frame has to come
out of a logged-in browser and that session lives in a dedicated Chrome
profile here:

    TV_SHOT_SECRET=          # empty disables the service
    TV_SHOT_PORT=8424
    TV_SHOT_BIND=127.0.0.1   # the container reaches this through
                             # host.docker.internal; the LAN never does
    TV_SHOT_LAYOUT=https://www.tradingview.com/chart/FzeTDQEI/
    TV_SHOT_PROFILE=~/.bybit-tv-sizer/tv-profile

Chrome's DevTools port is the blunt door behind this one: whoever reaches it
drives the whole browser and every session in the profile. It stays bound to
127.0.0.1, and this service never proxies it — the single thing it answers is
"a PNG of symbol X", on a secret path, to a caller that already knows the
secret.

Started by launchd beside the relay; see com.bybit.tv-capture.plist.
"""

import hashlib
import hmac
import logging
import os
import pathlib
import re
import subprocess
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit

from dotenv import load_dotenv

log = logging.getLogger("tv.capture")

load_dotenv("bipboop")

TV_SHOT_SECRET = os.getenv("TV_SHOT_SECRET", "")
TV_SHOT_PORT = int(os.getenv("TV_SHOT_PORT", "8424"))
# Loopback by default. Docker Desktop forwards host.docker.internal to the
# host's own loopback, so the container reaches this without it ever being
# offered to the LAN. Widen this only if you know why you are doing it.
TV_SHOT_BIND = os.getenv("TV_SHOT_BIND", "127.0.0.1")
TV_SHOT_LAYOUT = os.getenv("TV_SHOT_LAYOUT", "")
TV_SHOT_CDP = os.getenv("TV_SHOT_CDP", "http://127.0.0.1:9222")
TV_SHOT_PROFILE = os.path.expanduser(os.getenv("TV_SHOT_PROFILE", "~/.bybit-tv-sizer/tv-profile"))
TV_SHOT_CHROME = os.getenv(
    "TV_SHOT_CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
)
# The exchange prefix the fallback asks TradingView for. A pane already in
# the layout keeps whatever feed the user put it on; this only decides what
# a symbol absent from the grid is drawn from.
TV_SHOT_EXCHANGE = os.getenv("TV_SHOT_EXCHANGE", "BYBIT")
# Timeframe for that fallback pane. Empty leaves TradingView's own last
# choice for the symbol, which is whatever the user last looked at it on —
# a weekly chart of a five-minute trade, often enough. Set it to "15" or "5"
# and every fallback frame reads the same.
TV_SHOT_INTERVAL = os.getenv("TV_SHOT_INTERVAL", "")
# What one pane should come out as. 760x460 keeps the user's markup legible
# in the journal at about 40 KB a frame.
TV_SHOT_WIDTH = int(os.getenv("TV_SHOT_WIDTH", "760"))
TV_SHOT_HEIGHT = int(os.getenv("TV_SHOT_HEIGHT", "460"))
# Seconds to let the quotes land before shooting. The websocket never goes
# idle, so there is nothing to wait *for* — this is the settle, measured.
TV_SHOT_SETTLE = float(os.getenv("TV_SHOT_SETTLE", "6"))
# Whole-request ceiling. A shot is 8-20s; past this the caller is better off
# with the bot's own chart than with a hung notice.
TV_SHOT_TIMEOUT = float(os.getenv("TV_SHOT_TIMEOUT", "45"))

# The probe pass measures the grid; its size is arbitrary and never shot.
PROBE = (2000, 1200)
PANE = ".chart-widget"
# TradingView's own pane heading, "SOLUSDT.P\n1D\nBybit\n...".
_CHROME_WAIT = 20.0
# A symbol is a ticker, and it is about to be spliced into a URL this
# service then navigates to. Anything else is not a symbol.
_SYMBOL = re.compile(r"^[A-Z0-9]{2,15}(?:\.P)?$")


class Unavailable(Exception):
    """No frame, and the reason a human should be told."""


def is_closed(exc: BaseException) -> bool:
    """Whether the browser went away, rather than something really breaking.

    Matched on the words rather than the class: playwright keeps its error
    types under a private module, and pinning this file to an internal path
    is how it breaks on the next upgrade.
    """
    text = str(exc).lower()
    return (
        "connection closed" in text
        or "target page, context or browser has been closed" in text
        or "browser has been closed" in text
    )


class Closed(Exception):
    """The attached browser went away under us.

    Not the same as Chrome being down: the port can answer while the handle
    we hold belongs to a Chrome that has since restarted. Worth one
    reconnect rather than a message.
    """


def enabled() -> bool:
    return bool(TV_SHOT_SECRET and TV_SHOT_LAYOUT)


# What this process is running, fixed when it started. The file on disk can
# change under it — a merge, a deploy — and nothing says so: 17.10.2026 this
# service ran two-day-old code for two days while the fix sat unread beside
# it, and three deploys reported success without touching it.
RUNNING = hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest()[:12]


def on_disk() -> str:
    """What the file beside this process says now; "" when unreadable."""
    try:
        return hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest()[:12]
    # Deliberately broad: not knowing is not the same as being stale, and a
    # version check must never be the thing that breaks the service.
    except OSError:
        return ""


def _on_secret_path(path: str) -> bool:
    """Whether the request path is exactly the secret one, in constant time."""
    return hmac.compare_digest(path.encode(), f"/shot/{TV_SHOT_SECRET}".encode())


def _on_version_path(path: str) -> bool:
    """The same secret, asking what is running rather than for a frame."""
    return hmac.compare_digest(path.encode(), f"/version/{TV_SHOT_SECRET}".encode())


def wanted(query: str) -> str:
    """The symbol asked for, or "" when what was asked for is not one."""
    values = parse_qs(query).get("symbol") or []
    symbol = (values[0] if values else "").strip().upper()
    return symbol if _SYMBOL.match(symbol) else ""


def grid(boxes: list[dict]) -> tuple[int, int]:
    """Columns and rows of panes, read off where the panes actually sit."""
    return len({round(box["x"]) for box in boxes}), len({round(box["y"]) for box in boxes})


def viewport(probe: tuple[int, int], pane: tuple[float, float], cells: tuple[int, int]) -> tuple:
    """The window size that makes one pane come out TV_SHOT_WIDTH x HEIGHT.

    The grid never fills the window: toolbars and the watchlist take a fixed
    bite out of it. Measuring that bite once, at the probe size, is what lets
    the second pass land the pane on an exact size whatever grid the layout
    is in — a 4x4 of small panes, or a 5x1 of tall columns where every pane
    is a portrait strip and the candles are unreadable.
    """
    cols, rows = cells
    return (
        round(TV_SHOT_WIDTH * cols + (probe[0] - pane[0] * cols)),
        round(TV_SHOT_HEIGHT * rows + (probe[1] - pane[1] * rows)),
    )


# TradingView suffixes a perpetual with ".P" where the exchange does not:
# a pane reading FARTCOINUSDT.P is the FARTCOINUSDT position. Any other
# difference is a different instrument.
PERPETUAL = ".P"


def pick(heads: list[str], want: str) -> int | None:
    """Which pane shows `want`, or None.

    Exact once TradingView's perpetual suffix is off, and never a prefix: a
    frame of the wrong symbol in the journal is worse than no frame at all,
    and "ARB" is a prefix of plenty.

    17.10.2026: every shot answered "TradingView would not show
    FARTCOINUSDT" while the pane right there read FARTCOINUSDT.P.
    """
    wanted = want.removesuffix(PERPETUAL)
    for index, head in enumerate(heads):
        if head.removesuffix(PERPETUAL) == wanted:
            return index
    return None


def override_url(layout: str, symbol: str) -> str:
    """The layout with one pane driven to `symbol`, without saving anything.

    TradingView autosaves layout edits, so driving a pane by clicking and
    typing would quietly rewrite the user's own grid. The URL parameter does
    not: it overrides the active pane for this tab only, and the saved
    layout comes back untouched on the next plain load. Measured, not hoped.
    """
    url = f"{layout}?symbol={quote(f'{TV_SHOT_EXCHANGE}:{symbol}', safe='')}"
    return f"{url}&interval={quote(TV_SHOT_INTERVAL, safe='')}" if TV_SHOT_INTERVAL else url


def cdp_alive(url: str, opener=urllib.request.urlopen) -> bool:
    """Whether something is already answering DevTools on `url`."""
    try:
        with opener(f"{url}/json/version", timeout=3):
            return True
    except OSError:
        return False


def launch_chrome(popen=subprocess.Popen, alive=cdp_alive, sleep=time.sleep) -> None:
    """Start Chrome headless on the bot's profile and wait for DevTools.

    Only ever called when nothing is answering already: a Chrome the user
    started themselves on that profile is reused as it stands, so the headful
    window they sign in and fix the layout with survives this service
    restarting.
    """
    port = urlsplit(TV_SHOT_CDP).port or 9222
    popen(
        [
            TV_SHOT_CHROME,
            "--headless=new",
            f"--user-data-dir={TV_SHOT_PROFILE}",
            f"--remote-debugging-port={port}",
            # Never the LAN: this port is the whole browser.
            "--remote-debugging-address=127.0.0.1",
            "--no-first-run",
            "--no-default-browser-check",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    waited = 0.0
    while waited < _CHROME_WAIT:
        if alive(TV_SHOT_CDP):
            log.info("chrome up on %s", TV_SHOT_CDP)
            return
        sleep(0.5)
        waited += 0.5
    raise Unavailable("Chrome would not start on the TradingView profile")


def _playwright_browser(cdp_url: str):
    """Attach to the running Chrome over CDP.

    Chrome's own `--screenshot` is useless here: on a logged-in layout the
    quote websocket never goes idle, so `--virtual-time-budget` never expires
    and it hangs forever. CDP has no such idea and simply takes the picture.
    """
    from playwright.sync_api import sync_playwright

    play = sync_playwright().start()
    return play.chromium.connect_over_cdp(cdp_url)


class Shooter:
    """One warm Chrome, one page at a time.

    Every method here runs on the single worker thread `serve` owns: the
    Playwright sync API belongs to the thread that made it, and a
    thread-per-request server would hand it to a different one.
    """

    def __init__(
        self, connect=_playwright_browser, alive=cdp_alive, launch=launch_chrome, take=None
    ):
        self._connect, self._alive, self._launch = connect, alive, launch
        self._take = take or self._take_through
        self._browser = None

    def browser(self):
        """The attached browser, starting Chrome if nothing is up yet."""
        if self._browser is None:
            if not self._alive(TV_SHOT_CDP):
                self._launch()
            self._browser = self._connect(TV_SHOT_CDP)
        return self._browser

    def forget(self) -> None:
        """Drop the cached handle, so the next shot attaches afresh."""
        self._browser = None

    def shot(self, symbol: str) -> bytes:
        """A PNG of the pane showing `symbol`. Raises Unavailable with a reason.

        17.10.2026: every shot came back "Playwright connection closed" for
        hours. Chrome had restarted; the port answered, so `alive` said yes,
        and the handle belonging to the Chrome that was gone was handed out
        again on every request. A live port is not a live channel, and the
        only way to find out is to use it.

        One reconnect, not a loop — a Chrome that is really gone should
        still fail, and say so.
        """
        try:
            return self._take(self.browser(), symbol)
        except Closed:
            self.forget()
        return self._take(self.browser(), symbol)

    def _take_through(self, browser, symbol: str) -> bytes:
        try:
            context = browser.contexts[0]
            self._check_signed_in(context)
            page = context.new_page()
            try:
                return self._shoot(page, symbol)
            finally:
                page.close()
        except Unavailable:
            raise
        # Deliberately broad: anything the handle throws is examined, and
        # only a gone-browser is turned into a reconnect.
        except Exception as exc:  # noqa: BLE001
            if is_closed(exc):
                raise Closed(str(exc)) from exc
            raise

    def _check_signed_in(self, context) -> None:
        """Refuse early when the profile is signed out.

        Read through CDP, never off disk: Chrome keeps cookies in memory and
        flushes them when it feels like it, and the file said "signed out"
        three times while the browser was plainly signed in.
        """
        names = {cookie["name"] for cookie in context.cookies("https://www.tradingview.com")}
        if "sessionid" not in names:
            raise Unavailable("the TradingView profile is signed out")

    def _open(self, page, url: str) -> None:
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_selector("canvas", timeout=60000)
        page.wait_for_timeout(int(TV_SHOT_SETTLE * 1000))

    def _heads(self, page) -> list[str]:
        """The ticker each pane is showing, in pane order."""
        panes = page.locator(PANE)
        return [
            (panes.nth(i).inner_text() or "").split("\n")[0].strip().upper()
            for i in range(panes.count())
        ]

    def _shoot(self, page, symbol: str) -> bytes:
        page.set_viewport_size({"width": PROBE[0], "height": PROBE[1]})
        self._open(page, TV_SHOT_LAYOUT)
        index = pick(self._heads(page), symbol)
        if index is None:
            # Not in the grid: drive one pane to it for this tab only.
            self._open(page, override_url(TV_SHOT_LAYOUT, symbol))
            index = pick(self._heads(page), symbol)
            if index is None:
                raise Unavailable(f"TradingView would not show {symbol}")
        panes = page.locator(PANE)
        boxes = [panes.nth(i).bounding_box() for i in range(panes.count())]
        first = boxes[0]
        width, height = viewport(PROBE, (first["width"], first["height"]), grid(boxes))
        page.set_viewport_size({"width": width, "height": height})
        page.wait_for_timeout(int(TV_SHOT_SETTLE * 1000))
        # The resize reflows the grid. Read the headings again rather than
        # trust that pane 3 is still the pane we picked: a frame of the wrong
        # symbol is the one outcome worth failing over.
        heads = self._heads(page)
        if pick(heads, symbol) != index:
            raise Unavailable(f"the {symbol} pane moved while it was being shot")
        # Playwright is untyped to mypy here; the API returns PNG bytes.
        png: bytes = page.locator(PANE).nth(index).screenshot()
        return png


class _Handler(BaseHTTPRequestHandler):
    """Answers GET /shot/<secret>?symbol=X with a PNG. Nothing else, ever."""

    shoot: staticmethod

    def _refuse(self) -> None:
        """Hang up without a single byte, same as the alert webhook."""
        self.close_connection = True
        self.connection.close()

    def _answer(self, status: int, body: bytes, kind: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        split = urlsplit(self.path)
        if _on_version_path(split.path):
            disk = on_disk()
            answer = f"{RUNNING} {disk} {'stale' if disk and disk != RUNNING else 'fresh'}"
            self._answer(200, answer.encode(), "text/plain; charset=utf-8")
            return
        if not _on_secret_path(split.path):
            self._refuse()
            return
        symbol = wanted(split.query)
        if not symbol:
            self._answer(400, b"not a symbol", "text/plain; charset=utf-8")
            return
        try:
            png = self.shoot(symbol)
        # Deliberately broad: every way a browser can fail is the same answer
        # here — no frame, and a reason the bot can say out loud.
        except Exception as error:  # noqa: BLE001
            log.exception("no shot for %s", symbol)
            self._answer(503, str(error).encode()[:200], "text/plain; charset=utf-8")
            return
        self._answer(200, png, "image/png")

    def log_message(self, format: str, *args: object) -> None:
        log.debug("shot http: " + format, *args)


def serve(shooter=None) -> ThreadingHTTPServer:
    """Start the listener. Browser work runs on one thread, whoever asks.

    Playwright's sync API belongs to the thread that created it, and this is
    a thread-per-request server: every shot goes through the one worker, so
    two closes landing together queue instead of tearing the browser in half.
    """
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tv-shot")
    shooter = shooter or Shooter()

    def one_at_a_time(symbol: str) -> bytes:
        png: bytes = pool.submit(shooter.shot, symbol).result(timeout=TV_SHOT_TIMEOUT)
        return png

    handler = type("BoundHandler", (_Handler,), {"shoot": staticmethod(one_at_a_time)})
    httpd = ThreadingHTTPServer((TV_SHOT_BIND, TV_SHOT_PORT), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    log.info("tv capture on %s:%s", TV_SHOT_BIND, TV_SHOT_PORT)
    return httpd


def main(forever=threading.Event) -> int:
    """Run until killed. Returns a shell exit code when it will not run."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if not enabled():
        log.error("TV_SHOT_SECRET and TV_SHOT_LAYOUT must both be set")
        return 1
    serve()
    forever().wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
