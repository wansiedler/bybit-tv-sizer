"""Relay LEXX Draco alerts to your own bot in a compact form.

A Telegram bot cannot read messages sent by another bot, so the source side
runs on your user account (Telethon) and only the delivery side uses the bot.

    python relay.py --check    verify config, resolve source, send a test line
    python relay.py            listen and relay
"""

import argparse
import asyncio
import logging
import os
import signal
import sys
import time

import httpx
from dotenv import load_dotenv
from telethon import TelegramClient, events

from parser import parse_alert

load_dotenv()

API_ID = os.getenv("TG_API_ID")
API_HASH = os.getenv("TG_API_HASH")
SOURCE = os.getenv("SOURCE_CHAT", "source_bot")
BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID")
SESSION = os.getenv("SESSION_NAME", "lexx_relay")
RELAY_NAME = os.getenv("RELAY_NAME", "lexx-relay")
NOTIFY_LIFECYCLE = os.getenv("NOTIFY_LIFECYCLE", "1").lower() not in (
    "0",
    "false",
    "no",
    "",
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("relay")
# httpx logs the full request URL at INFO, which would print BOT_TOKEN.
logging.getLogger("httpx").setLevel(logging.WARNING)


def require_config() -> tuple[int, str]:
    """Fail fast on missing config, and hand back the Telegram app credentials."""
    missing = [
        name
        for name, value in (
            ("TG_API_ID", API_ID),
            ("TG_API_HASH", API_HASH),
            ("BOT_TOKEN", BOT_TOKEN),
            ("TARGET_CHAT_ID", TARGET_CHAT_ID),
        )
        if not value
    ]
    if missing:
        sys.exit(f"Missing in .env: {', '.join(missing)}")
    # The list above already covers this; the explicit test is what narrows
    # str | None to str for the type checker.
    if not API_ID or not API_HASH:
        sys.exit("Missing in .env: TG_API_ID, TG_API_HASH")
    if not API_ID.isdigit():
        sys.exit(f"TG_API_ID must be numeric, got {API_ID!r}")
    return int(API_ID), API_HASH


async def send_via_bot(http: httpx.AsyncClient, text: str) -> bool:
    """Post one line through the bot. Returns True when Telegram accepted it."""
    try:
        response = await http.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": TARGET_CHAT_ID, "text": text},
            timeout=15,
        )
    except httpx.HTTPError as exc:
        log.error("sendMessage failed: %s", exc)
        return False

    if response.status_code != 200:
        log.error("sendMessage %s: %s", response.status_code, response.text)
        return False

    log.info("sent: %s", text)
    return True


def human(seconds: float) -> str:
    """Uptime as something readable in a phone notification."""
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes}m"
    if minutes:
        return f"{minutes}m{secs}s"
    return f"{secs}s"


async def notify(http: httpx.AsyncClient, text: str) -> None:
    """Lifecycle ping. A missed notice must never take the relay down."""
    if not NOTIFY_LIFECYCLE:
        return
    try:
        await send_via_bot(http, text)
    except Exception as exc:  # noqa: BLE001 - shutdown path, log and move on
        log.error("lifecycle notice failed: %s", exc)


async def check() -> None:
    """Validate every moving part before leaving the relay unattended."""
    api_id, api_hash = require_config()
    client = TelegramClient(SESSION, api_id, api_hash)
    await client.start()

    me = await client.get_me()
    log.info("account: @%s (%s)", me.username or "-", me.id)

    try:
        source = await client.get_entity(SOURCE)
        log.info("source resolved: %s (%s)", SOURCE, source.id)
    except (ValueError, TypeError) as exc:
        log.error("cannot resolve SOURCE_CHAT=%r: %s", SOURCE, exc)
        await client.disconnect()
        sys.exit(1)

    async with httpx.AsyncClient() as http:
        bot = (await http.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getMe", timeout=15)).json()
        if not bot.get("ok"):
            log.error("bot token rejected: %s", bot)
            await client.disconnect()
            sys.exit(1)
        log.info("bot: @%s", bot["result"]["username"])

        sample = parse_alert(
            "🔔 #OPUSDT OPUSDT, Пересечение 0.10282\n"
            "-  exchange:  #BybitFutures\n"
            "-  trend: 📈\n"
            "-  price: 0.10277"
        )
        ok = await send_via_bot(http, f"{sample} (relay test)")

    await client.disconnect()
    if not ok:
        sys.exit(1)
    log.info("check passed")


async def run() -> None:
    api_id, api_hash = require_config()
    client = TelegramClient(SESSION, api_id, api_hash)

    stop = asyncio.Event()
    stop_reason = "connection lost"

    def request_stop(signame: str) -> None:
        nonlocal stop_reason
        if not stop.is_set():
            stop_reason = signame
            log.info("%s received, shutting down", signame)
            stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, request_stop, sig.name)

    async with httpx.AsyncClient() as http:

        @client.on(events.NewMessage(chats=SOURCE))
        async def handler(event):
            compact = parse_alert(event.raw_text)
            if compact is None:
                log.debug("skipped: %s", event.raw_text[:80].replace("\n", " "))
                return
            await send_via_bot(http, compact)

        await client.start()
        me = await client.get_me()
        who = me.username or me.id
        log.info("listening to %s as @%s", SOURCE, who)

        started = time.time()
        await notify(http, f"🟢 {RELAY_NAME} up — listening {SOURCE} as @{who}")

        listening = asyncio.create_task(client.run_until_disconnected())
        stopping = asyncio.create_task(stop.wait())
        _, pending = await asyncio.wait({listening, stopping}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

        uptime = human(time.time() - started)
        await notify(http, f"🔴 {RELAY_NAME} down — {stop_reason}, uptime {uptime}")

    await client.disconnect()
    log.info("stopped cleanly after %s", uptime)


def main() -> None:
    argparser = argparse.ArgumentParser(description=__doc__)
    argparser.add_argument(
        "--check",
        action="store_true",
        help="verify config and send one test line, then exit",
    )
    args = argparser.parse_args()
    asyncio.run(check() if args.check else run())


if __name__ == "__main__":
    main()
