"""Watch your own Bybit account and announce position changes through the bot.

Polls the private v5 REST API rather than the realtime WebSocket: httpx is
already a dependency, polling needs no reconnect logic, and a spoken "position
opened" does not care about a few seconds of latency.

    BB_A_K=...       # read-only key: Read permission, nothing else
    BB_A_S=...
    BYBIT_POLL=10           # seconds between polls

The watcher is dormant until both keys are present. It never places orders;
the key should not even be able to.
"""

import asyncio
import base64
import hashlib
import hmac
import html
import json
import logging
import math
import os
import time
from dataclasses import dataclass, replace
from decimal import Decimal

import httpx
from dotenv import load_dotenv

import chart
import sheets
import tv_shot
from coin_names import COIN_NAMES
from parser import base_symbol

log = logging.getLogger("relay.bybit")

# relay.py imports this module before its own load_dotenv, same as speaker.
load_dotenv("bipboop")

API_KEY = os.getenv("BB_A_K", "")
API_SECRET = os.getenv("BB_A_S", "")


def _api_url() -> str:
    """BYBIT_API_URL wins; otherwise BYBIT_TESTNET picks the public host."""
    override = os.getenv("BYBIT_API_URL", "")
    if override:
        return override
    if os.getenv("BYBIT_TESTNET", "0") == "1":
        return "https://api-testnet.bybit.com"
    return "https://api.bybit.com"


API_URL = _api_url()
POLL = float(os.getenv("BYBIT_POLL", "10"))
# Money-management checks on a freshly opened position: complain when the
# reward-to-risk is below MIN_RR (0 disables), or when the actual risk
# strays more than a quarter away from the RISK_PCT the sizer targets.
MIN_RR = float(os.getenv("MIN_RR", "2"))
RISK_TARGET = float(os.getenv("RISK_PCT", "0.5")) / 100
# Whether RISK_PCT is a target or a ceiling. Off, the sizer only ever cuts
# — the size drawn on the chart stands unless it risks too much — and being
# under the figure stops being worth a warning, because under a ceiling is
# where a trade is supposed to be.
RISK_GROW = os.getenv("RISK_GROW", "1") == "1"
# The RR that earns the full RISK_TARGET; the sizer scales a thinner order's
# risk down in proportion, and the fresh-position check expects the same.
RR_FULL_RISK = float(os.getenv("RR_FULL_RISK", "3"))
# A ceiling on the deposit the risk percent is taken from. With
# RISK_EQUITY_MAX=100000 a 300k wallet still risks one percent of 100k per
# trade, not of 300k — the position stops growing with the account. 0 = off.
RISK_EQUITY_MAX = float(os.getenv("RISK_EQUITY_MAX", "0"))
# The hard risk manager: positions breaking these rules get market-closed.
# A breach is announced first and enforced only if it survives the grace
# window — enough time to set a stop or drop the leverage after an entry.
RISK_GUARD = os.getenv("RISK_GUARD", "1") == "1"
# The trimmer: a position whose stop-distance risk overshoots the target
# is cut back down to it with a reduce-only market order — the market entry
# that filled worse than the sized limit gets reshaped. No tolerance: the
# only slack is the exchange's lot step, below which nothing can be cut.
RISK_TRIM = os.getenv("RISK_TRIM", "1") == "1"
# Maker takes: the position's market take-profit is traded for a reduce-only
# limit at the same price — a maker fill costs less than half the taker fee.
# The price can touch the level and bounce without filling a resting limit,
# so the cheaper exit is not guaranteed the way the market TP is.
TP_MAKER = os.getenv("TP_MAKER", "0") == "1"

# Scaling out instead of one take. The first rung stands where selling its
# share pays for the whole trade: fill it and a stop-out afterwards costs
# nothing. Its price fixes its share and the other way round — there is one
# choice here, not two, because selling at the entry itself would need the
# whole position. TP_FREE is that rung's distance in R; the share follows.
# TP_LADDER is where the rest sit, also in R, and the position's own take
# always ends the ladder. Empty TP_LADDER leaves the breakeven rung and the
# take; TP_FREE=0 drops the breakeven rung. Both need a stop to measure R
# against, and without one the exit stays the single limit it was.
TP_FREE = float(os.getenv("TP_FREE", "1"))
TP_LADDER = tuple(
    float(step) for step in os.getenv("TP_LADDER", "2,3,4").replace(" ", "").split(",") if step
)
# What the entry and the stop-out cost, for the breakeven share. The entry is
# a maker limit and the stop a market close, the same pair the sizer budgets.
MAKER_FEE = float(os.getenv("MAKER_FEE", "0.0002"))
GUARD_MAX_LEVERAGE = float(os.getenv("MAX_LEVERAGE", "1"))
GUARD_GRACE = float(os.getenv("GUARD_GRACE_SEC", "45"))

# Kline timeframe for every chart the relay draws, in minutes.
CHART_INTERVAL = os.getenv("CHART_INTERVAL", "15")
# How many bars of context a chart shows. Bybit caps one request at 1000
# bars, so anything above that is fetched in pages.
CHART_BARS = min(int(os.getenv("CHART_BARS", "1800")), 3000)
# Timeframes a CLOSE chart is drawn on, in minutes. The working one first,
# the slower one for context. They are pasted into a single wide picture, so
# Telegram shows them next to each other and the journal keeps its one
# screenshot column.
CLOSE_INTERVALS = [
    x.strip() for x in os.getenv("CLOSE_INTERVALS", f"{CHART_INTERVAL},240").split(",") if x.strip()
]
# How stale a signed request may be when Bybit reads it. Docker Desktop's
# clock stalls while the Mac sleeps and catches up afterwards: one evening
# the container ran ten seconds behind the exchange for thirteen minutes,
# and every signed call came back 10002 against the old 5000 ms. The window
# is the replay guard, so it is not free — twenty seconds buys surviving a
# nap, and the signature still binds the timestamp it was made for.
RECV_WINDOW = os.getenv("RECV_WINDOW", "20000")

_POSITIONS = "/v5/position/list"
_ORDERS = "/v5/order/realtime"
_NO_KEYS = "Bybit not connected: no API keys"
_NO_ANSWER = "Bybit did not answer, try again"


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
    leverage: float = 0.0
    position_idx: int = 0
    created_ms: int = 0
    # The take lives as our reduce-only limit order, not on the position.
    maker_exit: bool = False


async def positions(http: httpx.AsyncClient) -> dict[str, Position]:
    """Open USDT-perpetual positions, keyed by symbol. Zero sizes dropped.

    With TP_MAKER the take-profit may live as a reduce-only limit order
    instead of on the position; it is folded back into `take_profit` here so
    every report, chart and close label sees the take wherever it lives.
    """
    result = await _get(http, _POSITIONS, {"category": "linear", "settleCoin": "USDT"})
    open_now: dict[str, Position] = {}
    for row in result.get("list", []):
        if float(row.get("size") or 0) != 0:
            open_now[row["symbol"]] = _position_of(row)
    if TP_MAKER and any(p.take_profit is None for p in open_now.values()):
        await _fold_exits(http, open_now)
    return open_now


def _position_of(row: dict) -> Position:
    """One Bybit position row as the watcher's Position."""
    return Position(
        side="long" if row.get("side") == "Buy" else "short",
        size=float(row.get("size") or 0),
        price=float(row.get("avgPrice") or 0),
        value=float(row.get("positionValue") or 0),
        take_profit=float(row["takeProfit"]) if row.get("takeProfit") else None,
        stop_loss=float(row["stopLoss"]) if row.get("stopLoss") else None,
        unrealised=float(row.get("unrealisedPnl") or 0),
        leverage=float(row.get("leverage") or 0),
        position_idx=int(row.get("positionIdx") or 0),
        created_ms=int(row.get("createdTime") or 0),
    )


async def _fold_exits(http: httpx.AsyncClient, open_now: dict[str, Position]) -> None:
    """Give takes that live as maker exit orders back to their positions."""
    for symbol, orders in (await maker_exits(http)).items():
        position = open_now.get(symbol)
        if position is None or position.take_profit is not None:
            continue
        # The ladder's last rung is the take; the nearer ones are scale-outs.
        prices = [float(o["price"]) for o in orders]
        take = max(prices) if position.side == "long" else min(prices)
        open_now[symbol] = replace(position, take_profit=take, maker_exit=True)


async def maker_exits(http: httpx.AsyncClient) -> dict[str, list[dict]]:
    """Our reduce-only limit exits by symbol — the maker take-profits.

    Best-effort: an unreachable order list answers empty and the callers
    try again next poll.
    """
    try:
        result = await _get(http, _ORDERS, {"category": "linear", "settleCoin": "USDT"})
    # Deliberately broad: no order answer must not break the position poll.
    except Exception:  # noqa: BLE001
        log.exception("exit order listing failed")
        return {}
    exits: dict[str, list[dict]] = {}
    for r in result.get("list") or []:
        if (
            r.get("reduceOnly")
            and r.get("orderType") == "Limit"
            and not r.get("stopOrderType")
            and float(r.get("price") or 0) > 0
        ):
            exits.setdefault(r["symbol"], []).append(r)
    for orders in exits.values():
        orders.sort(key=lambda r: float(r["price"]))
    return exits


# /stopall arms on the first call and fires on the second within this window:
# commands in Telegram are tappable, and one stray tap must not flatten the
# whole book.
_STOPALL_WINDOW = 30.0
_stopall_armed = 0.0


async def close_everything(http: httpx.AsyncClient) -> str:
    """Close every open position at market, reduce-only. Two-step confirm."""
    global _stopall_armed
    if not enabled():
        return _NO_KEYS
    try:
        result = await _get(http, _POSITIONS, {"category": "linear", "settleCoin": "USDT"})
        rows = [r for r in result.get("list", []) if float(r.get("size") or 0) != 0]
    # Deliberately broad: a chat command must answer, not crash the poller.
    except Exception:  # noqa: BLE001
        log.exception("stopall listing failed")
        return _NO_ANSWER
    if not rows:
        _stopall_armed = 0.0
        return "No open positions — nothing to close"

    names = ", ".join(base_symbol(r["symbol"]) for r in rows)
    now = time.monotonic()
    if now - _stopall_armed > _STOPALL_WINDOW:
        _stopall_armed = now
        return (
            f"⚠️ Will close {len(rows)} pos. at MARKET: {names}\n"
            f"Repeat /stopall within {_STOPALL_WINDOW:.0f} seconds to confirm."
        )

    _stopall_armed = 0.0
    lines = []
    for r in rows:
        symbol = r["symbol"]
        try:
            await _close_market(
                http, symbol, r.get("side", ""), r["size"], int(r.get("positionIdx") or 0)
            )
            lines.append(f"✅ {base_symbol(symbol)} closing")
        # Deliberately broad: one refused close must not strand the rest.
        except Exception as exc:  # noqa: BLE001
            log.exception("could not close %s", symbol)
            lines.append(f"❌ {base_symbol(symbol)}: {exc}")
    lines.append("💸 reports with PnL will follow as the positions close.")
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


# When each still-open violation was first noticed, by symbol.
_guard_seen: dict[str, float] = {}
# Symbols the guard itself market-closed, with the rule they broke: the
# journal row carries the reason.
_guard_closed: dict[str, str] = {}


def _guard_violation(position: Position) -> str | None:
    """The rule a position breaks, or None. Leverage 0 means Bybit sent none."""
    if position.leverage > GUARD_MAX_LEVERAGE:
        return f"leverage {position.leverage:g}x > {GUARD_MAX_LEVERAGE:g}x"
    if position.stop_loss is None:
        return "no stop"
    return None


async def instrument_info(http: httpx.AsyncClient, symbol: str) -> dict:
    """The instrument row: lot rules, price rules and the rest."""
    result = await _get(
        http, "/v5/market/instruments-info", {"category": "linear", "symbol": symbol}
    )
    rows = result.get("list") or []
    return rows[0] if rows else {}


async def lot_filter(http: httpx.AsyncClient, symbol: str) -> dict:
    """The instrument's lot rules: step, minimum size, minimum notional."""
    return (await instrument_info(http, symbol)).get("lotSizeFilter") or {}


async def ladder_filters(http: httpx.AsyncClient, symbol: str) -> tuple[dict, float]:
    """The lot rules and the price tick, in one call rather than two."""
    row = await instrument_info(http, symbol)
    tick = float((row.get("priceFilter") or {}).get("tickSize") or 0)
    return row.get("lotSizeFilter") or {}, tick


