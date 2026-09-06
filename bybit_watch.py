"""Watch your own Bybit account and announce position changes through the bot.

Polls the private v5 REST API rather than the realtime WebSocket: httpx is
already a dependency, polling needs no reconnect logic, and a spoken "position
opened" does not care about a few seconds of latency.

    BYBIT_API_KEY=...       # read-only key: Read permission, nothing else
    BYBIT_API_SECRET=...
    BYBIT_POLL=10           # seconds between polls

The watcher is dormant until both keys are present. It never places orders;
the key should not even be able to.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass

import httpx
from dotenv import load_dotenv

import chart
from coin_names import COIN_NAMES
from parser import base_symbol

log = logging.getLogger("relay.bybit")

# relay.py imports this module before its own load_dotenv(), same as speaker.
load_dotenv()

API_KEY = os.getenv("BYBIT_API_KEY", "")
API_SECRET = os.getenv("BYBIT_API_SECRET", "")


def _api_url() -> str:
    """BYBIT_API_URL wins; otherwise BYBIT_TESTNET picks the public host."""
    override = os.getenv("BYBIT_API_URL", "")
    if override:
        return override
    if os.getenv("BYBIT_TESTNET", "0") == "1":
        return "https://api-testnet.bybit.com"
    return "https://api.bybit.com"


API_URL = _api_url()
POLL = int(os.getenv("BYBIT_POLL", "10"))
# Money-management checks on a freshly opened position: complain when the
# reward-to-risk is below MIN_RR (0 disables), or when the actual risk
# strays more than a quarter away from the RISK_PCT the sizer targets.
MIN_RR = float(os.getenv("MIN_RR", "2"))
RISK_TARGET = float(os.getenv("RISK_PCT", "0.5")) / 100

# Kline timeframe for every chart the relay draws, in minutes.
CHART_INTERVAL = os.getenv("CHART_INTERVAL", "15")
# How many bars of context a chart shows. Bybit caps one request at 1000
# bars, so anything above that is fetched in pages.
CHART_BARS = min(int(os.getenv("CHART_BARS", "1800")), 3000)
RECV_WINDOW = "5000"


def enabled() -> bool:
    return bool(API_KEY and API_SECRET)


def sign(timestamp: str, query: str) -> str:
    """Bybit v5 request signature: HMAC over timestamp+key+window+query."""
    payload = timestamp + API_KEY + RECV_WINDOW + query
    return hmac.new(API_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _headers(timestamp: str, payload: str) -> dict[str, str]:
    return {
        "X-BAPI-API-KEY": API_KEY,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-RECV-WINDOW": RECV_WINDOW,
        "X-BAPI-SIGN": sign(timestamp, payload),
    }


def _result(payload: dict, path: str) -> dict:
    if payload.get("retCode") != 0:
        raise RuntimeError(f"bybit {path}: {payload.get('retCode')} {payload.get('retMsg')}")
    result: dict = payload.get("result") or {}
    return result


async def _get(http: httpx.AsyncClient, path: str, params: dict[str, str]) -> dict:
    """One signed GET. Raises on transport errors and on Bybit refusals."""
    query = "&".join(f"{k}={v}" for k, v in params.items())
    timestamp = str(int(time.time() * 1000))
    response = await http.get(
        f"{API_URL}{path}?{query}", headers=_headers(timestamp, query), timeout=15
    )
    return _result(response.json(), path)


async def _post(http: httpx.AsyncClient, path: str, params: dict[str, str]) -> dict:
    """One signed POST: the v5 signature covers the JSON body, verbatim."""
    body = json.dumps(params, separators=(",", ":"))
    timestamp = str(int(time.time() * 1000))
    headers = _headers(timestamp, body) | {"Content-Type": "application/json"}
    response = await http.post(f"{API_URL}{path}", content=body, headers=headers, timeout=15)
    return _result(response.json(), path)


@dataclass(frozen=True)
class Position:
    side: str  # "long" | "short"
    size: float  # in the base coin
    price: float  # average entry
    value: float  # position value, USDT
    take_profit: float | None = None
    stop_loss: float | None = None


async def positions(http: httpx.AsyncClient) -> dict[str, Position]:
    """Open USDT-perpetual positions, keyed by symbol. Zero sizes dropped."""
    result = await _get(http, "/v5/position/list", {"category": "linear", "settleCoin": "USDT"})
    open_now: dict[str, Position] = {}
    for row in result.get("list", []):
        size = float(row.get("size") or 0)
        if size == 0:
            continue
        open_now[row["symbol"]] = Position(
            side="long" if row.get("side") == "Buy" else "short",
            size=size,
            price=float(row.get("avgPrice") or 0),
            value=float(row.get("positionValue") or 0),
            take_profit=float(row["takeProfit"]) if row.get("takeProfit") else None,
            stop_loss=float(row["stopLoss"]) if row.get("stopLoss") else None,
        )
    return open_now


async def _klines(
    http: httpx.AsyncClient,
    symbol: str,
    interval: str,
    limit: int,
    start: int | None = None,
    end: int | None = None,
) -> tuple[list[int], list[chart.Candle]]:
    """Bar start times and candles, oldest first.

    Bybit hands bars newest first, 1000 per request at most; more context is
    paged backwards through `end` until `limit` bars are in hand, the range
    is exhausted, or `start` is reached.
    """
    rows: list[list[str]] = []
    remaining, cursor = limit, end
    while remaining > 0:
        page = min(remaining, 1000)
        params = {"category": "linear", "symbol": symbol, "interval": interval}
        params["limit"] = str(page)
        if start is not None:
            params["start"] = str(start)
        if cursor is not None:
            params["end"] = str(cursor)
        result = await _get(http, "/v5/market/kline", params)
        batch = result.get("list", [])
        if not batch:
            break
        rows.extend(batch)
        remaining -= len(batch)
        oldest = int(batch[-1][0])
        if len(batch) < page or (start is not None and oldest <= start):
            break
        cursor = oldest - 1
    times, candles = [], []
    for ts, o, h, low, c, *_ in reversed(rows):
        times.append(int(ts))
        candles.append(chart.Candle(float(o), float(h), float(low), float(c)))
    return times, candles


def _bar_of(times: list[int], moment: int) -> int:
    """The bar whose slot holds `moment`, clamped to the fetched range."""
    fits = [i for i, ts in enumerate(times) if ts <= moment]
    return fits[-1] if fits else 0


async def entry_chart(http: httpx.AsyncClient, symbol: str, position: Position) -> bytes | None:
    """A PNG of recent candles with the entry, TP and SL drawn in. Best-effort:
    the text notice must go out even when the picture cannot be made.

    The position zones start at the newest bar — the entry — and stretch into
    the empty right-hand side, the way the TradingView tool draws an open
    trade.
    """
    try:
        _, candles = await _klines(http, symbol, CHART_INTERVAL, CHART_BARS)
        return chart.render(
            base_symbol(symbol),
            position.side,
            candles,
            position.price,
            position.take_profit,
            position.stop_loss,
            entry_index=len(candles) - 1,
            pad_right=max(CHART_BARS // 6, 4),
            timeframe=f"{CHART_INTERVAL}m",
        )
    # Deliberately broad: a chart is garnish, never worth losing the notice.
    except Exception:  # noqa: BLE001
        log.exception("no chart for %s", symbol)
        return None


async def close_chart(
    http: httpx.AsyncClient, symbol: str, was: Position, record: dict
) -> bytes | None:
    """A PNG of the finished trade: entry to exit, zone colored by outcome.

    The closed-pnl record carries no TP/SL, so those come from the last
    position snapshot and are drawn as lines. Every chart stays on
    CHART_INTERVAL, whatever the trade's length; Bybit caps one kline
    request at 1000 bars, so a trade longer than that shows its most
    recent stretch, with the entry clamped to the left edge.
    """
    try:
        opened, closed = int(record["createdTime"]), int(record["updatedTime"])
        span = int(CHART_INTERVAL) * 60_000
        trade_bars = (closed - opened) // span
        # Lead-in fills the frame up to CHART_BARS of context around the trade.
        lead = max(CHART_BARS - trade_bars - 3, 8)
        bars = min(trade_bars + lead + 4, 3000)  # plus the tail and a slack bar
        times, candles = await _klines(
            http,
            symbol,
            CHART_INTERVAL,
            bars,
            start=opened - lead * span,
            end=closed + 3 * span,
        )
        return chart.render(
            base_symbol(symbol),
            was.side,
            candles,
            float(record["avgEntryPrice"]),
            was.take_profit,
            was.stop_loss,
            entry_index=_bar_of(times, opened),
            exit_index=_bar_of(times, closed),
            exit_price=float(record["avgExitPrice"]),
            pad_right=2,
            timeframe=f"{CHART_INTERVAL}m",
        )
    # Deliberately broad: a chart is garnish, never worth losing the notice.
    except Exception:  # noqa: BLE001
        log.exception("no close chart for %s", symbol)
        return None


def trade_warnings(position: Position, depo: float | None) -> list[str]:
    """Money-management complaints about a position that just opened.

    Advisory only: the position is already open, so nothing is touched —
    the point is to hear about a rule broken while it can still be fixed.
    """
    if position.stop_loss is None:
        return ["⚠️ без стопа"]
    risk = abs(position.price - position.stop_loss) * position.size
    warns = []
    if depo and RISK_TARGET:
        actual = risk / depo
        # Within a quarter of the target nobody wants a ping.
        if abs(actual - RISK_TARGET) > RISK_TARGET * 0.25:
            warns.append(f"⚠️ риск {actual * 100:.2f}% депо, цель {RISK_TARGET * 100:g}%")
    if position.take_profit is not None and MIN_RR:
        rr = abs(position.take_profit - position.price) / abs(position.price - position.stop_loss)
        if rr < MIN_RR:
            warns.append(f"⚠️ RR {rr:.2f} < {MIN_RR:g}")
    return warns


async def equity(http: httpx.AsyncClient) -> float | None:
    """Account equity in USDT, best-effort: garnish for the PnL percent."""
    try:
        result = await _get(
            http,
            "/v5/account/wallet-balance",
            {"accountType": os.getenv("ACCOUNT_TYPE", "UNIFIED")},
        )
        rows = result.get("list") or []
        if not rows:
            return None
        value = rows[0].get("totalEquity") or rows[0].get("totalWalletBalance")
        return float(value) if value else None
    # Deliberately broad: no equity figure must never block the notice.
    except Exception:  # noqa: BLE001
        log.exception("no equity")
        return None


async def entry_fee(http: httpx.AsyncClient, symbol: str) -> float | None:
    """Fees paid on the fills that just opened the position, best-effort.

    The position list carries no fees; the execution log does, per fill. The
    watcher notices a position within one poll of its fill, so summing the
    fees of the last minute's executions covers the entry — a limit order
    that keeps filling later will simply show the fees paid so far.
    """
    try:
        result = await _get(
            http, "/v5/execution/list", {"category": "linear", "symbol": symbol, "limit": "50"}
        )
        cutoff = time.time() * 1000 - 60_000
        return sum(
            float(row.get("execFee") or 0)
            for row in result.get("list", [])
            if float(row.get("execTime") or 0) >= cutoff
        )
    # Deliberately broad: a fee figure is garnish on the entry notice.
    except Exception:  # noqa: BLE001
        log.exception("no entry fee for %s", symbol)
        return None


async def closed_record(http: httpx.AsyncClient, symbol: str) -> dict | None:
    """The most recently closed position's record, best-effort."""
    try:
        result = await _get(
            http, "/v5/position/closed-pnl", {"category": "linear", "symbol": symbol, "limit": "1"}
        )
        rows = result.get("list", [])
        return rows[0] if rows else None
    # Deliberately broad: the close notice must go out even without a figure.
    except Exception:  # noqa: BLE001
        log.exception("no closed pnl for %s", symbol)
        return None


