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
import sys

import httpx
from dotenv import load_dotenv
from telethon import TelegramClient, events

from parser import parse_alert

load_dotenv()

API_ID = os.getenv("TG_API_ID")
API_HASH = os.getenv("TG_API_HASH")
SOURCE = os.getenv("SOURCE_CHAT", "lexx_dra_bot")
BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID")
SESSION = os.getenv("SESSION_NAME", "lexx_relay")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("relay")


def require_config() -> None:
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


async def check() -> None:
    """Validate every moving part before leaving the relay unattended."""
    require_config()
    client = TelegramClient(SESSION, int(API_ID), API_HASH)
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
        bot = (
            await http.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getMe", timeout=15
            )
        ).json()
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
    require_config()
    client = TelegramClient(SESSION, int(API_ID), API_HASH)

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
        log.info("listening to %s as @%s", SOURCE, me.username or me.id)
        await client.run_until_disconnected()


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