async def _lot(http: httpx.AsyncClient, symbol: str) -> tuple[float, float]:
    """The instrument's quantity step and minimum order size."""
    lot = await lot_filter(http, symbol)
    return float(lot.get("qtyStep") or 0), float(lot.get("minOrderQty") or 0)


# When each symbol was last trimmed: Bybit needs a moment to show the new
# size, and re-cutting on every half-second poll would shred the position.
_trim_cooldown: dict[str, float] = {}


async def trim(http: httpx.AsyncClient, open_now: dict[str, Position], send, speak) -> None:
    """Cut an oversized position back to the target risk.

    The sizer shapes LIMIT orders; an entry thrown in at market keeps the
    quantity sized for a different price, so the stop suddenly risks more
    than RISK_PCT. This trims the position with a reduce-only market order
    until the stop distance costs the target again. It only ever shrinks.

    The stop's true cost includes every taker fee on the way: the market
    entry that oversized the position, the cut's own market close, and the
    stop's market close on what is kept — so the realised loss from entry
    to stop stays inside the target, not just the price distance.
    """
    if not RISK_TRIM:
        return
    depo = None
    for symbol, position in open_now.items():
        if not position.stop_loss or position.size <= 0:
            continue
        if time.monotonic() - _trim_cooldown.get(symbol, 0.0) < 10.0:
            continue
        if abs(position.price - position.stop_loss) <= 0:
            continue
        if depo is None:
            wallet = await wallet_balance(http)
            depo = risk_base(wallet) if wallet else wallet
        if not depo:
            return
        await _trim_cut(http, symbol, position, depo, send, speak)


async def _trim_cut(
    http: httpx.AsyncClient, symbol: str, position: Position, depo: float, send, speak
) -> None:
    """Cut one position whose stop-out overshoots the target, if it does."""
    # The caller has already ruled out a missing stop; `or 0` narrows the type.
    stop = position.stop_loss or 0.0
    distance = abs(position.price - stop)
    entry_fee = position.price * TAKER_FEE
    stop_fee = stop * TAKER_FEE
    per_unit = distance + entry_fee + stop_fee
    risk = per_unit * position.size
    target = depo * RISK_TARGET
    if risk <= target:
        return
    step, min_qty = await _lot(http, symbol)
    if step <= 0:
        return
    # The kept size answers for what is left of the budget once the sunk
    # fees are out: the entry fee was paid on every unit, and the cut
    # (size - want) pays its own taker fee at roughly the entry price.
    # target >= entry_fee*size + entry_fee*(size-want) + want*(distance+stop_fee)
    # solved for want:
    budget = target - 2 * position.size * entry_fee
    want = math.floor(budget / (distance + stop_fee - entry_fee) / step) * step
    cut = position.size - want
    if want < min_qty or cut < step:
        return
    _trim_cooldown[symbol] = time.monotonic()
    try:
        await _close_market(
            http,
            symbol,
            "Buy" if position.side == "long" else "Sell",
            _qty(cut),
            position.position_idx,
        )
        await send(
            f"✂️ {base_symbol(symbol)}: risk {risk / depo * 100:.2f}% depo vs target "
            f"{RISK_TARGET * 100:.2f}% — cutting {_qty(position.size)}→{_qty(want)}"
        )
        await speak(f"{COIN_NAMES.get(base_symbol(symbol), base_symbol(symbol))} trimmed")
        _trim_refused.pop(symbol, None)
    # Deliberately broad: one refused cut must not strand the rest.
    except Exception as exc:  # noqa: BLE001
        log.exception("trim failed for %s", symbol)
        if _trim_refused.get(symbol) != str(exc):
            _trim_refused[symbol] = str(exc)
            await send(f"❌ {base_symbol(symbol)}: could not trim ({exc})")


# symbol -> the refusal already announced for its trim, so one cut the
# exchange will not take is one message and not one every eleven seconds.
# GALA filed 10001 "Qty invalid" for as long as the position stood oversized.
_trim_refused: dict[str, str] = {}


# When each symbol's take was last touched: Bybit needs a moment to show a
# fresh order, and re-posting on every half-second poll would spam the book.
_tp_cooldown: dict[str, float] = {}
# symbol -> the refusal already announced for its maker exit, so one broken
# resize is one message and not one every pass.
_tp_refused: dict[str, str] = {}
# symbol -> (the position the ladder was last built for, what the exits then
# added up to). A rebuild that leaves the ladder exactly as short as it found
# it will do so again on the next pass and the one after — and a rung
# cancelled every eleven seconds cannot be dragged anywhere. Anything real —
# a fill, a trim, a dragged stop, a hand that cancels the exits — moves one
# of the two and lets the next rebuild through.
_ladder_built: dict[str, tuple[str, str]] = {}


def _ladder_state(position: Position, orders: list[dict]) -> tuple[str, str]:
    """What a rebuild would be answering: the position, and the exits it sees."""
    total = sum((Decimal(str(order.get("qty") or 0)) for order in orders), Decimal(0))
    levels = f"{position.size}|{position.price}|{position.stop_loss}|{position.take_profit}"
    return levels, str(total)


async def tp_maker(http: httpx.AsyncClient, open_now: dict[str, Position], send, speak) -> None:
    """Trade the position's market take-profit for a maker limit exit.

    Bybit's position TP fires a market order — a taker fee. A reduce-only
    limit resting at the same price fills as a maker for less than half of
    that. Only the take is converted: a limit stop can miss on a wick, and
    a missed stop has no ceiling, while a missed take is a smaller win.

    The limit is placed before the market TP comes off, so a refusal in
    between leaves the position covered by the original take. When the
    trimmer shrinks the position the exit is resized with it, and an exit
    orphaned by a stop-out is cancelled.
    """
    if not TP_MAKER:
        return
    exits = await maker_exits(http)
    await _cancel_orphan_exits(http, exits, open_now, send)
    for symbol, position in open_now.items():
        if position.take_profit is None:
            continue
        if time.monotonic() - _tp_cooldown.get(symbol, 0.0) < 10.0:
            continue
        exit_orders: list[dict] = exits.get(symbol) or []
        if not position.maker_exit:
            _tp_cooldown[symbol] = time.monotonic()
            await _repost_take(http, symbol, position, exit_orders, send, speak)
            continue
        if not exit_orders:
            continue
        lot, _ = await ladder_filters(http, symbol)
        step = float(lot.get("qtyStep") or 0)
        if ladder_covers(position, exit_orders, step):
            continue
        # The snapshot this pass works from was read before the exits were:
        # guard() and trim() each talk to the exchange in between. A rung
        # that fills in that gap leaves the exits short by exactly what
        # filled, and the ladder looks broken when it is not. One GALA
        # position was closed in full on that reading — the rebuild
        # cancelled two rungs that covered the remainder exactly.
        #
        # Nothing is rebuilt until the size the exits are measured against
        # is the size the exchange has now. A real trim only waits a poll.
        if await _position_size(http, symbol) != position.size:
            continue
        # Rebuilding into the same shortfall twice is a loop, not a repair.
        state = _ladder_state(position, exit_orders)
        if _ladder_built.get(symbol) == state:
            continue
        _ladder_built[symbol] = state
        _tp_cooldown[symbol] = time.monotonic()
        await _rebuild_ladder(http, symbol, position, exit_orders, send)


async def _cancel_orphan_exits(
    http: httpx.AsyncClient,
    exits: dict[str, list[dict]],
    open_now: dict[str, Position],
    send,
) -> None:
    """Cancel maker exits whose position is gone — a stop-out leaves one."""
    for symbol, orders in exits.items():
        if symbol in open_now or time.monotonic() - _tp_cooldown.get(symbol, 0.0) < 10.0:
            continue
        _tp_cooldown[symbol] = time.monotonic()
        _ladder_built.pop(symbol, None)
        try:
            await _cancel_rungs(http, symbol, orders)
            await send(f"🎯 {base_symbol(symbol)}: take limit left without a position — cancelled")
        # Deliberately broad: one refused cancel must not strand the rest.
        except Exception:  # noqa: BLE001
            log.exception("orphan exit cancel failed for %s", symbol)


def breakeven_share(position: Position, price: float) -> float:
    """Fraction of the position whose sale at `price` pays for a stop-out.

    Sell it and the rest is free: if the stop then fires, the trade is
    square. Both fees are in — the maker entry on the whole position, the
    maker exit on the slice, the taker stop on what is left.

        f = (R + S·taker + E·maker) / (|P − S| − P·maker + S·taker)

    Nearer the entry means a larger slice, and at the entry it would be the
    whole position. That is why the rung's distance is the setting and its
    share is derived, never both.

    0.0 when there is nothing to measure against or the answer is not a
    fraction of a position.
    """
    stop = position.stop_loss
    if not stop or not price or position.price <= 0:
        return 0.0
    risk = abs(position.price - stop)
    reach = abs(price - stop)
    if risk <= 0 or reach <= 0:
        return 0.0
    owed = risk + stop * TAKER_FEE + position.price * MAKER_FEE
    paid = reach - price * MAKER_FEE + stop * TAKER_FEE
    share = owed / paid if paid > 0 else 0.0
    return share if 0.0 < share < 1.0 else 0.0


def _on_tick(price: float, tick: float, up: bool) -> float:
    """A price the exchange can actually hold, so ours can be compared to it.

    Bybit snaps a submitted price to the instrument's tick and keeps the
    snapped one: 89.7622 on a tick of 0.01 came back 89.77. A ladder
    compared against what it asked for never matches what it got, and one
    HYPE position rebuilt itself every pass for as long as it was open.

    Away from the position, always — a long's rungs sell, so they round up,
    and a short's buy, so they round down. That is the direction Bybit was
    seen to take, and the one that cannot make the exit worse. Rounding here
    rather than at the exchange also means the breakeven share is derived
    from the price the order will really stand at.
    """
    if tick <= 0:
        return price
    step = Decimal(str(tick))
    rounding = "ROUND_CEILING" if up else "ROUND_FLOOR"
    return float((Decimal(str(price)) / step).to_integral_value(rounding=rounding) * step)


def _rung_price(position: Position, multiple: float) -> float:
    """Where `multiple` R from the entry sits, in the trade's direction."""
    risk = abs(position.price - (position.stop_loss or 0.0))
    step = risk * multiple
    return position.price + step if position.side == "long" else position.price - step


def ladder_rungs(position: Position, lot: dict, tick: float = 0.0) -> list[tuple[float, float]]:
    """(price, quantity) per rung, nearest first; [] to leave the one limit.

    The breakeven rung takes the share that pays for the trade, the rest
    split what remains evenly, and the position's own take ends the ladder
    carrying whatever rounding left over — so four rungs and a remainder
    never strand two coins above the take.

    A rung the exchange would refuse is dropped rather than sent: its share
    goes to the take, which is where it would have ended anyway.
    """
    if not TP_MAKER or not position.stop_loss or position.take_profit is None:
        return []
    step = float(lot.get("qtyStep") or 0)
    least = float(lot.get("minOrderQty") or 0)
    floor_value = float(lot.get("minNotionalValue") or 0)
    if step <= 0:
        return []
    up = position.side == "long"
    prices = [_on_tick(_rung_price(position, m), tick, up) for m in TP_LADDER if m > 0]
    if TP_FREE > 0:
        prices.insert(0, _on_tick(_rung_price(position, TP_FREE), tick, up))
    free_share = breakeven_share(position, prices[0]) if TP_FREE > 0 and prices else 0.0
    shares = [free_share] if free_share else []
    rest = [p for p in prices[len(shares) :] if _beyond(position, p)]
    if rest:
        shares += [(1.0 - free_share) / (len(rest) + 1)] * len(rest)
    rungs = []
    placed = 0.0
    for price, share in zip(prices[: len(shares)], shares, strict=True):
        qty = _floor_step(position.size * share, step)
        if qty < least or qty * price < floor_value:
            continue
        rungs.append((price, qty))
        placed += qty
    tail = _floor_step(position.size - placed, step)
    if tail >= least and tail * position.take_profit >= floor_value:
        rungs.append((position.take_profit, tail))
    return rungs


