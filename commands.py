"""Bot-side commands: /help, /status, /test, /ping.

The relay reads its source with a user account, but the bot can be talked to
directly — so this module long-polls `getUpdates` and answers. Delivery and
speech are passed in rather than imported, which keeps this module free of a
cycle with relay.py and lets the tests drive it without a network.

Only `TARGET_CHAT_ID` is obeyed. A bot's username is public, so anyone can
message it; commands from any other chat are ignored, not answered.
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field

import httpx
from dotenv import load_dotenv

log = logging.getLogger("relay.commands")

# relay.py imports this module before it calls load_dotenv(), so the file has
# to be read here too — otherwise BOT_TOKEN and TARGET_CHAT_ID are None and
# the bot API is polled as /botNone/getUpdates.
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID")
SOURCE = os.getenv("SOURCE_CHAT", "source_bot")
POLL_TIMEOUT = int(os.getenv("POLL_TIMEOUT", "25"))


def parse_watch_users(raw: str) -> list[str]:
    """Comma-separated usernames, with @-prefixes and stray spaces forgiven."""
    return [name.strip().lstrip("@") for name in raw.split(",") if name.strip()]


# People whose messages are relayed verbatim, wherever they post.
WATCH_USERS = parse_watch_users(os.getenv("WATCH_USERS", "some_trader"))

SAMPLE_ALERT = "OP 📈 0.10277"

HELP = (
    "Commands:\n"
    "/status — uptime, counters, speaker\n"
    "/positions — open Bybit positions with uPnL\n"
    "/close <ticker> — close one position at market, e.g. /close CL\n"
    "/stopall — close every position at market (asks to confirm)\n"
    "/test — push a sample alert through the whole chain\n"
    "/ping — answer if alive\n"
    "/help — this list"
)


@dataclass
class Stats:
    """What the relay knows about itself, for /status."""

    started: float = field(default_factory=time.time)
    relayed: int = 0
    watched: int = 0
    skipped: int = 0
    spoken: int = 0
    last_line: str = ""

    def record(self, line: str, spoke: bool) -> None:
        self.relayed += 1
        self.last_line = line
        if spoke:
            self.spoken += 1


class Refused(Exception):
    """Telegram answered, but said no. Backing off is the only sane response."""


async def fetch_updates(http: httpx.AsyncClient, offset: int | None) -> list[dict]:
    """One long poll. Raises Refused when Telegram rejects the request."""
    params: dict[str, object] = {"timeout": POLL_TIMEOUT}
    if offset is not None:
        params["offset"] = offset
    response = await http.get(
        f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
        params=params,
        timeout=POLL_TIMEOUT + 10,
    )
    payload = response.json()
    if not payload.get("ok"):
        # Returning an empty list here would send us straight back for more,
        # hammering the API for as long as the token stays invalid.
        raise Refused(str(payload))
    updates: list[dict] = payload.get("result", [])
    return updates


def command_of(update: dict) -> tuple[str, str] | None:
    """The command and its argument, if the update is one from the owner.

    "/close@some_bot CL" -> ("close", "CL"); no argument -> ("status", "").
    """
    message = update.get("message")
    if not message:
        return None
    if str(message.get("chat", {}).get("id")) != str(TARGET_CHAT_ID):
        log.warning("ignoring command from chat %s", message.get("chat", {}).get("id"))
        return None
    text = message.get("text", "").strip()
    if not text.startswith("/"):
        return None
    word, _, rest = text.partition(" ")
    return word.removeprefix("/").split("@")[0].lower(), rest.strip()


def uptime(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes}m"
    return f"{minutes}m{secs}s"


def status_text(stats: Stats, speaking: bool) -> str:
    watching = ", ".join(f"@{name}" for name in WATCH_USERS) or "—"
    return (
        f"🟢 up {uptime(time.time() - stats.started)}\n"
        f"source: {SOURCE}\n"
        f"watching: {watching} (all chats)\n"
        f"relayed: {stats.relayed} · watched: {stats.watched} · "
        f"skipped: {stats.skipped} · spoken: {stats.spoken}\n"
        f"speaker: {'on' if speaking else 'off'}\n"
        f"last: {stats.last_line or '—'}"
    )


async def dispatch(
    command: str,
    arg: str,
    stats: Stats,
    send,
    speak,
    speaking: bool,
    positions=None,
    stop_all=None,
    close_one=None,
    market=None,
) -> None:
    """Answer one command. Unknown commands get the help text.

    `positions`, `stop_all` and `close_one` are optional async callables from
    the Bybit side; without them their commands fall through to the help text.
    """
    if command == "ping":
        await send("pong")
    elif command == "status":
        await send(status_text(stats, speaking))
        if market is not None:
            await market()
    elif command == "positions" and positions is not None:
        # An empty answer means the report already went out as a media group.
        report = await positions()
        if report:
            await send(report, True)  # our own markup: HTML bold is safe
    elif command == "stopall" and stop_all is not None:
        await send(await stop_all())
    elif command == "close" and close_one is not None:
        await send(await close_one(arg))
    elif command == "test":
        await send(f"{SAMPLE_ALERT} (test)")
        spoke = await speak(SAMPLE_ALERT, stats.relayed + 1)
        await send("spoke it" if spoke else "speaker silent")
    else:
        await send(HELP)


async def poll(
    http: httpx.AsyncClient,
    stats: Stats,
    send,
    speak,
    speaking: bool,
    positions=None,
    stop_all=None,
    close_one=None,
    market=None,
) -> None:
    """Answer commands until cancelled. Never lets one failure end the loop."""
    offset: int | None = None
    log.info("listening for bot commands")
    while True:
        try:
            updates = await fetch_updates(http, offset)
        except (httpx.HTTPError, ValueError, Refused):
            log.exception("getUpdates failed")
            await asyncio.sleep(5)
            continue
        for update in updates:
            offset = update["update_id"] + 1
            parsed = command_of(update)
            if parsed is None:
                continue
            command, arg = parsed
            log.info("command: /%s %s", command, arg)
            try:
                await dispatch(
                    command,
                    arg,
                    stats,
                    send,
                    speak,
                    speaking,
                    positions,
                    stop_all,
                    close_one,
                    market,
                )
            # Deliberately broad: one bad command must not end the loop.
            except Exception:  # noqa: BLE001
                log.exception("/%s failed", command)
