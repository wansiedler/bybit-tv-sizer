"""Every ERROR the relay logs, in the chat where you will actually see it.

The journal stopped writing on the 12th and nobody noticed until the 17th,
because the only trace was `ERROR sheets refused: 403` in a container log
no one reads. Some failures already reached Telegram — a refused amend, a
refused rebuild — but each had to be wired by hand, and whatever nobody
thought to wire stayed invisible.

So this is not another hand-wired case. It is a logging handler: anything
any module logs at ERROR goes out, including code written after it.

    NOTIFY_ERRORS=1          # 0 silences the whole thing
    ERROR_REPEAT_SEC=900     # the same reason again, no sooner than this
    ERROR_BURST=4            # at most this many in a window
    ERROR_WINDOW_SEC=60      # the window the burst is counted over

Two guards, because an error channel that floods is one you learn to
ignore. A standing fault — no margin, a dead endpoint — says itself once
and then holds its tongue; and a storm is capped, with the ones held back
counted rather than dropped quietly.
"""

import asyncio
import logging
import os
import time

from dotenv import load_dotenv

# relay.py imports this module before its own load_dotenv, same as speaker.
load_dotenv("bipboop")

log = logging.getLogger("relay.errors")

NOTIFY_ERRORS = os.getenv("NOTIFY_ERRORS", "1").lower() not in ("0", "false", "no", "")
REPEAT_SEC = float(os.getenv("ERROR_REPEAT_SEC", "900"))
BURST = int(os.getenv("ERROR_BURST", "4"))
WINDOW_SEC = float(os.getenv("ERROR_WINDOW_SEC", "60"))

# How much of a message survives: enough to name the fault, not enough to
# bury the chat. Tracebacks stay in the log, where they are readable.
MAX_LEN = 400


def reason(record: logging.LogRecord) -> str:
    """What makes two errors the same error.

    The logger and the message, not the formatted line: a timestamp or a
    changing price would otherwise make every repeat look like news.
    """
    try:
        return f"{record.name}:{record.getMessage()[:200]}"
    # Deliberately broad: a record whose own formatting throws must not
    # take down the handler that was trying to report something else.
    except Exception:  # noqa: BLE001
        return f"{record.name}:<unformattable>"


def describe(record: logging.LogRecord) -> str:
    """The line that goes to the chat: the fault, and where it came from."""
    try:
        text = record.getMessage()
    # Deliberately broad: see reason().
    except Exception:  # noqa: BLE001
        text = "(a message that would not format)"
    if record.exc_info and record.exc_info[1] is not None:
        # The exception itself, not its traceback — one line that names the
        # thing, where the log keeps the full story.
        text = f"{text} — {record.exc_info[1]}"
    text = " ".join(text.split())
    if len(text) > MAX_LEN:
        text = text[: MAX_LEN - 1] + "…"
    return f"⛔️ {record.name}: {text}"


class Gate:
    """Decides what gets through: one line per reason, a cap per window."""

    def __init__(
        self, burst: int = BURST, window: float = WINDOW_SEC, repeat: float = REPEAT_SEC
    ) -> None:
        self.burst, self.window, self.repeat = burst, window, repeat
        self._said: dict[str, float] = {}
        self._opened = 0.0
        self._sent = 0
        self._held = 0

    def admit(self, key: str, now: float) -> bool:
        """Whether this reason may speak now."""
        said = self._said.get(key)
        if said is not None and now - said < self.repeat:
            return False
        if now - self._opened >= self.window:
            # A new window reopens the allowance, but not the backlog: what
            # was held back is still unreported, and zeroing it here would
            # lose the count the next message is supposed to carry.
            self._opened, self._sent = now, 0
        if self._sent >= self.burst:
            self._held += 1
            return False
        # Reasons older than the repeat window can never suppress anything
        # again, and a relay that runs for weeks should not remember them.
        if len(self._said) > 256:
            self._said = {k: v for k, v in self._said.items() if now - v < self.repeat}
        self._said[key] = now
        self._sent += 1
        return True

    def held(self) -> int:
        """How many were kept back in this window; reported once, then zero."""
        held, self._held = self._held, 0
        return held


class Telegram(logging.Handler):
    """Hands every ERROR to the bot, from whatever thread logged it.

    Logging is synchronous and the sender is not, so the record is passed
    to the loop rather than awaited here. A failure to send must never be
    reported through this handler — that is a loop with no bottom — so the
    sender's own errors are dropped on the floor and left to the log.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, send) -> None:
        super().__init__(level=logging.ERROR)
        self._loop, self._send, self._gate = loop, send, Gate()
        self._sending = False

    def emit(self, record: logging.LogRecord) -> None:
        if self._sending or record.name.startswith("relay.errors"):
            return
        if not self._gate.admit(reason(record), time.monotonic()):
            return
        held = self._gate.held()
        text = describe(record)
        if held:
            text += f"\n…и ещё {held} — смотрите лог"
        try:
            self._loop.call_soon_threadsafe(self._spawn, text)
        # Deliberately broad: a closed or missing loop must not raise out of
        # logging, which is called from everywhere including shutdown.
        except Exception:  # noqa: BLE001
            pass

    def _spawn(self, text: str) -> None:
        asyncio.ensure_future(self._deliver(text))

    async def _deliver(self, text: str) -> None:
        self._sending = True
        try:
            await self._send(text)
        # Deliberately broad: the error channel failing is not itself news
        # worth sending through the error channel.
        except Exception:  # noqa: BLE001
            log.exception("could not report an error to the chat")
        finally:
            self._sending = False


def watch(loop: asyncio.AbstractEventLoop, send) -> Telegram | None:
    """Start forwarding errors; None when NOTIFY_ERRORS is off."""
    if not NOTIFY_ERRORS:
        return None
    handler = Telegram(loop, send)
    logging.getLogger().addHandler(handler)
    return handler