# How close to the take a rung may stand before it stops being its own
# step, in R. A ZEC trade whose take sat at 3.19R put the 4R rung at 1399.12
# against a take of 1403.65 — half a percent apart, two orders doing one
# order's work. Nearer than this and the rung is dropped; its share goes to
# the take, which is where it was headed.
_RUNG_GAP = 0.5


def _beyond(position: Position, price: float) -> bool:
    """Whether a rung stands far enough short of the take to be worth one."""
    if position.take_profit is None or not position.stop_loss:
        return False
    gap = abs(position.price - position.stop_loss) * _RUNG_GAP
    if position.side == "long":
        return price < position.take_profit - gap
    return price > position.take_profit + gap


def _floor_step(qty: float, step: float) -> float:
    """Down to the lot step, without the float dust `//` leaves behind."""
    lot = Decimal(str(step))
    return float((Decimal(str(qty)) / lot).to_integral_value(rounding="ROUND_DOWN") * lot)


async def _place_rung(
    http: httpx.AsyncClient, symbol: str, position: Position, price: float, qty: float
) -> None:
    """One reduce-only limit of the ladder."""
    await _post(
        http,
        "/v5/order/create",
        {
            "category": "linear",
            "symbol": symbol,
            "side": "Sell" if position.side == "long" else "Buy",
            "orderType": "Limit",
            "qty": _qty(qty),
            "price": _qty(price),
            "reduceOnly": True,
            # A rung below a long's market price is not a resting exit: it
            # crosses the book and fills at once, as a taker, which is the
            # opposite of what a maker exit is for. One GALA rebuild posted
            # the whole position at a take the price had passed an hour
            # earlier and closed the trade at market. PostOnly makes the
            # exchange refuse that order instead of executing it.
            "timeInForce": "PostOnly",
            "positionIdx": position.position_idx,
        },
    )


async def _cancel_rungs(http: httpx.AsyncClient, symbol: str, orders: list[dict]) -> None:
    """Take the ladder down, so it can be built again from the new size."""
    for order in orders:
        await _post(
            http,
            "/v5/order/cancel",
            {"category": "linear", "symbol": symbol, "orderId": order["orderId"]},
        )


def _ladder_note(symbol: str, rungs: list[tuple[float, float]], size: float) -> str:
    """The ladder as one line: where each rung stands and what share it takes."""
    steps = " · ".join(f"{price:g}×{qty / size * 100:.0f}%" for price, qty in rungs)
    return f"🎯 {base_symbol(symbol)}: {steps}"


async def _repost_take(
    http: httpx.AsyncClient,
    symbol: str,
    position: Position,
    exit_orders: list[dict],
    send,
    speak,
) -> None:
    """Stand the ladder, then drop the market TP.

    In that order: a refusal in between leaves the position covered by the
    original take. Without a stop there is no R to measure rungs against, so
    the exit stays the single limit it has always been.
    """
    lot, tick = await ladder_filters(http, symbol)
    rungs = ladder_rungs(position, lot, tick)
    if not rungs:
        rungs = [(position.take_profit or 0.0, position.size)]
    try:
        if not exit_orders:
            for price, qty in rungs:
                await _place_rung(http, symbol, position, price, qty)
        await _post(
            http,
            "/v5/position/trading-stop",
            {
                "category": "linear",
                "symbol": symbol,
                "takeProfit": "0",
                "positionIdx": position.position_idx,
            },
        )
        await send(_ladder_note(symbol, rungs, position.size) + " (maker)")
        await speak(f"{COIN_NAMES.get(base_symbol(symbol), base_symbol(symbol))} take reposted")
    # Deliberately broad: one refused repost must not strand the rest.
    except Exception as exc:  # noqa: BLE001
        log.exception("tp-maker repost failed for %s", symbol)
        await send(f"❌ {base_symbol(symbol)}: could not repost the take ({exc})")


def _qty(size: float) -> str:
    """A quantity the exchange will read back as the number we meant.

    `%g` keeps six significant digits. That is exact for a 36.84 ETH
    position and lossy for a 149314.2 ARB one: the tenth is dropped, the
    amend asks for the size the order already carries, and Bybit answers
    10001 "order not modified" — once every ten seconds for as long as the
    position lives, because the size it was compared against never changed.
    A market close formatted this way would leave the same tenth behind.
    """
    return f"{size:.8f}".rstrip("0").rstrip(".") or "0"


async def _position_size(http: httpx.AsyncClient, symbol: str) -> float | None:
    """The symbol's size as the exchange has it now; None when unreadable.

    0.0 for a symbol with no position left. Unknown is not zero: a caller
    about to cancel live orders must do nothing rather than act on a guess.
    """
    try:
        result = await _get(http, _POSITIONS, {"category": "linear", "symbol": symbol})
    # Deliberately broad: an unanswered read means "don't touch anything".
    except Exception:  # noqa: BLE001
        log.exception("could not re-read the position on %s", symbol)
        return None
    for row in result.get("list") or []:
        if row.get("symbol") == symbol:
            return float(row.get("size") or 0)
    return 0.0


async def _rebuild_ladder(
    http: httpx.AsyncClient, symbol: str, position: Position, exit_orders: list[dict], send
) -> None:
    """Take the ladder down and build it again from the size that is left.

    A trim invalidates every rung at once — their shares were cut from the
    old size — so resizing one of them is not a smaller version of this. A
    rung that already filled is simply not there to cancel.
    """
    lot, tick = await ladder_filters(http, symbol)
    rungs = ladder_rungs(position, lot, tick)
    if not rungs:
        # No take is nothing to fall back to. A limit at price 0 is not an
        # exit, and asking for one only earns a refusal.
        if not position.take_profit:
            return
        rungs = [(position.take_profit, position.size)]
    try:
        await _cancel_rungs(http, symbol, exit_orders)
    # Deliberately broad: a ladder that would not come down stays up, and
    # the position keeps the exits it already had.
    except Exception as exc:  # noqa: BLE001
        log.exception("tp-maker rebuild failed for %s", symbol)
        await _rebuild_refused(symbol, exc, False, send)
        return
    # From here the old exits are gone, so every rung is placed on its own:
    # aborting the loop on the first refusal left the position with nothing
    # covering it at all.
    stood: list[tuple[float, float]] = []
    refusal = ""
    for price, qty in rungs:
        try:
            await _place_rung(http, symbol, position, price, qty)
            stood.append((price, qty))
        # Deliberately broad: one rung the exchange will not hold must not
        # cost the others. PostOnly makes this an ordinary answer.
        except Exception as exc:  # noqa: BLE001
            log.exception("tp-maker rung at %s refused on %s", price, symbol)
            refusal = str(exc)
    if not stood:
        await _rebuild_refused(symbol, refusal, True, send)
        return
    _tp_refused.pop(symbol, None)
    note = _ladder_note(symbol, stood, position.size) + " (rebuilt)"
    if refusal:
        note += f"\n⚠️ one rung the exchange would not hold: {refusal}"
    await send(note)


async def _rebuild_refused(symbol: str, reason: object, bare: bool, send) -> None:
    """Report a refused rebuild once per distinct reason.

    `bare` means the old exits are already cancelled and none of the new
    ones stood, so the position is uncovered. That is the part worth saying
    out loud: everything else is the exchange being briefly unhelpful.
    """
    text = str(reason)
    if _tp_refused.get(symbol) == text:
        return
    _tp_refused[symbol] = text
    line = f"❌ {base_symbol(symbol)}: could not rebuild the ladder ({text})"
    if bare:
        line += " — the position has no maker exits now"
    await send(line)


def ladder_covers(position: Position, orders: list[dict], step: float) -> bool:
    """Whether the standing exits still add up to the whole position.

    The ladder exists to cover the position; where each rung stands is the
    bot's suggestion, not its decision. Comparing prices too meant a rung
    dragged by hand was put back within half a second, which made the
    ladder impossible to adjust. Only the arithmetic is defended now: a
    trim or a fill leaves the exits short of the position and the ladder is
    rebuilt, while a rung moved to a better price is left exactly where it
    was put.

    Cancel the exits to get the computed prices back.
    """
    # In decimal, not in float. Exits of 0.139 against a position of 0.14
    # are one lot step apart — the slack this function exists to grant —
    # but in binary the gap is 0.0010000000000000009, which is larger than
    # 0.001 by 8.7e-19. On that difference one BTC ladder was torn down and
    # rebuilt every eleven seconds, so no rung could be dragged anywhere.
    total = sum((Decimal(str(order.get("qty") or 0)) for order in orders), Decimal(0))
    return abs(total - Decimal(str(position.size))) <= max(Decimal(str(step)), Decimal(0))


async def guard(http: httpx.AsyncClient, open_now: dict[str, Position], send, speak) -> None:
    """The risk manager: warn about a rule breach, then enforce it.

    A breach gets one warning and the grace window to fix itself (set the
    stop, drop the leverage). Afterwards a stopless position is closed with
    a reduce-only market order, and a stopless entry order is cancelled.
    """
    if not RISK_GUARD:
        return
    naked = await _naked_orders(http)
    _forget_fixed(naked, open_now)
    await _cancel_naked(http, naked, send, speak)
    await _enforce_rules(http, open_now, send, speak)


async def _naked_orders(http: httpx.AsyncClient) -> dict[str, dict]:
    """Entry orders with no stop attached, keyed for the guard's memory.

    Exits are left alone — reduce-only and conditional (stop/take) orders
    carry no stopLoss by nature.
    """
    naked: dict[str, dict] = {}
    try:
        result = await _get(http, _ORDERS, {"category": "linear", "settleCoin": "USDT"})
        for r in result.get("list") or []:
            if r.get("reduceOnly") or r.get("stopOrderType") or r.get("closeOnTrigger"):
                continue
            if r.get("stopLoss"):
                continue
            naked[f"order:{r['orderId']}"] = r
    # Deliberately broad: no order answer must not stop the position rules.
    except Exception:  # noqa: BLE001
        log.exception("guard order listing failed")
    return naked


def _forget_fixed(naked: dict[str, dict], open_now: dict[str, Position]) -> None:
    """Drop guard memory for breaches that healed themselves."""
    # A list copy on purpose: the dict shrinks inside the loop.
    for key in list(_guard_seen):  # NOSONAR
        if key.startswith("order:"):
            if key not in naked:
                del _guard_seen[key]
        else:
            fixed = open_now.get(key)
            if fixed is None or _guard_violation(fixed) is None:
                del _guard_seen[key]


async def _grace_holds(key: str, warning: str, spoken: str, send, speak) -> bool:
    """Whether enforcement waits: warn on first sight, hold inside the window.

    A refused action retries after a full window (half a minute when the
    grace is zero) instead of hammering the API every poll.
    """
    now = time.monotonic()
    first = _guard_seen.get(key)
    if first is None:
        _guard_seen[key] = now
        if GUARD_GRACE > 0:
            await send(warning)
            await speak(spoken)
            return True
    elif now - first < (GUARD_GRACE or 30.0):
        return True
    else:
        _guard_seen[key] = now
    return False


async def _cancel_naked(http: httpx.AsyncClient, naked: dict[str, dict], send, speak) -> None:
    """Cancel entry orders that still have no stop once the grace runs out."""
    for key, order in naked.items():
        symbol = order["symbol"]
        if await _grace_holds(
            key,
            f"🛑 {base_symbol(symbol)}: limit order without a stop — cancelling in {GUARD_GRACE:.0f}s",
            f"{COIN_NAMES.get(base_symbol(symbol), base_symbol(symbol))} naked order",
            send,
            speak,
        ):
            continue
        try:
            await _post(
                http,
                "/v5/order/cancel",
                {"category": "linear", "symbol": symbol, "orderId": order["orderId"]},
            )
            await send(
                f"🛑 {base_symbol(symbol)}: limit order without a stop cancelled by the risk manager"
            )
        # Deliberately broad: one refused cancel must not strand the rest.
        except Exception as exc:  # noqa: BLE001
            log.exception("guard cancel failed for %s", symbol)
            await send(f"❌ {base_symbol(symbol)}: could not cancel the limit order ({exc})")


