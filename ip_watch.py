"""Watch the household's external IP and the tunnel it rides through.

The Bybit API key is locked to a whitelist; a silent IP change from the
provider would cut the sizer and the watcher off without a word. This
watcher notices within a minute and tells the bot to update the list.

Every Bybit call also leaves through the proxy named in `HTTPS_PROXY` —
httpx reads that variable on its own, so the tunnel is load-bearing without
appearing anywhere in the code. When it drops, nothing recovers by itself
and nothing complains: the calls simply start failing. So the same loop
knocks on the proxy's port and shouts the moment it stops answering.

    IP_WATCH=1          # 0 disables the watcher
    IP_POLL_SEC=60      # seconds between checks
    TUNNEL_TIMEOUT=5    # seconds to wait for the proxy's TCP handshake
"""

import asyncio
import logging
import os
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv

log = logging.getLogger("relay.ip")

# relay.py imports this module before its own load_dotenv, same as speaker.
load_dotenv("bipboop")

ENABLED = os.getenv("IP_WATCH", "1") == "1"
POLL = float(os.getenv("IP_POLL_SEC", "60"))
URL = "https://api.ipify.org"
# The same variable httpx reads; empty means there is no tunnel to watch.
PROXY = os.getenv("HTTPS_PROXY", "") or os.getenv("https_proxy", "")
TUNNEL_TIMEOUT = float(os.getenv("TUNNEL_TIMEOUT", "5"))


def enabled() -> bool:
    return ENABLED


def proxy_endpoint() -> tuple[str, int] | None:
    """Host and port of the proxy every Bybit call rides through, if any."""
    if not PROXY:
        return None
    # A bare "host:port" is a legal value for the variable, and urlparse
    # needs a scheme before it will look for a netloc at all.
    parsed = urlparse(PROXY if "://" in PROXY else f"http://{PROXY}")
    if not parsed.hostname:
        return None
    return parsed.hostname, parsed.port or 80


async def reachable(host: str, port: int) -> bool:
    """True when something accepts a TCP connection there."""
    writer = None
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), TUNNEL_TIMEOUT)
        return True
    except asyncio.CancelledError:
        raise
    # Deliberately broad: refused, timed out, no route — all mean "down".
    except Exception:  # noqa: BLE001
        return False
    finally:
        if writer is not None:
            writer.close()


async def current(http: httpx.AsyncClient) -> str:
    """The external IPv4 as the world sees it."""
    response = await http.get(URL, timeout=10)
    return response.text.strip()


async def poll(http: httpx.AsyncClient, send, speak) -> None:
    """Announce IP changes until cancelled. Failures never end the loop."""
    log.info("watching the external ip every %ss", POLL)
    known: str | None = None
    # None until the first check: a tunnel already down at startup is worth
    # shouting about, a working one is not.
    tunnel: bool | None = None
    while True:
        try:
            endpoint = proxy_endpoint()
            up = await reachable(*endpoint) if endpoint else True
            if endpoint and up is not tunnel:
                where = f"{endpoint[0]}:{endpoint[1]}"
                if not up:
                    await send(
                        f"🚨 Туннель до {where} не отвечает — бот отрезан от Bybit.\n"
                        f"API-ключ ходит только через него. Проверь WireGuard на VPS!"
                    )
                    await speak("Tunnel down, Bybit unreachable")
                elif tunnel is not None:
                    await send(f"✅ Туннель до {where} снова на связи")
                    await speak("Tunnel back up")
            tunnel = up
            # A dead tunnel fails the IP check too; asking would only log noise.
            if up:
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
