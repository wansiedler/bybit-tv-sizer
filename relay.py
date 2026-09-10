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
from pathlib import Path

import httpx
from dotenv import load_dotenv
from telethon import TelegramClient, events

import bybit_watch
import commands
import ip_watch
import sheets
import sizer
import speaker
import tv_alerts
from parser import parse_alert

load_dotenv("bipboop")

API_ID = os.getenv("TG_API_ID")
API_HASH = os.getenv("TG_API_HASH")
SOURCE = os.getenv("SOURCE_CHAT", "source_bot")
BOT_TOKEN = os.getenv("BOT_TOKEN")
# People whose messages are relayed verbatim, wherever they post.
WATCH_USERS = commands.parse_watch_users(os.getenv("WATCH_USERS", "some_trader"))
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID")
# `or` rather than a getenv default: a SESSION_NAME left blank in bipboop is an
# empty string, not a missing key, and an empty session path is nobody's
# intent.
SESSION = os.getenv("SESSION_NAME") or "session/lexx_relay"
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
        sys.exit(f"Missing in bipboop: {', '.join(missing)}")
    # `or ""` is what narrows str | None to str; the list above has already
    # ruled the empty case out, so no unreachable branch is left behind.
    api_id, api_hash = API_ID or "", API_HASH or ""
    if not api_id.isdigit():
        sys.exit(f"TG_API_ID must be numeric, got {api_id!r}")
    return int(api_id), api_hash


def _accepted(response, what: str) -> bool:
    """Whether Telegram actually took the message.

    A 200 is not acceptance: Telegram reports refusals in the body, and
    treating those as delivered loses the message silently.
    """
    if response.status_code != 200:
        log.error("%s %s: %s", what, response.status_code, response.text)
        return False
    try:
        payload = response.json()
    except ValueError:
        log.error("%s returned no JSON: %s", what, response.text[:200])
        return False
    if not payload.get("ok"):
        log.error("%s refused: %s", what, payload)
        return False
    return True


async def send_via_bot(http: httpx.AsyncClient, text: str, html: bool = False) -> bool:
    """Post one line through the bot. Returns True when Telegram accepted it.

    `html` turns on Telegram's HTML parse mode — only for text we compose
    ourselves; relayed foreign text could break parsing with stray tags.
    """
    payload: dict[str, object] = {"chat_id": TARGET_CHAT_ID, "text": text}
    if html:
        payload["parse_mode"] = "HTML"
    try:
        response = await http.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json=payload,
            timeout=15,
        )
    except httpx.HTTPError:
        log.exception("sendMessage failed")
        return False

    if not _accepted(response, "sendMessage"):
        return False
    log.info("sent: %s", text)
    return True


async def send_album_via_bot(http: httpx.AsyncClient, caption: str, pngs: list[bytes]) -> bool:
    """Post several pictures as one media group — a single Telegram message.

    The caption rides on the first photo; Telegram caps a group at ten.
    """
    import json as _json

    media = []
    files = {}
    for i, png in enumerate(pngs[:10]):
        name = f"p{i}"
        item: dict[str, str] = {"type": "photo", "media": f"attach://{name}"}
        if i == 0:
            item["caption"] = caption
            item["parse_mode"] = "HTML"
        media.append(item)
        files[name] = (f"{name}.png", png, "image/png")
    try:
        response = await http.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMediaGroup",
            data={"chat_id": TARGET_CHAT_ID, "media": _json.dumps(media)},
            files=files,
            timeout=60,
        )
    except httpx.HTTPError:
        log.exception("sendMediaGroup failed")
        return False

    if not _accepted(response, "sendMediaGroup"):
        return False
    log.info("sent album of %s: %s", len(files), caption.splitlines()[0])
    return True


async def send_photo_via_bot(http: httpx.AsyncClient, caption: str, png: bytes) -> bool:
    """Post one picture with a caption. Returns True when Telegram accepted it."""
    try:
        response = await http.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
            data={"chat_id": TARGET_CHAT_ID, "caption": caption, "parse_mode": "HTML"},
            files={"photo": ("chart.png", png, "image/png")},
            timeout=60,
        )
    except httpx.HTTPError:
        log.exception("sendPhoto failed")
        return False

    if not _accepted(response, "sendPhoto"):
        return False
    log.info("sent photo: %s", caption)
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
    # Deliberately broad: the shutdown path must not raise on its way out.
    except Exception:  # noqa: BLE001
        log.exception("lifecycle notice failed")