async def _enforce_rules(
    http: httpx.AsyncClient, open_now: dict[str, Position], send, speak
) -> None:
    """Market-close positions that still break a rule once the grace runs out."""
    for symbol, position in open_now.items():
        reason = _guard_violation(position)
        if reason is None:
            continue
        if await _grace_holds(
            symbol,
            f"🛑 {base_symbol(symbol)}: {reason} — closing at market in {GUARD_GRACE:.0f}s",
            f"{COIN_NAMES.get(base_symbol(symbol), base_symbol(symbol))} risk breach",
            send,
            speak,
        ):
            continue
        try:
            await _close_market(
                http,
                symbol,
                "Buy" if position.side == "long" else "Sell",
                _qty(position.size),
                position.position_idx,
            )
            _guard_closed[symbol] = reason
            note = ""
            if reason.startswith("leverage") and await _cap_leverage(http, symbol) == "done":
                note = " · leverage reset to 1x"
            await send(
                f"🛑 {base_symbol(symbol)} closed at market by the risk manager: {reason}{note}"
            )
        # Deliberately broad: the guard must keep watching even when one
        # close is refused (margin mode quirks, min qty, hedged legs).
        except Exception as exc:  # noqa: BLE001
            log.exception("guard close failed for %s", symbol)
            await send(f"❌ {base_symbol(symbol)}: the risk manager could not close ({exc})")


async def price_before(http: httpx.AsyncClient, ticker: str) -> float | None:
    """Where the market was a moment before the alert's cross.

    An alert level is crossed FROM somewhere; the price after the cross has
    often bounced right back over the line, so the arrow must come from the
    side the market arrived from, not where it sits now. Crypto answers from
    Bybit's last completed minute; a ticker Bybit does not list (a MOEX share
    like GAZP) falls back to the Moscow Exchange's public ISS API.
    """
    symbol = ticker if ticker.endswith("USDT") else f"{ticker}USDT"
    try:
        result = await _get(
            http,
            "/v5/market/kline",
            {"category": "linear", "symbol": symbol, "interval": "1", "limit": "2"},
        )
        rows = result.get("list") or []
        # Newest first; rows[1] is the last completed minute, close at index 4.
        if len(rows) > 1:
            return float(rows[1][4])
    # Deliberately broad: an unlisted symbol is Bybit refusing, not a crash.
    except Exception:  # noqa: BLE001
        log.info("no bybit price for %s, asking MOEX", symbol)
    return await _moex_last(http, ticker.removesuffix("USDT"))


async def _moex_last(http: httpx.AsyncClient, ticker: str) -> float | None:
    """The last trade on the Moscow Exchange's main share board, best-effort.

    The unauthenticated ISS feed runs ~15 minutes behind — old enough to sit
    on the arriving side of a cross that just fired, which is exactly what
    the arrow wants.
    """
    try:
        response = await http.get(
            "https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/"
            f"securities/{ticker}.json"
            "?iss.meta=off&iss.only=marketdata&marketdata.columns=LAST",
            timeout=10,
        )
        data = response.json().get("marketdata", {}).get("data") or []
        last = data[0][0] if data and data[0] else None
        return float(last) if last is not None else None
    # Deliberately broad: no MOEX answer just means no arrow.
    except Exception:  # noqa: BLE001
        log.exception("no moex price for %s", ticker)
        return None


async def _active_symbols(http: httpx.AsyncClient) -> set[str]:
    """Symbols with an open position or a live order."""
    symbols: set[str] = set()
    result = await _get(http, _POSITIONS, {"category": "linear", "settleCoin": "USDT"})
    symbols |= {r["symbol"] for r in result.get("list", []) if float(r.get("size") or 0) != 0}
    result = await _get(http, _ORDERS, {"category": "linear", "settleCoin": "USDT"})
    symbols |= {r["symbol"] for r in result.get("list", [])}
    return symbols


async def _all_symbols(http: httpx.AsyncClient) -> set[str]:
    """Every trading USDT perpetual on the exchange."""
    symbols: set[str] = set()
    cursor = ""
    while True:
        params = {"category": "linear", "limit": "1000"}
        if cursor:
            params["cursor"] = cursor
        result = await _get(http, "/v5/market/instruments-info", params)
        symbols |= {
            r["symbol"]
            for r in result.get("list", [])
            if r.get("settleCoin") == "USDT" and r.get("status") == "Trading"
        }
        cursor = result.get("nextPageCursor") or ""
        if not cursor:
            return symbols


async def _cap_leverage(http: httpx.AsyncClient, symbol: str) -> str:
    """One set-leverage call: 'done', 'already' or the refusal text."""
    try:
        await _post(
            http,
            "/v5/position/set-leverage",
            {"category": "linear", "symbol": symbol, "buyLeverage": "1", "sellLeverage": "1"},
        )
        return "done"
    # Deliberately broad: one refused symbol must not strand the rest.
    except Exception as exc:  # noqa: BLE001
        # 110043: leverage not modified — it already stands at 1x.
        if "110043" in str(exc):
            return "already"
        log.exception("could not set leverage on %s", symbol)
        return str(exc)


async def force_leverage_one(http: httpx.AsyncClient, query: str = "") -> str:
    """Force 1x leverage, for /lev1 [ticker].

    With a ticker — that instrument alone (CL → CLUSDT). Without one the
    symbols with open positions and live orders go first, then every other
    USDT perpetual on the exchange, so a fresh instrument is already capped
    before the first order ever touches it.
    """
    if not enabled():
        return _NO_KEYS
    query = query.strip()
    if query and query.lower() != "all":
        wanted = query.upper()
        active = [wanted if wanted.endswith("USDT") else f"{wanted}USDT"]
        rest: list[str] = []
    else:
        try:
            open_now = await _active_symbols(http)
            active = sorted(open_now)
            rest = sorted(await _all_symbols(http) - open_now)
        # Deliberately broad: a chat command must answer, not crash the poller.
        except Exception:  # noqa: BLE001
            log.exception("lev1 listing failed")
            return _NO_ANSWER
    lines = await _cap_actives(http, active)
    if not active and not rest:
        return "No open positions or orders — nothing to set leverage on"
    if rest:
        lines.append(await _cap_the_rest(http, rest))
    return "\n".join(lines)


async def _cap_actives(http: httpx.AsyncClient, active: list[str]) -> list[str]:
    """A verdict line per named instrument."""
    lines = []
    for symbol in active:
        verdict = await _cap_leverage(http, symbol)
        if verdict == "done":
            lines.append(f"✅ {base_symbol(symbol)} → 1x")
        elif verdict == "already":
            lines.append(f"· {base_symbol(symbol)} already 1x")
        else:
            lines.append(f"❌ {base_symbol(symbol)}: {verdict}")
    return lines


async def _cap_the_rest(http: httpx.AsyncClient, rest: list[str]) -> str:
    """The whole-exchange sweep, condensed to counters."""
    done = already = failed = 0
    for symbol in rest:
        verdict = await _cap_leverage(http, symbol)
        if verdict == "done":
            done += 1
        elif verdict == "already":
            already += 1
        else:
            failed += 1
    return f"Other {len(rest)} instruments: ✅ {done} · already 1x {already} · ❌ {failed}"


async def _open_position_row(http: httpx.AsyncClient, query: str) -> tuple[dict | None, str]:
    """One live position row by ticker, or the message saying why there is none.

    The listing is fresh on every call: a ticker typed minutes ago may name
    a position since stopped out, trimmed or flipped.
    """
    wanted = query.strip().upper()
    try:
        result = await _get(http, _POSITIONS, {"category": "linear", "settleCoin": "USDT"})
        rows = [r for r in result.get("list", []) if float(r.get("size") or 0) != 0]
    # Deliberately broad: a chat command must answer, not crash the poller.
    except Exception:  # noqa: BLE001
        log.exception("position listing failed")
        return None, _NO_ANSWER
    matches = [r for r in rows if wanted in (r["symbol"].upper(), base_symbol(r["symbol"]).upper())]
    if not matches:
        names = ", ".join(base_symbol(r["symbol"]) for r in rows) or "—"
        return None, f"No {wanted} position. Open: {names}"
    return matches[0], ""


async def close_position(http: httpx.AsyncClient, query: str) -> str:
    """Close one position by ticker, for /close CL. No confirm: the typed
    argument is the confirmation."""
    if not enabled():
        return _NO_KEYS
    if not query.strip():
        return "Which position? e.g. /close CL"
    row, refusal = await _open_position_row(http, query)
    if row is None:
        return refusal
    try:
        await _close_market(
            http, row["symbol"], row.get("side", ""), row["size"], int(row.get("positionIdx") or 0)
        )
    # Deliberately broad: the refusal text is the answer.
    except Exception as exc:  # noqa: BLE001
        log.exception("could not close %s", row["symbol"])
        return f"❌ {base_symbol(row['symbol'])}: {exc}"
    return f"✅ {base_symbol(row['symbol'])} closing — 💸 report follows"


async def cut_position(http: httpx.AsyncClient, query: str) -> str:
    """Close part of one position by ticker, for /cut CL 25.

    The buttons only exist under an entry notice, and a notice scrolls away;
    this is the same partial exit reachable at any time. The ticker is
    matched exactly as /close matches it, then the share is handed to the
    very function a tap calls.
    """
    if not enabled():
        return _NO_KEYS
    ticker, _, share = query.strip().rpartition(" ")
    try:
        percent = float(share.removesuffix("%").replace(",", "."))
    except ValueError:
        percent = 0.0
    if not ticker or not 0 < percent <= 100:
        return "Cut what, and how much? e.g. /cut CL 25"
    row, refusal = await _open_position_row(http, ticker)
    if row is None:
        return refusal
    return await close_fraction(http, row["symbol"], percent)


# The buttons that ride under an entry notice: one tap closes that share of
# the position at market. 100% is a full exit, so it sits last and is marked.
EXIT_SHARES = (5, 25, 50, 100)
_TAP = "x"


# A tenth of a winning trade, set aside for the mentor. The button only
# offers the figure and writes it down — nothing here moves money, and the
# Bybit key stays without withdrawal rights. 0 takes the button away.
TITHE_PCT = float(os.getenv("TITHE_PCT", "10"))
_TITHE = "t"


def tithe_amount(pnl: float) -> float:
    """What a winning trade owes, rounded to the cent."""
    return round(pnl * TITHE_PCT / 100, 2)


def tithe_buttons(pnl: float) -> dict | None:
    """The one-button keyboard under a profitable close; None when there is
    nothing to tithe — a loss, or the feature switched off."""
    if TITHE_PCT <= 0 or pnl <= 0:
        return None
    owed = tithe_amount(pnl)
    if owed <= 0:
        return None
    return {
        "inline_keyboard": [
            [{"text": f"🙏 десятина {owed:,.2f}$", "callback_data": f"{_TITHE}:{owed}"}]
        ]
    }


async def log_tithe(http: httpx.AsyncClient, amount: float) -> str:
    """Write one tithe to the journal and say what was written.

    Its own word in the kind column, not `вывод`: the money has not left
    Bybit, and a row that claimed it had would put the journal out of step
    with the exchange and make the month's tithes impossible to pick out.
    """
    from datetime import datetime

    depo = await equity(http)
    row = [
        datetime.now().strftime("%d.%m.%y"),
        "десятина",
        # The module settles everything in USDT; the money column follows it.
        "USDT",
        *([""] * 10),
        round(depo, 2) if depo else "",
        -abs(round(amount, 2)),
        "",  # P: Закрытие — a tithe has no closing kind
    ]
    written = await sheets.log_close(http, {"row": row})
    link = sheets.row_link(written)
    said = f"🙏 десятина <b>{abs(amount):,.2f}$</b> записана"
    return f'<a href="{link}">{said}</a>' if link else said


def exit_buttons(symbol: str) -> dict:
    """The inline keyboard offering partial exits for one position."""
    return {
        "inline_keyboard": [
            [
                {
                    "text": f"{share}%" if share < 100 else "❌ 100%",
                    "callback_data": f"{_TAP}:{symbol}:{share}",
                }
                for share in EXIT_SHARES
            ]
        ]
    }


async def _exit_qty(
    http: httpx.AsyncClient, symbol: str, size: float, percent: float
) -> float | None:
    """What a percent button closes out of `size`; None when it is dust.

    Rounded down to the lot step — a quantity the exchange would refuse
    closes nothing at all, and half a step is worse than a whole one.
    """
    if percent >= 100:
        return size
    step, min_qty = await _lot(http, symbol)
    if step <= 0:
        return None
    qty = math.floor(size * percent / 100 / step) * step
    if qty < min_qty or qty <= 0:
        return None
    # A remainder too small to be closed later is not worth leaving behind.
    if size - qty < min_qty:
        return size
    return qty