def diff(
    before: dict[str, Position], after: dict[str, Position]
) -> list[tuple[str, str, Position | None, Position | None]]:
    """(kind, symbol, was, now) for every change between two snapshots."""
    changes: list[tuple[str, str, Position | None, Position | None]] = []
    for symbol, now in after.items():
        was = before.get(symbol)
        if was is None:
            changes.append(("opened", symbol, None, now))
        elif was.side != now.side:
            changes.append(("flipped", symbol, was, now))
        elif was.size != now.size:
            changes.append(("changed", symbol, was, now))
    changes.extend(
        ("closed", symbol, was, None) for symbol, was in before.items() if symbol not in after
    )
    return changes


def describe(kind: str, symbol: str, was: Position | None, now: Position | None) -> tuple[str, str]:
    """One change as (bot line, spoken line)."""
    sym = base_symbol(symbol)
    name = COIN_NAMES.get(sym, sym)
    if kind == "opened" and now is not None:
        return (
            f"💰 {sym} {now.side} {now.value:,.0f} USDT @ {now.price}",
            f"{name} {now.side} opened",
        )
    if kind == "flipped" and now is not None:
        return (
            f"💰 {sym} flipped to {now.side} {now.value:,.0f} USDT @ {now.price}",
            f"{name} flipped to {now.side}",
        )
    if kind == "changed" and was is not None and now is not None:
        word = "increased" if now.size > was.size else "reduced"
        return (
            f"💰 {sym} {now.side} {word} {was.value:,.0f} → {now.value:,.0f} USDT",
            f"{name} {now.side} {word}",
        )
    assert was is not None  # closed  # noqa: S101
    return f"💸 {sym} {was.side} closed", f"{name} {was.side} closed"


