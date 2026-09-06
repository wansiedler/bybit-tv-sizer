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


async def _post(http: httpx.AsyncClient, path: str, params: dict[str, object]) -> dict:
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
    unrealised: float = 0.0


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
            unrealised=float(row.get("unrealisedPnl") or 0),
        )
    return open_now


# /stopall arms on the first call and fires on the second within this window:
# commands in Telegram are tappable, and one stray tap must not flatten the
# whole book.
_STOPALL_WINDOW = 30.0
_stopall_armed = 0.0


async def close_everything(http: httpx.AsyncClient) -> str:
    """Close every open position at market, reduce-only. Two-step confirm."""
    global _stopall_armed
    if not enabled():
        return "Bybit не подключён: нет API-ключей"
    try:
        result = await _get(http, "/v5/position/list", {"category": "linear", "settleCoin": "USDT"})
        rows = [r for r in result.get("list", []) if float(r.get("size") or 0) != 0]
    # Deliberately broad: a chat command must answer, not crash the poller.
    except Exception:  # noqa: BLE001
        log.exception("stopall listing failed")
        return "Bybit не ответил, попробуй ещё раз"
    if not rows:
        _stopall_armed = 0.0
        return "Открытых позиций нет — закрывать нечего"

    names = ", ".join(base_symbol(r["symbol"]) for r in rows)
    now = time.monotonic()
    if now - _stopall_armed > _STOPALL_WINDOW:
        _stopall_armed = now
        return (
            f"⚠️ Закрою МАРКЕТОМ {len(rows)} поз.: {names}\n"
            f"Повтори /stopall в течение {_STOPALL_WINDOW:.0f} секунд для подтверждения."
        )

    _stopall_armed = 0.0
    lines = []
    for r in rows:
        symbol = r["symbol"]
        try:
            await _close_market(
                http, symbol, r.get("side", ""), r["size"], int(r.get("positionIdx") or 0)
            )
            lines.append(f"✅ {base_symbol(symbol)} закрывается")
        # Deliberately broad: one refused close must not strand the rest.
        except Exception as exc:  # noqa: BLE001
            log.exception("could not close %s", symbol)
            lines.append(f"❌ {base_symbol(symbol)}: {exc}")
    lines.append("Отчёты 💸 с PnL придут, как позиции закроются.")
    return "\n".join(lines)


async def _close_market(
    http: httpx.AsyncClient, symbol: str, bybit_side: str, qty: str, position_idx: int = 0
) -> None:
    """One reduce-only market order: it can only close, never open."""
    await _post(
        http,
        "/v5/order/create",
        {
            "category": "linear",
            "symbol": symbol,
            "side": "Sell" if bybit_side == "Buy" else "Buy",
            "orderType": "Market",
            "qty": qty,
            "reduceOnly": True,
            "positionIdx": position_idx,
        },
    )


async def close_position(http: httpx.AsyncClient, query: str) -> str:
    """Close one position by ticker, for /close CL. No confirm: the typed
    argument is the confirmation."""
    if not enabled():
        return "Bybit не подключён: нет API-ключей"
    if not query.strip():
        return "Какую позицию? Например: /close CL"
    wanted = query.strip().upper()
    try:
        result = await _get(http, "/v5/position/list", {"category": "linear", "settleCoin": "USDT"})
        rows = [r for r in result.get("list", []) if float(r.get("size") or 0) != 0]
    # Deliberately broad: a chat command must answer, not crash the poller.
    except Exception:  # noqa: BLE001
        log.exception("close listing failed")
        return "Bybit не ответил, попробуй ещё раз"
    matches = [r for r in rows if wanted in (r["symbol"].upper(), base_symbol(r["symbol"]).upper())]
    if not matches:
        names = ", ".join(base_symbol(r["symbol"]) for r in rows) or "—"
        return f"Позиции {wanted} нет. Открыты: {names}"
    row = matches[0]
    try:
        await _close_market(
            http, row["symbol"], row.get("side", ""), row["size"], int(row.get("positionIdx") or 0)
        )
    # Deliberately broad: the refusal text is the answer.
    except Exception as exc:  # noqa: BLE001
        log.exception("could not close %s", row["symbol"])
        return f"❌ {base_symbol(row['symbol'])}: {exc}"
    return f"✅ {base_symbol(row['symbol'])} закрывается — отчёт 💸 придёт следом"


# Fee assumed per fill for the "net" estimate: market orders pay taker.
TAKER_FEE = float(os.getenv("TAKER_FEE", "0.00055"))