def _client(api_id: int, api_hash: str) -> TelegramClient:
    """A Telethon client whose session directory is guaranteed to exist.

    Telethon opens the store with sqlite3, which does not create the directory
    above it, and `session/` is gitignored — so on a fresh clone the first run
    would die on "unable to open database file" instead of asking for a login
    code.
    """
    Path(SESSION).parent.mkdir(parents=True, exist_ok=True)
    return TelegramClient(SESSION, api_id, api_hash)


async def check() -> None:
    """Validate every moving part before leaving the relay unattended."""
    api_id, api_hash = require_config()
    client = _client(api_id, api_hash)
    await client.start()

    me = await client.get_me()
    log.info("account: @%s (%s)", me.username or "-", me.id)

    for name in (SOURCE, *WATCH_USERS):
        try:
            entity = await client.get_entity(name)
            log.info("resolved: %s (%s)", name, entity.id)
        except (ValueError, TypeError):
            log.exception("cannot resolve %r", name)
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


class _Shutdown:
    """SIGTERM/SIGINT wired to an event, remembering which signal arrived."""

    def __init__(self) -> None:
        self.event = asyncio.Event()
        self.reason = "connection lost"

    def install(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._request, sig.name)

    def _request(self, signame: str) -> None:
        if self.event.is_set():
            return
        self.reason = signame
        log.info("%s received, shutting down", signame)
        self.event.set()


async def _relay_watched(http: httpx.AsyncClient, stats: commands.Stats, event) -> None:
    """Forward one watched user's message verbatim, with who and where."""
    text = event.raw_text
    if not text:
        return
    sender = await event.get_sender()
    who = getattr(sender, "username", None) or getattr(sender, "first_name", None) or "?"
    chat = await event.get_chat()
    where = getattr(chat, "title", None) or getattr(chat, "username", None) or "?"
    # 4096 is Telegram's hard cap on sendMessage; stay under it.
    await send_via_bot(http, f"👤 @{who} · {where}:\n{text}"[:4000])
    stats.watched += 1


def _register_listeners(client, http: httpx.AsyncClient, stats: commands.Stats) -> None:
    """Attach the alert relay, and the watched-user relay when configured."""

    @client.on(events.NewMessage(chats=SOURCE))
    async def handler(event):
        compact = parse_alert(event.raw_text)
        if compact is None:
            stats.skipped += 1
            log.debug("skipped: %s", event.raw_text[:80].replace("\n", " "))
            return
        await send_via_bot(http, compact)
        spoke = await speaker.announce(compact, stats.relayed + 1)
        stats.record(compact, spoke)

    if not WATCH_USERS:
        return

    @client.on(events.NewMessage(from_users=WATCH_USERS))
    async def watched(event):
        await _relay_watched(http, stats, event)