async def tick(
    http: httpx.AsyncClient, before: dict[str, Position] | None, send, speak, send_photo
) -> dict[str, Position]:
    """One poll: fetch, announce every change, hand back the new snapshot.

    The first call primes the snapshot silently — positions that were already
    open when the relay started are not news. An entry (or flip) goes out as
    a chart with the notice as its caption; everything else stays text.
    """
    after = await positions(http)
    if before is None:
        return after
    for kind, symbol, was, now in diff(before, after):
        line, spoken_line = describe(kind, symbol, was, now)
        png = None
        if kind in ("opened", "flipped") and now is not None:
            fee = await entry_fee(http, symbol)
            if fee:
                line += f" · fee {fee:.4g} USDT"
            if now.take_profit is not None:
                # What reaching the TP pays, net of both fees: the entry fee
                # just paid and a like-sized one for the exit.
                target = abs(now.take_profit - now.price) * now.size
                if fee:
                    target -= 2 * fee
                line += f" · на тейке ≈ {target:+,.2f} USDT"
            for warn in trade_warnings(now, await equity(http)):
                line += f"\n{warn}"
            png = await entry_chart(http, symbol, now)
        elif kind == "closed" and was is not None:
            record = await closed_record(http, symbol)
            if record is not None:
                # Bybit's closedPnl is already net of both fees.
                pnl = float(record["closedPnl"])
                line += f", PnL {pnl:+,.2f} USDT чистыми"
                depo = await equity(http)
                if depo:
                    line += f" ({pnl / depo * 100:+.2f}% депо)"
                opened_fee = float(record.get("openFee") or 0)
                closed_fee = float(record.get("closeFee") or 0)
                if opened_fee or closed_fee:
                    line += f" · fees {opened_fee:.4g} + {closed_fee:.4g} USDT"
                spoken_line += f", {'profit' if pnl >= 0 else 'loss'} {abs(pnl):.0f}"
                png = await close_chart(http, symbol, was, record)
        if png is None or not await send_photo(line, png):
            await send(line)
        await speak(spoken_line)
    return after


async def poll(http: httpx.AsyncClient, send, speak, send_photo) -> None:
    """Announce position changes until cancelled. Never lets one failure end it.

    `send` posts to the bot, `send_photo` posts a picture with a caption,
    `speak` voices a line; all injected, which keeps this module free of a
    cycle with relay.py and lets tests drive it dry.
    """
    log.info("watching bybit positions every %ss", POLL)
    snapshot: dict[str, Position] | None = None
    while True:
        try:
            snapshot = await tick(http, snapshot, send, speak, send_photo)
        except asyncio.CancelledError:
            raise
        # Deliberately broad: Bybit hiccups, rate limits, bad JSON — back off
        # and try again; the relay must outlive them all.
        except Exception:  # noqa: BLE001
            log.exception("bybit poll failed")
            await asyncio.sleep(max(POLL * 3, 30))
            continue
        await asyncio.sleep(POLL)
