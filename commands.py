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

log = logging.getLogger("relay.commands")

BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID")
SOURCE = os.getenv("SOURCE_CHAT", "source_bot")
POLL_TIMEOUT = int(os.getenv("POLL_TIMEOUT", "25"))

SAMPLE_ALERT = "OP 📈 0.10277"

HELP = (
    "Commands:\n"
    "/status — uptime, counters, speaker\n"
    "/test — push a sample alert through the whole chain\n"
    "/ping — answer if alive\n"
    "/help — this list"
)


@dataclass
class Stats:
    """What the relay knows about itself, for /status."""

    started: float = field(default_factory=time.time)
    relayed: int = 0
    skipped: int = 0
    spoken: int = 0
    last_line: str = ""

    def record(self, line: str, spoke: bool) -> None:
        self.relayed += 1
        self.last_line = line
        if spoke:
            self.spoken += 1


async def fetch_updates(http: httpx.AsyncClient, offset: int | None) -> list[dict]:
    """One long poll. Returns the updates, or an empty list on any refusal."""
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
        log.warning("getUpdates refused: %s", payload)
        return []
    updates: list[dict] = payload.get("result", [])
    return updates


def command_of(update: dict) -> str | None:
    """The command in an update, if it is one and it came from the owner."""
    message = update.get("message")
    if not message:
        return None
    if str(message.get("chat", {}).get("id")) != str(TARGET_CHAT_ID):
        log.warning("ignoring command from chat %s", message.get("chat", {}).get("id"))
        return None
    text = message.get("text", "").strip()
    if not text.startswith("/"):
        return None
    # "/status@some_bot arg" -> "status"
    word: str = text.split()[0]
    return word.removeprefix("/").split("@")[0].lower()


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
    return (
        f"🟢 up {uptime(time.time() - stats.started)}\n"
        f"source: {SOURCE}\n"
        f"relayed: {stats.relayed} · skipped: {stats.skipped} · spoken: {stats.spoken}\n"
        f"speaker: {'on' if speaking else 'off'}\n"
        f"last: {stats.last_line or '—'}"
    )


async def dispatch(command: str, stats: Stats, send, speak, speaking: bool) -> None:
    """Answer one command. Unknown commands get the help text."""
    if command == "ping":
        await send("pong")
    elif command == "status":
        await send(status_text(stats, speaking))
    elif command == "test":
        await send(f"{SAMPLE_ALERT} (test)")
        spoke = await speak(SAMPLE_ALERT, stats.relayed + 1)
        await send("spoke it" if spoke else "speaker silent")
    else:
        await send(HELP)


async def poll(http: httpx.AsyncClient, stats: Stats, send, speak, speaking: bool) -> None:
    """Answer commands until cancelled. Never lets one failure end the loop."""
    offset: int | None = None
    log.info("listening for bot commands")
    while True:
        try:
            updates = await fetch_updates(http, offset)
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("getUpdates failed: %s", exc)
            await asyncio.sleep(5)
            continue
        for update in updates:
            offset = update["update_id"] + 1
            command = command_of(update)
            if command is None:
                continue
            log.info("command: /%s", command)
            try:
                await dispatch(command, stats, send, speak, speaking)
            except Exception as exc:  # noqa: BLE001 - one bad command is not fatal
                log.error("/%s failed: %s", command, exc)