async def shave_ladder(http: httpx.AsyncClient, symbol: str, side: str, qty: float) -> None:
    """Take `qty` out of the ladder, starting at the rung furthest from entry.

    A button closes part of the position at market, and the exits are then
    larger than what is left. Rebuilding would shrink every rung in
    proportion, including the breakeven one — the whole reason the ladder
    was built. Eating the far end instead leaves the near rungs exactly
    where they stand: the cut comes out of the target, which is what taking
    profit early means.

    Downwards when the far rung is smaller than the cut, so the exits keep
    adding up to the position. Nothing else in the ladder depends on more
    than that.

    Never raises: the market close already happened, and a ladder left
    oversized is corrected by the next rebuild.
    """
    try:
        orders = (await maker_exits(http)).get(symbol) or []
        # Furthest from the entry first: a long's rungs sell above it, a
        # short's buy below.
        orders.sort(key=lambda o: float(o["price"]), reverse=side == "long")
        left = qty
        for order in orders:
            if left <= 0:
                break
            have = float(order.get("qty") or 0)
            if have <= left:
                await _post(
                    http,
                    "/v5/order/cancel",
                    {"category": "linear", "symbol": symbol, "orderId": order["orderId"]},
                )
                left -= have
                continue
            await _post(
                http,
                "/v5/order/amend",
                {
                    "category": "linear",
                    "symbol": symbol,
                    "orderId": order["orderId"],
                    "qty": _qty(have - left),
                },
            )
            left = 0.0
    # Deliberately broad: the position is already smaller, and an uncut
    # ladder is a cosmetic wrong the next rebuild fixes.
    except Exception:  # noqa: BLE001
        log.exception("could not shave the ladder on %s", symbol)


async def close_fraction(http: httpx.AsyncClient, symbol: str, percent: float) -> str:
    """Close part of one position at market — what the exit buttons do."""
    if not enabled():
        return _NO_KEYS
    try:
        result = await _get(http, _POSITIONS, {"category": "linear", "symbol": symbol})
        rows = [r for r in result.get("list", []) if float(r.get("size") or 0) != 0]
        if not rows:
            return f"{base_symbol(symbol)}: position already closed"
        row = rows[0]
        size = float(row["size"])
        qty = await _exit_qty(http, symbol, size, percent)
    # Deliberately broad: a tap must answer, not crash the command loop.
    except Exception:  # noqa: BLE001
        log.exception("partial close listing failed for %s", symbol)
        return _NO_ANSWER
    if qty is None:
        return f"{base_symbol(symbol)}: {percent:g}% — below the minimum lot"
    # Claim the trimmer's cooldown before the order, not after: it polls
    # twice a second, and Bybit needs a moment to show the smaller size —
    # without this the trimmer would cut a second time off the stale one.
    _trim_cooldown[symbol] = time.monotonic()
    try:
        await _close_market(
            http, symbol, row.get("side", ""), _qty(qty), int(row.get("positionIdx") or 0)
        )
    # Deliberately broad: the refusal text is the answer.
    except Exception as exc:  # noqa: BLE001
        log.exception("could not close %g%% of %s", percent, symbol)
        return f"❌ {base_symbol(symbol)}: {exc}"
    # The exits now cover more than the position does. Take the difference
    # off the far end rather than letting a rebuild shrink every rung.
    if qty < size:
        await shave_ladder(http, symbol, "long" if row.get("side") == "Buy" else "short", qty)
    part = "the whole position" if qty >= size else f"{percent:g}% ({qty:g})"
    return f"✂️ {base_symbol(symbol)}: closing {part} — 💸 report follows"


# Fee assumed per fill for the "net" estimate: market orders pay taker.
TAKER_FEE = float(os.getenv("TAKER_FEE", "0.00055"))


async def closed_history(http: httpx.AsyncClient, days: int = 30) -> list[dict]:
    """Closed-pnl records for the last `days`, paged and chunked.

    Bybit caps the query window, so the span is walked in seven-day chunks,
    each drained through its cursor.
    """
    end = int(time.time() * 1000)
    start = end - days * 86_400_000
    window = 7 * 86_400_000
    rows: list[dict] = []
    a = start
    while a < end:
        b = min(a + window, end)
        cursor = ""
        while True:
            params = {
                "category": "linear",
                "startTime": str(a),
                "endTime": str(b),
                "limit": "100",
            }
            if cursor:
                params["cursor"] = cursor
            result = await _get(http, "/v5/position/closed-pnl", params)
            rows.extend(result.get("list") or [])
            cursor = result.get("nextPageCursor") or ""
            if not cursor:
                break
        a = b
    return rows


async def stats_report(http: httpx.AsyncClient, send_photo, arg: str = "") -> str:
    """Rolling days of closed trades: figures plus the equity curve.

    `arg` is the day count typed after the command — /statistics 7 — with
    thirty as the default and ninety as the ceiling.
    """
    if not enabled():
        return _NO_KEYS
    try:
        days = max(1, min(int(arg), 90)) if arg.strip() else 30
    except ValueError:
        return "How many days? e.g. /statistics 7"
    try:
        rows = await closed_history(http, days)
    # Deliberately broad: a chat command must answer, not crash the poller.
    except Exception:  # noqa: BLE001
        log.exception("stats history failed")
        return _NO_ANSWER
    if not rows:
        return f"No closed trades in {days} d"
    depo = await equity(http)

    from datetime import date, datetime, timedelta

    pnls = [float(r.get("closedPnl") or 0) for r in rows]
    wins = sum(1 for value in pnls if value >= 0)
    total = sum(pnls)
    buckets: dict[date, float] = {}
    for r, value in zip(rows, pnls, strict=True):
        day = datetime.fromtimestamp(int(r.get("updatedTime") or 0) / 1000).date()
        buckets[day] = buckets.get(day, 0.0) + value
    today = date.today()
    daily = [buckets.get(today - timedelta(days=i), 0.0) for i in range(days - 1, -1, -1)]

    text = (
        f"📊 last {days} d: trades {len(pnls)} · win {wins}/loss {len(pnls) - wins}"
        f" ({wins / len(pnls) * 100:.0f}%)\n"
        f"PnL <b>{_usd(total)}"
    )
    if depo:
        text += f" ({total / depo * 100:+.2f}% depo {depo:,.0f})"
    text += f"</b> · best {_usd(max(pnls))} · worst {_usd(min(pnls))}"

    try:
        png = chart.equity_curve(daily, title=f"PnL · {days}d", depo=depo, end=today)
    # Deliberately broad: the curve is garnish on the figures.
    except Exception:  # noqa: BLE001
        log.exception("no equity curve")
        return text
    if await send_photo(text, png):
        return ""
    return text


async def market_report(http: httpx.AsyncClient, send_photo) -> None:
    """BTC and ETH side by side on one 15m picture, for /status."""
    try:
        pngs = []
        closes = []
        for market in ("BTCUSDT", "ETHUSDT"):
            _, candles = await _klines(http, market, CHART_INTERVAL, CHART_BARS)
            closes.append(candles[-1].close)
            pngs.append(
                chart.render(
                    base_symbol(market),
                    "",
                    candles,
                    candles[-1].close,
                    timeframe=f"{CHART_INTERVAL}m",
                    plain=True,
                )
            )
        caption = f"BTC {closes[0]:,.0f} · ETH {closes[1]:,.0f} · {CHART_INTERVAL}m"
        await send_photo(caption, chart.side_by_side(pngs))
    # Deliberately broad: the market picture is garnish on /status.
    except Exception:  # noqa: BLE001
        log.exception("no market snapshot")


async def positions_report(http: httpx.AsyncClient, send_album=None) -> str:
    """Every open position as one line, for the bot's /positions command.

    With `send_album` the charts go out as one media group whose first
    caption carries the whole report — a single Telegram message. The
    function then returns "" so the caller has nothing left to send.
    """
    if not enabled():
        return _NO_KEYS
    try:
        open_now = await positions(http)
    # Deliberately broad: a chat command must answer, not crash the poller.
    except Exception:  # noqa: BLE001
        log.exception("positions report failed")
        return _NO_ANSWER
    if not open_now:
        return "No open positions"
    depo = await equity(http)
    lines = []
    pngs = []
    total = 0.0
    total_net = 0.0
    for symbol, position in sorted(open_now.items()):
        # uPnL is pure price difference; what was really paid so far (entry
        # fills, from the execution log) and the taker exit still to come
        # both come off. An unknown history falls back to a taker entry.
        real = await entry_fee_per_unit(http, symbol, position)
        paid = real * position.size if real is not None else TAKER_FEE * position.value
        fees = paid + TAKER_FEE * position.value
        funding = await accrued_funding(http, symbol, position.created_ms)
        net = position.unrealised - fees - funding
        block, rr, at_sl, at_tp = _position_block(symbol, position, fees, funding, net, depo)
        total += position.unrealised
        total_net += net
        lines.append(block)
        if send_album is not None:
            png = await _position_chart(
                http, symbol, position, real, fees, funding, net, rr, at_sl, at_tp, depo
            )
            if png is not None:
                pngs.append(png)
    # A total of one position would just repeat its line.
    if len(lines) > 1:
        lines.append(f"ΣPnL{_usd(total)}=<b>{_usd(total_net)}{_share(total_net, depo)}</b>")
    text = "\n".join(lines)
    # Telegram caps a media-group caption at 1024 characters.
    if send_album is not None and pngs and len(text) <= 1024 and await send_album(text, pngs):
        return ""
    return text


def _position_block(
    symbol: str, position: Position, fees: float, funding: float, net: float, depo: float | None
) -> tuple[str, float | None, float | None, float | None]:
    """One /positions block, plus (rr, at_sl, at_tp) for the chart notes."""
    head = (
        f"{_arrow(position.side)}{base_symbol(symbol)} {_val(position.value)}$@{position.price:g}"
    )
    rr = None
    if position.stop_loss and position.take_profit:
        rr = abs(position.take_profit - position.price) / abs(position.price - position.stop_loss)
        head += f" | RR{rr:.2f}"
    exits = []
    at_sl = at_tp = None
    if position.stop_loss:
        # What the stop costs if it fires, fees included.
        at_sl = -abs(position.price - position.stop_loss) * position.size - fees - funding
        exits.append(f"sl{position.stop_loss:g}:<b>{_usd(at_sl)}{_share(at_sl, depo)}</b>")
    if position.take_profit:
        sign = 1 if position.side == "long" else -1
        at_tp = sign * (position.take_profit - position.price) * position.size - fees - funding
        tp_depo = f"=depo{depo + at_tp:,.2f}$" if depo else ""
        exits.append(
            f"tp{position.take_profit:g}:<b>{_usd(at_tp)}{_share(at_tp, depo)}{tp_depo}</b>"
        )
    fund_note = ""
    if funding > 0:
        fund_note = f"−funding{_sig2(funding)}"
    elif funding < 0:
        fund_note = f"+funding{_sig2(-funding)}"
    pnl = (
        f"PnL{position.unrealised:+,.2f}−fees{_sig2(fees)}{fund_note}"
        f"=<b>{net:+,.2f}{_share(net, depo)}</b>"
    )
    return "\n".join([head, *exits, pnl]), rr, at_sl, at_tp


async def _position_chart(
    http: httpx.AsyncClient,
    symbol: str,
    position: Position,
    real: float | None,
    fees: float,
    funding: float,
    net: float,
    rr: float | None,
    at_sl: float | None,
    at_tp: float | None,
    depo: float | None,
) -> bytes | None:
    """The /positions album chart for one position, breakeven drawn in."""
    be = breakeven_price(
        position.side,
        position.price,
        funding / position.size if position.size else 0.0,
        entry_fee_per_unit=real,
    )
    tp_note, sl_note = _chart_notes(at_tp, at_sl, depo)
    return await entry_chart(
        http,
        symbol,
        position,
        entry_note=(
            f"{_val(position.value)}$"
            + (f" ({position.value / depo * 100:.1f}% depo)" if depo else "")
            + (f" | RR {rr:.2f}" if rr is not None else "")
            + f" | PnL {position.unrealised:+,.2f} - fee {fees:.2f}"
            + f" = {net:+,.2f}{_share(net, depo)}"
        ),
        tp_note=tp_note,
        sl_note=sl_note,
        breakeven=be,
    )


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


