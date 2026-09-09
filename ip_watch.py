"""Watch the household's external IP and shout when it changes.

The Bybit API key is locked to a whitelist; a silent IP change from the
provider would cut the sizer and the watcher off without a word. This
watcher notices within a minute and tells the bot to update the list.

    IP_WATCH=1          # 0 disables the watcher
    IP_POLL_SEC=60      # seconds between checks
"""

import asyncio
import logging
import os

import httpx
from dotenv import load_dotenv

log = logging.getLogger("relay.ip")

# relay.py imports this module before its own load_dotenv, same as speaker.
load_dotenv("bipboop")

ENABLED = os.getenv("IP_WATCH", "1") == "1"
POLL = float(os.getenv("IP_POLL_SEC", "60"))
URL = "https://api.ipify.org"


def enabled() -> bool:
    return ENABLED


async def current(http: httpx.AsyncClient) -> str:
    """The external IPv4 as the world sees it."""
    response = await http.get(URL, timeout=10)
    return response.text.strip()


async def poll(http: httpx.AsyncClient, send, speak) -> None:
    """Announce IP changes until cancelled. Failures never end the loop."""
    log.info("watching the external ip every %ss", POLL)
    known: str | None = None
    while True:
        try:
            ip = await current(http)
            if known is None:
                known = ip
                log.info("external ip: %s", ip)
            elif ip != known:
                await send(
                    f"⚠️ Внешний IP сменился: {known} → {ip}\n"
                    f"Обнови whitelist API-ключа на Bybit, иначе бот отвалится!"
                )
                await speak("External IP changed, update the Bybit whitelist")
                known = ip
        except asyncio.CancelledError:
            raise
        # Deliberately broad: a flaky check must not kill the watcher.
        except Exception:  # noqa: BLE001
            log.exception("ip check failed")
        await asyncio.sleep(POLL)