async def run() -> None:
    api_id, api_hash = require_config()
    client = _client(api_id, api_hash)

    shutdown = _Shutdown()
    shutdown.install()
    audio = None
    webhook = None
    uptime = "0s"

    # Everything below runs under try/finally: an exception on the way up —
    # a rejected login, a speaker port already taken — must still close the
    # client and the audio server rather than leak both.
    try:
        async with httpx.AsyncClient() as http:
            stats = commands.Stats()
            _register_listeners(client, http, stats)

            await client.start()
            me = await client.get_me()
            who = me.username or me.id
            log.info("listening to %s as @%s", SOURCE, who)
            if WATCH_USERS:
                log.info("watching users: %s", ", ".join(f"@{u}" for u in WATCH_USERS))

            # The speaker fetches its audio from us, so the file server has to be
            # up before the first alert can arrive.
            audio = speaker.serve_forever() if speaker.enabled() else None
            speaking = f" · 🔊 {speaker.hours_text()}" if audio is not None else ""
            if audio is not None:
                log.info("speaking hours: %s", speaker.hours_text())

            started = time.time()
            try:
                ip = await ip_watch.current(http)
            # Deliberately broad: no IP answer must not delay the start.
            except Exception:  # noqa: BLE001
                ip = ""
            net = f" · 🌐 {ip}" if ip else ""
            links = ""
            if tv_alerts.JOURNAL_URL:
                links += f"\n📒 {tv_alerts.JOURNAL_URL}"
            if tv_alerts.enabled() and tv_alerts.TV_PUBLIC_URL:
                links += f"\n📡 {tv_alerts.TV_PUBLIC_URL}/tv/{tv_alerts.TV_WEBHOOK_SECRET}"
            await notify(
                http, f"🟢 {RELAY_NAME} up — listening {SOURCE} as @{who}{speaking}{net}{links}"
            )
            if audio is not None:
                await speaker.lifecycle("Relay up")

            answering = asyncio.create_task(
                commands.poll(
                    http,
                    stats,
                    lambda text, html=False: send_via_bot(http, text, html),
                    speaker.announce,
                    speaker.enabled(),
                    lambda: bybit_watch.positions_report(
                        http, lambda caption, pngs: send_album_via_bot(http, caption, pngs)
                    ),
                    lambda: bybit_watch.close_everything(http),
                    lambda arg: bybit_watch.close_position(http, arg),
                    lambda: bybit_watch.market_report(
                        http, lambda caption, png: send_photo_via_bot(http, caption, png)
                    ),
                    lambda arg: bybit_watch.stats_report(
                        http, lambda caption, png: send_photo_via_bot(http, caption, png), arg
                    ),
                    links.strip(),
                    lambda: ip_watch.current(http),
                    lambda arg: bybit_watch.force_leverage_one(http, arg),
                )
            )
            background = {answering}
            if bybit_watch.enabled():
                background.add(
                    asyncio.create_task(
                        bybit_watch.poll(
                            http,
                            # The watcher composes its own markup: HTML is safe.
                            lambda text: send_via_bot(http, text, True),
                            speaker.trade,
                            lambda caption, png: send_photo_via_bot(http, caption, png),
                        )
                    )
                )
            if sizer.enabled():
                background.add(
                    asyncio.create_task(sizer.poll(http, lambda text: send_via_bot(http, text)))
                )
            if bybit_watch.enabled():
                background.add(
                    asyncio.create_task(
                        bybit_watch.money_poll(http, lambda text: send_via_bot(http, text))
                    )
                )
            if sheets.enabled():
                background.add(asyncio.create_task(sheets.weekly(http)))
            if ip_watch.enabled():
                background.add(
                    asyncio.create_task(
                        ip_watch.poll(http, lambda text: send_via_bot(http, text), speaker.trade)
                    )
                )
            if tv_alerts.enabled():
                alerts: asyncio.Queue = asyncio.Queue()
                webhook = tv_alerts.serve(asyncio.get_running_loop(), alerts)
                background.add(
                    asyncio.create_task(
                        tv_alerts.pump(
                            alerts,
                            lambda text: send_via_bot(http, text),
                            speaker.trade,
                            lambda symbol: bybit_watch.price_before(http, symbol),
                        )
                    )
                )
            listening = asyncio.create_task(client.run_until_disconnected())
            stopping = asyncio.create_task(shutdown.event.wait())
            done, pending = await asyncio.wait(
                {listening, stopping}, return_when=asyncio.FIRST_COMPLETED
            )
            pending |= background
            for task in pending:
                task.cancel()
            # `done` is gathered too: if run_until_disconnected raised, its
            # exception has to be retrieved here, or asyncio complains about it
            # at garbage-collection time long after the fact.
            await asyncio.gather(*pending, *done, return_exceptions=True)

            uptime = human(time.time() - started)
            # Telegram first: it is quick, while a dead speaker can eat its
            # timeouts out of the 30s stop grace period.
            await notify(http, f"🔴 {RELAY_NAME} down — {shutdown.reason}, uptime {uptime}")
            if audio is not None:
                await speaker.lifecycle("Relay down")

    finally:
        if audio is not None:
            audio.shutdown()
        if webhook is not None:
            webhook.shutdown()
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