def breakeven_price(
    side: str,
    entry: float,
    extra_cost_per_unit: float = 0.0,
    entry_fee_per_unit: float | None = None,
) -> float:
    """The exit price where the trade nets zero.

    The exit is priced as a taker market close. The entry fee is the real
    one per unit when known (a limit fill costs maker, not taker); without
    it a taker entry is assumed — the pessimistic bound. Any extra per-unit
    cost is the funding the position has actually been charged so far; a
    short that RECEIVES funding gets a kinder breakeven the same way.
    """
    t = TAKER_FEE
    paid = entry * t if entry_fee_per_unit is None else entry_fee_per_unit
    if side == "long":
        return (entry + paid + extra_cost_per_unit) / (1 - t)
    return (entry - paid - extra_cost_per_unit) / (1 + t)


async def accrued_fees(
    http: httpx.AsyncClient, symbol: str, since_ms: int, exec_type: str
) -> float:
    """execFee actually charged since `since_ms`, best-effort; unknown is zero.

    The execution log only reaches seven days back, so an older position's
    entry fees can fall off the edge — the callers treat zero as "unknown"
    and fall back to an estimate.
    """
    if not since_ms:
        return 0.0
    try:
        result = await _get(
            http,
            "/v5/execution/list",
            {
                "category": "linear",
                "symbol": symbol,
                "execType": exec_type,
                "startTime": str(since_ms),
                "limit": "100",
            },
        )
        return sum(float(r.get("execFee") or 0) for r in result.get("list", []))
    # Deliberately broad: the chart line is garnish, zero is a fine answer.
    except Exception:  # noqa: BLE001
        log.exception("no %s history for %s", exec_type, symbol)
        return 0.0


async def accrued_funding(http: httpx.AsyncClient, symbol: str, since_ms: int) -> float:
    """Funding actually charged on the position since it opened.

    Bybit settles funding on its own clock (every eight hours on most
    perpetuals); each charge is an execFee — positive paid, negative
    received.
    """
    return await accrued_fees(http, symbol, since_ms, "Funding")


async def entry_fee_per_unit(
    http: httpx.AsyncClient, symbol: str, position: Position
) -> float | None:
    """The real entry cost per unit, from the trade executions.

    Covers however the position was actually filled — maker, taker or a mix.
    None when nothing is on record (a fill not yet in the log, or a position
    older than the seven-day execution history): the caller assumes taker.
    """
    fees = await accrued_fees(http, symbol, position.created_ms, "Trade")
    if fees <= 0 or position.size <= 0:
        return None
    return fees / position.size


async def entry_chart(
    http: httpx.AsyncClient,
    symbol: str,
    position: Position,
    entry_note: str = "",
    tp_note: str = "",
    sl_note: str = "",
    breakeven: float | None = None,
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
            notes=chart.Notes(entry=entry_note, tp=tp_note, sl=sl_note),
            breakeven=breakeven,
        )
    # Deliberately broad: a chart is garnish, never worth losing the notice.
    except Exception:  # noqa: BLE001
        log.exception("no chart for %s", symbol)
        return None


def _timeframe_label(interval: str) -> str:
    """Bybit counts minutes; a chart reads better in hours past the hour."""
    minutes = int(interval)
    if minutes >= 60 and minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