async def positions_report(http: httpx.AsyncClient, send_album=None) -> str:
    """Every open position as one line, for the bot's /positions command.

    With `send_album` the charts go out as one media group whose first
    caption carries the whole report — a single Telegram message. The
    function then returns "" so the caller has nothing left to send.
    """
    if not enabled():
        return "Bybit не подключён: нет API-ключей"
    try:
        open_now = await positions(http)
    # Deliberately broad: a chat command must answer, not crash the poller.
    except Exception:  # noqa: BLE001
        log.exception("positions report failed")
        return "Bybit не ответил, попробуй ещё раз"
    if not open_now:
        return "Открытых позиций нет"
    depo = await equity(http)

    def share(amount: float) -> str:
        return f" ({amount / depo * 100:+.2f}%)" if depo else ""

    lines = []
    pngs = []
    total = 0.0
    total_net = 0.0
    for symbol, position in sorted(open_now.items()):
        arrow = "📈" if position.side == "long" else "📉"
        # uPnL is pure price difference; both fees at the taker rate come
        # off — the entry already paid, the exit still to come.
        fees = 2 * TAKER_FEE * position.value
        net = position.unrealised - fees
        head = f"{arrow}{base_symbol(symbol)} {position.value:,.0f}@{position.price:g}"
        if position.stop_loss:
            head += f" · sl {position.stop_loss:g}"
        detail = (
            f"PnL {position.unrealised:+,.2f} − комса {fees:.2f} = <b>{net:+,.2f}{share(net)}</b>"
        )
        if position.take_profit:
            sign = 1 if position.side == "long" else -1
            at_tp = sign * (position.take_profit - position.price) * position.size - fees
            detail += f", tp {position.take_profit:g}: <b>{at_tp:+,.2f}{share(at_tp)}</b>"
        total += position.unrealised
        total_net += net
        lines.append(f"{head}\n{detail}")
        if send_album is not None:
            png = await entry_chart(http, symbol, position, _plain(f"{head}\n{detail}"))
            if png is not None:
                pngs.append(png)
    # A total of one position would just repeat its line.
    if len(lines) > 1:
        lines.append(f"Σ PnL {total:+,.2f} = <b>{total_net:+,.2f}{share(total_net)}</b>")
    text = "\n".join(lines)
    # Telegram caps a media-group caption at 1024 characters.
    if send_album is not None and pngs and len(text) <= 1024 and await send_album(text, pngs):
        return ""
    return text


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


def _plain(text: str) -> tuple[str, ...]:
    """Markup-free lines of a notice, ready to be painted onto a chart."""
    return tuple(text.replace("<b>", "").replace("</b>", "").splitlines())


async def entry_chart(
    http: httpx.AsyncClient, symbol: str, position: Position, info: tuple[str, ...] = ()
) -> bytes | None:
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
            info=info,
        )
    # Deliberately broad: a chart is garnish, never worth losing the notice.
    except Exception:  # noqa: BLE001
        log.exception("no chart for %s", symbol)
        return None


async def close_chart(
    http: httpx.AsyncClient, symbol: str, was: Position, record: dict, info: tuple[str, ...] = ()
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
            info=info,
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


def _arrow(side: str) -> str:
    return "📈" if side == "long" else "📉"


def describe(kind: str, symbol: str, was: Position | None, now: Position | None) -> tuple[str, str]:
    """One change as (bot line, spoken line) — the same terse dialect as
    /positions: direction arrow, value@entry, no filler words."""
    sym = base_symbol(symbol)
    name = COIN_NAMES.get(sym, sym)
    if kind == "opened" and now is not None:
        head = f"💰{_arrow(now.side)}{sym} {now.value:,.0f}@{now.price:g}"
        if now.stop_loss:
            head += f" · sl {now.stop_loss:g}"
        return head, f"{name} {now.side} opened"
    if kind == "flipped" and now is not None:
        return (
            f"💰{sym} → {now.side} {now.value:,.0f}@{now.price:g}",
            f"{name} flipped to {now.side}",
        )
    if kind == "changed" and was is not None and now is not None:
        word = "increased" if now.size > was.size else "reduced"
        return (
            f"💰{_arrow(now.side)}{sym} {was.value:,.0f}→{now.value:,.0f}",
            f"{name} {now.side} {word}",
        )
    assert was is not None  # closed  # noqa: S101
    return f"💸{_arrow(was.side)}{sym}", f"{name} {was.side} closed"


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

    def share(amount: float, depo: float | None) -> str:
        return f" ({amount / depo * 100:+.2f}%)" if depo else ""

    for kind, symbol, was, now in diff(before, after):
        line, spoken_line = describe(kind, symbol, was, now)
        png = None
        if kind in ("opened", "flipped") and now is not None:
            fee = await entry_fee(http, symbol)
            depo = await equity(http)
            extras = []
            if fee:
                extras.append(f"комса {fee:.4g}")
            if now.take_profit is not None:
                # What reaching the TP pays, net of both fees: the entry fee
                # just paid and a like-sized one for the exit.
                target = abs(now.take_profit - now.price) * now.size
                if fee:
                    target -= 2 * fee
                extras.append(f"tp {now.take_profit:g}: <b>{target:+,.2f}{share(target, depo)}</b>")
            if extras:
                line += "\n" + ", ".join(extras)
            for warn in trade_warnings(now, depo):
                line += f"\n{warn}"
            png = await entry_chart(http, symbol, now, _plain(line))
        elif kind == "closed" and was is not None:
            record = await closed_record(http, symbol)
            if record is not None:
                # Bybit's closedPnl is already net of both fees.
                pnl = float(record["closedPnl"])
                depo = await equity(http)
                line += f": <b>{pnl:+,.2f}{share(pnl, depo)}</b>"
                opened_fee = float(record.get("openFee") or 0)
                closed_fee = float(record.get("closeFee") or 0)
                if opened_fee or closed_fee:
                    line += f" · комса {opened_fee:.4g}+{closed_fee:.4g}"
                spoken_line += f", {'profit' if pnl >= 0 else 'loss'} {abs(pnl):.0f}"
                png = await close_chart(http, symbol, was, record, _plain(line))
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