async def close_chart(
    http: httpx.AsyncClient,
    symbol: str,
    was: Position,
    record: dict,
    exit_note: str = "",
    interval: str = "",
) -> bytes | None:
    """A PNG of the finished trade: entry to exit, zone colored by outcome.

    The closed-pnl record carries no TP/SL, so those come from the last
    position snapshot and are drawn as lines. A chart stays on one timeframe
    — CHART_INTERVAL unless `interval` names another — whatever the trade's
    length; Bybit caps one kline request at 1000 bars, so a trade longer than
    that shows its most recent stretch, with the entry clamped to the left
    edge.
    """
    interval = interval or CHART_INTERVAL
    try:
        opened, closed = int(record["createdTime"]), int(record["updatedTime"])
        span = int(interval) * 60_000
        trade_bars = (closed - opened) // span
        # Lead-in fills the frame up to CHART_BARS of context around the trade.
        lead = max(CHART_BARS - trade_bars - 3, 8)
        bars = min(trade_bars + lead + 4, 3000)  # plus the tail and a slack bar
        times, candles = await _klines(
            http,
            symbol,
            interval,
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
            exit_at=(_bar_of(times, closed), float(record["avgExitPrice"])),
            pad_right=2,
            timeframe=_timeframe_label(interval),
            notes=chart.Notes(exit=exit_note),
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
        return ["⚠️ no stop"]
    risk = abs(position.price - position.stop_loss) * position.size
    rr = None
    if position.take_profit is not None:
        rr = abs(position.take_profit - position.price) / abs(position.price - position.stop_loss)
    warns = []
    if depo and RISK_TARGET:
        # Against the capped base, the same one the sizer and trimmer aim at,
        # scaled by RR the way the sizer scales the order.
        target = RISK_TARGET
        if rr is not None and RR_FULL_RISK > 0 and rr < RR_FULL_RISK:
            target = RISK_TARGET * rr / RR_FULL_RISK
        actual = risk / risk_base(depo)
        # Within a quarter of the figure nobody wants a ping. Which side of
        # it counts depends on what the figure is: a target is missed in
        # both directions, a ceiling only upwards — risking less than the
        # most you allow yourself is the trade going to plan.
        over = actual - target > target * 0.25
        adrift = over if not RISK_GROW else abs(actual - target) > target * 0.25
        if adrift:
            word = "cap" if not RISK_GROW else "target"
            warns.append(f"⚠️ risk {actual * 100:.2f}% depo, {word} {target * 100:.2g}%")
    if rr is not None and MIN_RR and rr < MIN_RR:
        warns.append(f"⚠️ RR {rr:.2f} < {MIN_RR:g}")
    return warns


async def _account_figure(http: httpx.AsyncClient, *fields: str) -> float | None:
    """The first non-empty of `fields` on the account, in USDT; best-effort."""
    try:
        result = await _get(
            http,
            "/v5/account/wallet-balance",
            {"accountType": os.getenv("ACCOUNT_TYPE", "UNIFIED")},
        )
        rows = result.get("list") or []
        if not rows:
            return None
        value = next((rows[0].get(field) for field in fields if rows[0].get(field)), None)
        return float(value) if value else None
    # Deliberately broad: no account figure must never block a notice.
    except Exception:  # noqa: BLE001
        log.exception("no %s", fields[0])
        return None


async def equity(http: httpx.AsyncClient) -> float | None:
    """Account equity in USDT, best-effort: garnish for the PnL percent."""
    return await _account_figure(http, "totalEquity", "totalWalletBalance")


async def wallet_balance(http: httpx.AsyncClient) -> float | None:
    """Wallet balance in USDT, unrealised PnL excluded: the base every risk
    figure is measured against.

    Equity breathes with the open positions' own PnL: a drawdown shrinks
    it, every risk share grows, and a trimmer keyed to it would slice the
    losers on the way down. The wallet only moves on fills and transfers,
    so a position sized at RISK_PCT stays that size until it closes.
    """
    return await _account_figure(http, "totalWalletBalance")


async def deposit_line(http: httpx.AsyncClient) -> str:
    """The 💰 line under /status: the wallet, and what differs from it.

    The wallet balance is the deposit every risk share is measured against.
    Equity is named too when the open positions move it, and the capped risk
    base when RISK_EQUITY_MAX holds the sizing below the real balance.
    """
    wallet = await wallet_balance(http)
    if wallet is None:
        return "💰 deposit unavailable"
    line = f"💰 deposit {wallet:,.2f}$"
    total = await equity(http)
    if total is not None and round(total, 2) != round(wallet, 2):
        line += f" · equity {total:,.2f}$ ({total - wallet:+,.2f}$ open)"
    base = risk_base(wallet)
    if base != wallet:
        line += f" · risk base {base:,.2f}$"
    return line


def risk_base(depo: float) -> float:
    """The deposit a risk percent is measured against, RISK_EQUITY_MAX applied.

    Only the risk arithmetic uses this — the notices keep reporting the real
    wallet, so a capped account still sees its true balance and true shares.
    """
    if RISK_EQUITY_MAX > 0:
        return min(depo, RISK_EQUITY_MAX)
    return depo


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


# The journal is a Russian-language document: its headers are Russian and it
# is read as one. Telegram and the logs stay English (a74056e), so the sheet's
# words live here and only here — the notice keeps the English `kind`, the row
# that goes to the sheet gets its translation.
_SHEET_RU = {
    "stop": "стоп",
    "take": "тейк",
    "manual": "руками",
    "risk-guard": "риск-гард",
    "no stop": "нет стопа",
}


def _ru(word: str) -> str:
    """A journal cell in the sheet's language; unknown text passes through."""
    if word in _SHEET_RU:
        return _SHEET_RU[word]
    # The leverage breach carries its numbers, so only the lead word turns.
    if word.startswith("leverage "):
        return "плечо " + word.removeprefix("leverage ")
    return word


# The stop and take a position carried when it opened, by symbol. Both are
# dragged while a trade runs — a stop pulled to breakeven leaves almost no
# risk to divide by, which is how one trade came out as 2.34R on a stop
# worth 13$ and an RR of 1:50.4. R and RR describe the trade that was
# entered, so they are measured against these, not against the levels that
# happened to be standing at the close. Keyed by the position's open time,
# so a position the relay never saw open is known to be unknown.
_entry_levels: dict[str, tuple[int, float | None, float | None, float]] = {}

# The entry screenshots in flight. asyncio keeps only a weak reference to a
# running task, so a shot nobody holds can be collected mid-flight and the
# journal quietly loses the frame; these hold them until they are done.
_shooting: set[asyncio.Task] = set()


def remember_levels(symbol: str, now: Position) -> None:
    """Keep the stop, the take and the moment an opening position appeared."""
    _entry_levels[symbol] = (now.created_ms, now.stop_loss, now.take_profit, time.time())


def entry_levels(symbol: str, was: Position) -> tuple[float | None, float | None]:
    """The stop and take this position opened with; (None, None) if unseen.

    A relay restarted mid-trade did not see the entry. Saying so costs two
    empty cells; guessing with the current stop costs a number that looks
    like a result and is not one.
    """
    seen = _entry_levels.get(symbol)
    if seen is None or seen[0] != was.created_ms:
        return None, None
    return seen[1], seen[2]


def opened_at(symbol: str, was: Position) -> float | None:
    """When the relay first saw this position, in epoch seconds; None if never.

    `Position.created_ms` looked like the obvious answer and is not: two
    positions on different symbols, closed a day apart, both reported
    12.07.26 through it. Whatever Bybit counts from there, it is not this
    trade's open. The relay's own sighting is, for every trade it watched
    open.
    """
    seen = _entry_levels.get(symbol)
    if seen is None or seen[0] != was.created_ms:
        return None
    return seen[3]


def _journal_meta(
    symbol: str, was: Position, record: dict, entry: float, pnl: float, forced: str
) -> tuple[str, str, float | str]:
    """(open date, RR text, R multiple) for the journal row.

    The R multiple is PnL over the risk the entry stop carried. Without that
    stop there is no R, and the cell stays empty: the money is in the PnL
    column already, and a dollar figure sitting in an R column made the
    totals add R to USDT.
    """
    from datetime import datetime

    # The relay's own sighting of the open, and the closing record's time
    # when it never saw one. `Position.created_ms` is neither: it dated two
    # positions closed a day apart to the same 12.07.26.
    seconds = opened_at(symbol, was)
    if seconds is None:
        created = record.get("createdTime")
        seconds = int(created) / 1000 if created else None
    opened = datetime.fromtimestamp(seconds).strftime("%d.%m.%y") if seconds else ""
    stop, take = entry_levels(symbol, was)
    rr = f"принудительно остановлено: {_ru(forced)}" if forced else ""
    if stop and take:
        rr = f"1:{abs(take - entry) / abs(entry - stop):.1f}"
    size = float(record.get("closedSize") or 0) or was.size
    risk = abs(entry - stop) * size if stop else 0.0
    fact: float | str = round(pnl / risk, 2) if risk else ""
    return opened, rr, fact


def journal_row(
    symbol: str,
    was: Position,
    record: dict,
    pnl: float,
    depo: float | None = None,
    forced: str = "",
    kind: str = "",
) -> list:
    """One trade as a row of the trading-diary sheet.

    Columns: Date, Pair, Side, Result, RR, Result (R), Screen,
    Volume $, Entry, Exit, PnL, % depo, Fees, Depo.
    One row per position, not per closing execution: `record` is the merged
    record of every piece, so a position trimmed six times and then closed
    reports what it made, not what its last 229 coins made. The result
    figure is the R multiple against the entry stop, empty without one.
    Free-text columns stay empty for hand-written notes.
    """
    entry = float(record.get("avgEntryPrice") or was.price)
    opened, rr, fact = _journal_meta(symbol, was, record, entry, pnl, forced)
    # The position's whole size, which `was` no longer is once it was trimmed.
    closed_size = float(record.get("closedSize") or 0)
    value = round(closed_size * entry, 2) if closed_size and entry else round(was.value, 2)
    opened_fee = float(record.get("openFee") or 0)
    closed_fee = float(record.get("closeFee") or 0)
    exit_price = float(record.get("avgExitPrice") or 0)
    return [
        opened,
        base_symbol(symbol),
        "Лонг" if was.side == "long" else "Шорт",
        "win" if pnl >= 0 else "stop",
        rr,
        fact,
        "",
        value,
        entry,
        exit_price or "",
        float(_sig2(pnl)),
        # A fraction, not a number already multiplied by a hundred: the cell
        # is formatted as a percentage, and Sheets multiplies it again. 0.13
        # written here showed as 13.00%, so the column was being divided back
        # down by hand, trade after trade. Six decimals, because four round a
        # 0.0746% trade to 0.07% before the format ever sees it.
        round(pnl / depo, 6) if depo else "",
        f"{_sig2(opened_fee)}+{_sig2(closed_fee)}" if opened_fee or closed_fee else "",
        round(depo, 2) if depo else "",
        "",  # O: Withdrew/Deposited — the money watcher's column
        _ru(kind),
    ]


async def close_kind(http: httpx.AsyncClient, record: dict) -> str:
    """How the closing order came to be: stop, take or manual.

    Bybit's order history carries `createType` — CreateByStopLoss and
    CreateByTakeProfit mean the exchange fired a level, CreateByUser means a
    hand on the button. Empty when history has no answer.
    """
    order_id = record.get("orderId") or ""
    if not order_id:
        return ""
    try:
        result = await _get(http, "/v5/order/history", {"category": "linear", "orderId": order_id})
        rows = result.get("list") or []
        if not rows:
            return ""
        create = rows[0].get("createType") or ""
        stop_type = rows[0].get("stopOrderType") or ""
        if "StopLoss" in create or stop_type in ("StopLoss", "Stop"):
            return "stop"
        if "TakeProfit" in create or stop_type == "TakeProfit":
            return "take"
        if create in ("CreateByUser", "CreateByClosing"):
            # The maker exit is our own reduce-only limit resting at the
            # take: its fill is a take, not a hand on the button.
            if TP_MAKER and rows[0].get("reduceOnly") and rows[0].get("orderType") == "Limit":
                return "take"
            return "manual"
        return ""
    # Deliberately broad: the notice must go out with or without the detail.
    except Exception:  # noqa: BLE001
        log.exception("no close kind for %s", order_id)
        return ""


# Bybit writes the closed-pnl row moments AFTER the position disappears;
# at a half-second poll the close is usually seen first. So the record is
# asked for a few times, and one older than this many seconds is somebody
# else's trade, not the close that just happened.
PNL_RETRIES = 6
PNL_RETRY_SEC = 1.0
PNL_FRESH_SEC = 300.0


# The newest closed-pnl record already announced for a symbol, by Bybit's
# own clock. A trim files a record of its own, so "the newest record is less
# than five minutes old" stops meaning "this is the close that just
# happened": one position trimmed four times and then closed reported the
# fourth trim instead, 153.25 of a trade that made 617.64, because that
# record was 297 seconds old and the real close was not written yet.
# Compared against the exchange's timestamps and never against ours, so a
# clock that drifts cannot starve the check.
_last_close_ms: dict[str, int] = {}


async def _fresh_closes(
    http: httpx.AsyncClient, symbol: str, limit: str, newer_than: int = 0
) -> list[dict]:
    """Closed-pnl records, newest first, once the newest one is the fresh one.

    Retries while Bybit is still writing it, and refuses a stale answer —
    reporting the previous trade's figures would be worse than none. Stale
    means too old, and also not newer than a record already reported.
    """
    for attempt in range(PNL_RETRIES):
        if attempt:
            await asyncio.sleep(PNL_RETRY_SEC)
        try:
            result = await _get(
                http,
                "/v5/position/closed-pnl",
                {"category": "linear", "symbol": symbol, "limit": limit},
            )
        # Deliberately broad: the close notice must go out even without a figure.
        except Exception:  # noqa: BLE001
            log.exception("no closed pnl for %s", symbol)
            return []
        rows: list[dict] = result.get("list", [])
        if rows:
            written = int(rows[0].get("updatedTime") or 0)
            age = time.time() - written / 1000
            if age < PNL_FRESH_SEC and written > newer_than:
                return rows
    log.warning("closed pnl for %s never appeared", symbol)
    return []


async def closed_record(http: httpx.AsyncClient, symbol: str) -> dict | None:
    """The record of the one close that just happened — a trim, usually."""
    rows = await _fresh_closes(http, symbol, "1", _last_close_ms.get(symbol, 0))
    if not rows:
        return None
    _last_close_ms[symbol] = int(rows[0].get("updatedTime") or 0)
    return rows[0]


async def closed_group(http: httpx.AsyncClient, symbol: str) -> list[dict]:
    """Every record of the position that just vanished, newest first.

    The newest must be newer than any record already announced for this
    symbol, or the trims this position reported on the way down would stand
    in for the close that ended it.
    """
    rows = await _fresh_closes(http, symbol, "50", _last_close_ms.get(symbol, 0))
    return _same_position(rows)


# How far apart two renderings of one average entry price may sit and still
# be the same position. Bybit does not render it consistently: one position
# produced 1326.481626, 1326.48162595, 1326.48162597 and 1326.48162592
# across its eleven closing records — a different number of digits, and the
# last digit wandering. Comparing the strings grouped two of those eleven
# and booked 73.47 of a trade that made 519.14. A millionth is far wider
# than that wobble and far narrower than two positions apart.
_SAME_PRICE = 1e-6


def _same_price(one: str | None, other: str | None) -> bool:
    """Whether two average entry prices name the same position."""
    try:
        a, b = float(one or 0), float(other or 0)
    except ValueError:
        return False
    return abs(a - b) <= max(abs(a), abs(b)) * _SAME_PRICE


def _same_position(rows: list[dict]) -> list[dict]:
    """The newest record and the older ones that closed the same position.

    Bybit files one record per closing execution and gives them nothing to
    be grouped by: `createdTime` belongs to the closing order, not to the
    position. What the pieces of one position do share is its average entry
    price — so the run of records carrying the newest one's entry and side
    is the position, and the first record that differs ends it.

    The coincidence this cannot see through: closing a position and opening
    another within a millionth of the same average entry merges the two.
    The cost is one merged row.
    """
    if not rows:
        return []
    head = rows[0]
    group = []
    for row in rows:
        if not _same_price(row.get("avgEntryPrice"), head.get("avgEntryPrice")) or row.get(
            "side"
        ) != head.get("side"):
            break
        group.append(row)
    return group


def merge_closes(group: list[dict]) -> dict:
    """The pieces of one position as a single record.

    A trimmed position is closed piece by piece, and the last piece alone
    told a fraction of the story — 30.81 of a trade that made 116.73. Money
    and fees add up, the exit is averaged by size, and the kind comes from
    the newest record because that is the close that ended the position.
    """
    merged = dict(group[0])
    size = sum(float(row.get("closedSize") or 0) for row in group)
    merged["closedPnl"] = sum(float(row.get("closedPnl") or 0) for row in group)
    merged["openFee"] = sum(float(row.get("openFee") or 0) for row in group)
    merged["closeFee"] = sum(float(row.get("closeFee") or 0) for row in group)
    merged["closedSize"] = size
    if size:
        merged["avgExitPrice"] = (
            sum(
                float(row.get("avgExitPrice") or 0) * float(row.get("closedSize") or 0)
                for row in group
            )
            / size
        )
    merged["closes"] = len(group)
    return merged


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


def _sig2(amount: float) -> str:
    """Two leading fraction digits: 0.5320 -> 0.53, 0.04213 -> 0.042."""
    if amount == 0:
        return "0.00"
    decimals = max(2, 1 - math.floor(math.log10(abs(amount))))
    return f"{amount:.{decimals}f}"


def _usd(amount: float) -> str:
    """Signed money: cents normally, two significant fraction digits below."""
    if abs(amount) >= 0.995:
        return f"{amount:+,.2f}"
    if amount > 0:
        return f"+{_sig2(amount)}"
    if amount < 0:
        return f"-{_sig2(abs(amount))}"
    return "+0.00"


def _val(value: float) -> str:
    """Unsigned position value: whole dollars, cents when below ten."""
    return f"{value:,.0f}" if value >= 10 else f"{value:,.2f}"


def _arrow(side: str) -> str:
    return "📈" if side == "long" else "📉"


def describe(kind: str, symbol: str, was: Position | None, now: Position | None) -> tuple[str, str]:
    """One change as (bot line, spoken line) — the same terse dialect as
    /positions: direction arrow, value@entry, no filler words."""
    sym = base_symbol(symbol)
    name = COIN_NAMES.get(sym, sym)
    if kind == "opened" and now is not None:
        # The stop-loss risk annotation is appended by tick(), which knows
        # the deposit share.
        head = f"💰{_arrow(now.side)}{sym} {_val(now.value)}$@{now.price:g}"
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


def _share(amount: float, depo: float | None) -> str:
    """The amount as a signed share of the deposit; nothing without one."""
    return f"({amount / depo * 100:+.2f}%)" if depo else ""


def _exit_lines(
    now: Position, fees: float, depo: float | None
) -> tuple[float | None, float | None, list[str]]:
    """(at_sl, target, notice lines): what each exit costs or pays, fees off."""
    at_sl = target = None
    exits = []
    if now.stop_loss is not None:
        # What the stop costs if it fires, fees included.
        at_sl = -abs(now.price - now.stop_loss) * now.size - fees
        exits.append(f"sl{now.stop_loss:g}:<b>{_usd(at_sl)}{_share(at_sl, depo)}</b>")
    if now.take_profit is not None:
        # What reaching the TP pays, net of both fees: the entry fee
        # just paid and a like-sized one for the exit.
        target = abs(now.take_profit - now.price) * now.size - fees
        tp_depo = f"=depo{depo + target:,.2f}$" if depo else ""
        exits.append(f"tp{now.take_profit:g}:<b>{_usd(target)}{_share(target, depo)}{tp_depo}</b>")
    return at_sl, target, exits


def _chart_notes(target: float | None, at_sl: float | None, depo: float | None) -> tuple[str, str]:
    """The chart's TP and SL annotations, with the deposit they would leave."""
    tp_note = sl_note = ""
    if target is not None:
        tp_note = f"{target:+,.2f}{_share(target, depo)}"
        if depo:
            tp_note += f" = {depo + target:,.2f}$"
    if at_sl is not None:
        sl_note = f"{at_sl:+,.2f}{_share(at_sl, depo)}"
        if depo:
            sl_note += f" = {depo + at_sl:,.2f}$"
    return tp_note, sl_note


async def _entry_notice(
    http: httpx.AsyncClient, symbol: str, now: Position, line: str
) -> tuple[str, bytes | None]:
    """The entry notice with exits, fees and warnings, plus its chart."""
    fee = await entry_fee(http, symbol)
    depo = await equity(http)
    fees = 2 * (fee or TAKER_FEE * now.value)
    rr = None
    if now.stop_loss is not None and now.take_profit is not None:
        rr = abs(now.take_profit - now.price) / abs(now.price - now.stop_loss)
        line += f" | RR{rr:.2f}"
    at_sl, target, exits = _exit_lines(now, fees, depo)
    if exits:
        line += "\n" + "\n".join(exits)
    if fee:
        line += f"\nfees{_sig2(fee)}"
    # The line is HTML: a bare "<" in "RR 0.63 < 2" makes Telegram
    # refuse the whole notice, photo and text fallback both.
    for warn in trade_warnings(now, depo):
        line += f"\n{html.escape(warn)}"
    tp_note, sl_note = _chart_notes(target, at_sl, depo)
    png = await entry_chart(
        http,
        symbol,
        now,
        breakeven=breakeven_price(
            now.side,
            now.price,
            entry_fee_per_unit=fee / now.size if fee and now.size else None,
        ),
        entry_note=(
            f"{_val(now.value)}$"
            + (f" ({now.value / depo * 100:.1f}% depo)" if depo else "")
            + (f" | RR {rr:.2f}" if rr is not None else "")
            + (f" | fee {fee:.2f}" if fee else "")
        ),
        tp_note=tp_note,
        sl_note=sl_note,
    )
    return line, png


async def _close_notice(
    http: httpx.AsyncClient, symbol: str, was: Position, spoken_line: str
) -> tuple[str | None, str, bytes | None, dict | None]:
    """The close notice, its chart, its keyboard and the journal write.

    A position closed in pieces is reported whole: every piece is collected
    and merged, so the figure that goes out is what the trade made, not what
    its last slice made. The keyboard carries the tithe button on a winning
    trade — `tick` does not see the PnL, so the offer is made here, where it
    is known.
    """
    group = await closed_group(http, symbol)
    if not group:
        return None, spoken_line, None, None
    record = merge_closes(group)
    # Bybit's closedPnl nets both trading fees; funding it does
    # not — that is charged separately, so it comes off here.
    funding = await accrued_funding(http, symbol, was.created_ms)
    pnl = float(record["closedPnl"]) - funding
    depo = await equity(http)
    opened_fee = float(record.get("openFee") or 0)
    closed_fee = float(record.get("closeFee") or 0)
    spoken_line += f", {'profit' if pnl >= 0 else 'loss'} {abs(pnl):.0f}"
    total_fees = opened_fee + closed_fee
    exit_note = (
        f"PnL {pnl + total_fees:+,.2f} - fee {total_fees:.2f} = {pnl:+,.2f}{_share(pnl, depo)}"
    )
    # One picture per timeframe, then one picture out of them: the journal has
    # a single screenshot column, and Telegram reads better with the pair side
    # by side than as two attachments.
    drawn = []
    # The host shoots the user's own TradingView pane while the bot draws its
    # own chart: both take seconds, and there is no reason to spend them twice.
    shooting = asyncio.create_task(tv_shot.shot(http, symbol))
    for interval in CLOSE_INTERVALS:
        one = await close_chart(http, symbol, was, record, exit_note=exit_note, interval=interval)
        if one is not None:
            drawn.append(one)
    png = chart.side_by_side(drawn) if len(drawn) > 1 else next(iter(drawn), None)
    seen, why = await shooting
    forced = _guard_closed.pop(symbol, "")
    kind = "risk-guard" if forced else await close_kind(http, record)
    entry: dict = {"row": journal_row(symbol, was, record, pnl, depo, forced, kind)}
    # The journal would rather have the chart the trade was actually read on.
    # Without it the row gets the bot's own, exactly as it always has.
    journalled = seen or png
    if journalled is not None:
        # The Apps Script saves it to Drive and writes the link
        # into the "Link" column of the same row.
        entry["png"] = base64.b64encode(journalled).decode()
        entry["name"] = f"{base_symbol(symbol)}-{record.get('updatedTime', '')}"
    # The frame kept since the entry notice, into its own column.
    opened_png = tv_shot.take(symbol)
    if opened_png is not None:
        entry["png_entry"] = base64.b64encode(opened_png).decode()
    # The journal goes first so the notice can point at the row it produced;
    # a refusal only costs the link, never the notice.
    link = sheets.row_link(await sheets.log_close(http, entry))
    # Net first, ticker after: the money is the news.
    line = f"💸<b>{_usd(pnl)}</b>"
    if depo:
        share = f"<b>{depo:,.2f}$</b>{_share(pnl, depo)}"
        line += "=" + (f'<a href="{link}">{share}</a>' if link else share)
    line += f"·{_arrow(was.side)}{base_symbol(symbol)}"
    if kind:
        line += f"·{kind}"
    if opened_fee or closed_fee or funding:
        costs = f"fees {_sig2(opened_fee + closed_fee)}"
        if funding:
            costs += f", funding {_sig2(funding)}"
        line += f" ({costs})"
    if why:
        # Never a silent downgrade: the row has the drawn chart, not the one
        # the trade was read on, and this is why.
        line += f"\n📷 {html.escape(why)}"
    if record["closes"] > 1:
        # Each trim already announced its own slice. Saying how many closes
        # this figure covers is what keeps the two from reading as one sum
        # twice over.
        line += f" · {record['closes']} закрытий"
    _entry_levels.pop(symbol, None)
    _last_close_ms.pop(symbol, None)
    return line, spoken_line, png, tithe_buttons(pnl)


async def _trim_notice(http: httpx.AsyncClient, symbol: str) -> str:
    """What a trim just realised, for the resize line to carry.

    `/cut` promises "💸 report follows" and until now nothing followed: a
    trim leaves the position open, so the close notice never fired and six
    realised profits in a row went unannounced and unjournalled. The slice
    is announced here; the journal still gets one row per position, written
    when the last slice closes it.
    """
    record = await closed_record(http, symbol)
    if record is None:
        return ""
    pnl = float(record.get("closedPnl") or 0)
    fees = float(record.get("openFee") or 0) + float(record.get("closeFee") or 0)
    note = f" · 💸<b>{_usd(pnl)}</b>"
    return note + (f" (fees {_sig2(fees)})" if fees else "")


async def tick(
    http: httpx.AsyncClient, before: dict[str, Position] | None, send, speak, send_photo
) -> dict[str, Position]:
    """One poll: fetch, announce every change, hand back the new snapshot.

    The first call primes the snapshot silently — positions that were already
    open when the relay started are not news. An entry (or flip) goes out as
    a chart with the notice as its caption and the exit buttons under it;
    everything else stays text.
    """
    after = await positions(http)
    await guard(http, after, send, speak)
    await trim(http, after, send, speak)
    await tp_maker(http, after, send, speak)
    if before is None:
        return after
    for kind, symbol, was, now in diff(before, after):
        line, spoken_line = describe(kind, symbol, was, now)
        png = None
        buttons = None
        if kind in ("opened", "flipped") and now is not None:
            # The levels this trade was entered on, before anything drags them.
            remember_levels(symbol, now)
            line, png = await _entry_notice(http, symbol, now, line)
            if tv_shot.enabled():
                # Behind the notice: the entry is news now, the picture can
                # take its twelve seconds and be there by the close.
                shot = asyncio.create_task(tv_shot.capture_entry(http, symbol, send))
                _shooting.add(shot)
                shot.add_done_callback(_shooting.discard)
            # Exits hang off the entry notice, where the position is news.
            buttons = exit_buttons(symbol)
        elif kind == "changed" and was is not None and now is not None and now.size < was.size:
            line += await _trim_notice(http, symbol)
        elif kind == "closed" and was is not None:
            closed_line, spoken_line, png, buttons = await _close_notice(
                http, symbol, was, spoken_line
            )
            if closed_line is not None:
                line = closed_line
        if png is None or not await send_photo(line, png, buttons):
            # A chartless entry — a fresh listing, a refused photo — is still
            # an entry: the fallback text carries the same exit buttons.
            await send(line, buttons)
        await speak(spoken_line)
    return after


async def money_moves(http: httpx.AsyncClient) -> list[dict]:
    """Finished deposits, withdrawals and unified-account transfers, 7 days.

    Each move: an `id` stable across polls, a signed `amount` (into the
    trading account positive, out of it negative) and the `coin`.

    Money passing through the funding account would show up twice: Bybit's
    withdraw flow is an inter-transfer out of UNIFIED followed by the on-chain
    withdrawal of the same money from FUND, and a deposit lands in FUND before
    it is transferred in. The transfer is the event that crosses the trading
    account's border, so a deposit or withdrawal already covered by a matching
    transfer of the same coin and amount is dropped, not counted twice.
    """
    since = str(int((time.time() - 7 * 86400) * 1000))
    moves: list[dict] = []
    into_trading: set[tuple[str, float]] = set()
    out_of_trading: set[tuple[str, float]] = set()
    result = await _get(http, "/v5/asset/transfer/query-inter-transfer-list", {"limit": "50"})
    for r in result.get("list", []):
        into = r.get("toAccountType") == "UNIFIED"
        out_of = r.get("fromAccountType") == "UNIFIED"
        # Only finished transfers that cross the trading account's border.
        if r.get("status") != "SUCCESS" or into == out_of:
            continue
        amount = float(r.get("amount") or 0)
        coin = r.get("coin", "")
        (into_trading if into else out_of_trading).add((coin, round(amount, 8)))
        moves.append(
            {
                "id": f"tr-{r.get('transferId')}",
                "amount": amount * (1 if into else -1),
                "coin": coin,
            }
        )
    result = await _get(http, "/v5/asset/deposit/query-record", {"startTime": since, "limit": "50"})
    moves.extend(_deposit_moves(result.get("rows", []), into_trading))
    result = await _get(
        http, "/v5/asset/withdraw/query-record", {"startTime": since, "limit": "50"}
    )
    moves.extend(_withdrawal_moves(result.get("rows", []), out_of_trading))
    return moves


def _deposit_moves(rows: list[dict], into_trading: set[tuple[str, float]]) -> list[dict]:
    """Finished deposits not already counted as an incoming transfer."""
    moves = []
    for r in rows:
        amount = float(r.get("amount") or 0)
        coin = r.get("coin", "")
        # Status three means success.
        if str(r.get("status")) == "3" and (coin, round(amount, 8)) not in into_trading:
            moves.append(
                {
                    "id": f"dep-{r.get('txID') or r.get('successAt')}",
                    "amount": amount,
                    "coin": coin,
                }
            )
    return moves


def _withdrawal_moves(rows: list[dict], out_of_trading: set[tuple[str, float]]) -> list[dict]:
    """Finished withdrawals not already counted as an outgoing transfer."""
    moves = []
    for r in rows:
        amount = float(r.get("amount") or 0)
        # The transfer moved amount plus the withdrawal fee out of UNIFIED.
        debited = round(amount + float(r.get("withdrawFee") or 0), 8)
        coin = r.get("coin", "")
        if r.get("status") == "success" and (coin, debited) not in out_of_trading:
            moves.append(
                {
                    "id": f"wd-{r.get('withdrawId')}",
                    "amount": -amount,
                    "coin": coin,
                }
            )
    return moves


async def money_tick(http: httpx.AsyncClient, seen: set[str] | None, send) -> set[str]:
    """One money poll: announce and journal every move not seen before.

    A `seen` of None primes silently — history that predates the relay's
    start is not news, exactly like tick() and its first snapshot.
    """
    from datetime import datetime

    moves = await money_moves(http)
    ids = {str(m["id"]) for m in moves}
    if seen is None:
        return ids
    for m in moves:
        if m["id"] in seen:
            continue
        amount = float(m["amount"])
        word = "deposited" if amount > 0 else "withdrew"
        depo = await equity(http)
        line = f"💵 {word} {amount:+,.2f} {m['coin']}"
        if depo:
            line += f" · depo {depo:,.2f}$"
        await send(line)
        # "transfer" said nothing a reader could not already see; the
        # direction is what the eye wants, and the sign of column O is a poor
        # place to learn it from. The coin follows, for the day the account
        # holds more than one.
        row = [
            datetime.now().strftime("%d.%m.%y"),
            "ввод" if amount > 0 else "вывод",
            m["coin"],
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            round(depo, 2) if depo else "",
            round(amount, 2),
            "",  # P: Закрытие — a transfer has no closing kind
        ]
        await sheets.log_close(http, {"row": row})
    return seen | ids


async def money_poll(http: httpx.AsyncClient, send) -> None:
    """Watch deposits and withdrawals until cancelled. Failures never end it."""
    log.info("watching bybit transfers every 60s")
    seen: set[str] | None = None
    while True:
        try:
            seen = await money_tick(http, seen, send)
        except asyncio.CancelledError:
            raise
        # Deliberately broad: a key without Assets permission answers with an
        # error forever; the watcher must idle, not crash the relay.
        except Exception:  # noqa: BLE001
            log.exception("money poll failed")
        await asyncio.sleep(60.0)


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
