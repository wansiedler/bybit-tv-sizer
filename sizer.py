"""Auto-size the limit orders you draw on the TradingView chart.

Ported from wansiedler/bybit-tv-sizer into the relay: one process, one bot.
You place a limit order with any quantity through Bybit's broker panel on the
chart; this watcher rewrites the quantity so the distance between entry and
stop-loss always risks the same fixed percentage of account equity.

    DRY_RUN=1               # log and announce instead of amending
    RISK_PCT=0.5            # percent of equity between entry and stop
    FALLBACK_SL_PCT=0       # assumed stop when the order has none; 0 skips
    MAX_LEVERAGE=5          # notional ceiling, as a multiple of equity
    SYMBOLS=                # comma-separated allowlist; empty = all
    POLL_SEC=3              # seconds between passes
    SETTLE_POLLS=2          # passes the order must hold still first
    CATEGORY=linear
    SETTLE_COIN=USDT

The variable names match bybit-tv-sizer one to one, and so does the arming:
present API keys switch the sizer on, DRY_RUN=1 keeps it harmless.

Needs the Bybit key to carry Read AND Trade permission (never withdrawal).
It will not touch market orders, conditional orders, reduce-only exits,
partially filled orders, or anything already at the right size.
"""

import asyncio
import logging
import os
from decimal import ROUND_DOWN, Decimal

import httpx
from dotenv import load_dotenv

import bybit_watch

log = logging.getLogger("relay.sizer")

# relay.py imports this module before its own load_dotenv(), same as speaker.
load_dotenv()

DRY_RUN = os.getenv("DRY_RUN", "1").lower() not in ("0", "false", "no", "")
RISK_PCT = Decimal(os.getenv("RISK_PCT", "0.5")) / Decimal(100)
FALLBACK_SL_PCT = Decimal(os.getenv("FALLBACK_SL_PCT", "0")) / Decimal(100)
MAX_LEVERAGE = Decimal(os.getenv("MAX_LEVERAGE", "5"))
SYMBOLS = {s.strip().upper() for s in os.getenv("SYMBOLS", "").split(",") if s.strip()}
POLL = float(os.getenv("POLL_SEC", "3"))
CATEGORY = os.getenv("CATEGORY", "linear")
SETTLE_COIN = os.getenv("SETTLE_COIN", "USDT")
SETTLE_POLLS = int(os.getenv("SETTLE_POLLS", "2"))
ACCOUNT_TYPE = os.getenv("ACCOUNT_TYPE", "UNIFIED")

# Warn when a limit order's reward-to-risk sits below this. 0 disables.
MIN_RR = Decimal(os.getenv("MIN_RR", "2"))

# orderId -> (entry|stop signature, how many passes in a row have seen it)
_settling: dict[str, tuple[str, int]] = {}
_instruments: dict[str, dict] = {}
# orderId -> the entry|stop|tp signature already warned about, so dragging
# nothing does not repeat the same warning every pass.
_warned: dict[str, str] = {}


def enabled() -> bool:
    """Armed exactly like the original: the API key pair being present."""
    return bybit_watch.enabled()


async def get_equity(http: httpx.AsyncClient) -> Decimal:
    result = await bybit_watch._get(
        http, "/v5/account/wallet-balance", {"accountType": ACCOUNT_TYPE}
    )
    accounts = result.get("list") or []
    if not accounts:
        raise RuntimeError("wallet-balance returned no accounts")
    return Decimal(accounts[0].get("totalEquity") or accounts[0].get("totalWalletBalance"))


async def get_open_orders(http: httpx.AsyncClient) -> list[dict]:
    result = await bybit_watch._get(
        http, "/v5/order/realtime", {"category": CATEGORY, "settleCoin": SETTLE_COIN, "limit": "50"}
    )
    orders: list[dict] = result.get("list") or []
    return orders


async def get_instrument(http: httpx.AsyncClient, symbol: str) -> dict:
    """Lot-size filter for a symbol, cached: the exchange never changes it midday."""
    if symbol not in _instruments:
        result = await bybit_watch._get(
            http, "/v5/market/instruments-info", {"category": CATEGORY, "symbol": symbol}
        )
        items = result.get("list") or []
        if not items:
            raise RuntimeError(f"no instrument info for {symbol}")
        _instruments[symbol] = items[0]
    return _instruments[symbol]


async def amend_qty(http: httpx.AsyncClient, symbol: str, order_id: str, qty: Decimal) -> None:
    await bybit_watch._post(
        http,
        "/v5/order/amend",
        {"category": CATEGORY, "symbol": symbol, "orderId": order_id, "qty": str(qty)},
    )


def round_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def target_qty(order: dict, equity: Decimal, instrument: dict) -> Decimal | None:
    """Quantity that puts exactly RISK_PCT of equity between entry and stop."""
    symbol, order_id = order["symbol"], order["orderId"][:8]
    entry = Decimal(order["price"])
    if entry <= 0:
        return None

    stop_raw = order.get("stopLoss") or ""
    if stop_raw not in ("", "0"):
        stop = Decimal(stop_raw)
    elif FALLBACK_SL_PCT > 0:
        sign = 1 - FALLBACK_SL_PCT if order["side"] == "Buy" else 1 + FALLBACK_SL_PCT
        stop = entry * sign
        log.info("%s %s: no stop-loss, assuming %s", symbol, order_id, stop)
    else:
        log.info("%s %s: no stop-loss, skipping", symbol, order_id)
        return None

    distance = abs(entry - stop)
    if distance <= 0:
        log.warning("%s %s: stop equals entry, skipping", symbol, order_id)
        return None

    lot = instrument["lotSizeFilter"]
    qty = (equity * RISK_PCT) / distance
    qty = min(qty, (equity * MAX_LEVERAGE) / entry)  # notional ceiling
    qty = min(round_step(qty, Decimal(lot["qtyStep"])), Decimal(lot["maxOrderQty"]))

    if qty < Decimal(lot["minOrderQty"]):
        log.warning(
            "%s %s: risk-sized qty %s below exchange minimum, skipping", symbol, order_id, qty
        )
        return None
    return qty


def is_managed(order: dict) -> bool:
    """Only plain, untouched limit entries are ours to resize.

    A partial fill is left alone on purpose: Bybit amends the *total* quantity
    and rejects a total below what is already filled, so resizing would either
    fail or quietly change the risk on a position that is already half open.
    """
    if SYMBOLS and order["symbol"].upper() not in SYMBOLS:
        return False
    if order.get("orderType") != "Limit":
        return False
    if order.get("orderStatus") not in ("New", "Untriggered"):
        return False
    if order.get("reduceOnly"):
        return False
    if order.get("triggerPrice") not in ("", None):  # conditional order
        return False
    return not Decimal(order.get("cumExecQty") or 0) > 0


def has_settled(order: dict) -> bool:
    """True once entry and stop have held still for SETTLE_POLLS passes.

    Dragging a stop across the chart amends the order on every mouse step;
    chasing each intermediate value would burn the exchange's amend limit on
    prices that were only being passed through.
    """
    order_id = order["orderId"]
    signature = f"{order['price']}|{order.get('stopLoss') or ''}"
    seen, count = _settling.get(order_id, ("", 0))
    count = count + 1 if signature == seen else 1
    _settling[order_id] = (signature, count)
    return count >= SETTLE_POLLS


def rr_warning(order: dict) -> str | None:
    """A reward-to-risk complaint, when both exits are set and RR falls short.

    An order without a TP or SL cannot be measured and stays silent — the
    sizing path already reports a missing stop in its own way.
    """
    if not MIN_RR:
        return None
    entry = Decimal(order["price"])
    stop_raw = order.get("stopLoss") or ""
    tp_raw = order.get("takeProfit") or ""
    if stop_raw in ("", "0") or tp_raw in ("", "0"):
        return None
    risk = abs(entry - Decimal(stop_raw))
    if risk <= 0:
        return None
    rr = abs(Decimal(tp_raw) - entry) / risk
    if rr >= MIN_RR:
        return None
    return f"RR {rr:.2f} < {MIN_RR}"


# Keep this share of the margin headroom unused when capping — price can
# move between the check and the amend.
_MARGIN_SAFETY = Decimal("0.97")


async def margin_cap(http: httpx.AsyncClient, symbol: str, entry: Decimal, have: Decimal):
    """The largest quantity the free balance can carry, or None when unknown.

    The order's current quantity is already margined, so the cap is what is
    held now plus what the available balance buys at the symbol's exchange
    leverage. Unknown means "don't cap": worst case the amend is refused
    with the same 110007 the cap exists to avoid.
    """
    try:
        result = await bybit_watch._get(
            http, "/v5/account/wallet-balance", {"accountType": ACCOUNT_TYPE}
        )
        accounts = result.get("list") or []
        available = accounts[0].get("totalAvailableBalance") if accounts else ""
        if not available:
            return None
        result = await bybit_watch._get(
            http, "/v5/position/list", {"category": CATEGORY, "symbol": symbol}
        )
        rows = result.get("list") or []
        leverage = Decimal(rows[0].get("leverage") or "1") if rows else Decimal(1)
        return have + Decimal(available) * leverage / entry * _MARGIN_SAFETY
    # Deliberately broad: no cap is merely the pre-cap behaviour.
    except Exception:  # noqa: BLE001
        log.exception("no margin cap for %s", symbol)
        return None


async def tick(http: httpx.AsyncClient, send) -> None:
    """One pass: resize every managed, settled order that is the wrong size."""
    open_orders = await get_open_orders(http)
    # Orders that are gone — filled, cancelled, no longer ours — must not
    # keep their settle counters alive forever.
    live = {order["orderId"] for order in open_orders}
    for stale in _settling.keys() - live:
        del _settling[stale]
        _warned.pop(stale, None)

    orders = [order for order in open_orders if is_managed(order)]
    # has_settled() advances the counter, so it runs for every managed order
    # even once one of them is ready.
    orders = [order for order in orders if has_settled(order)]
    if not orders:
        return

    equity = await get_equity(http)
    for order in orders:
        symbol, order_id = order["symbol"], order["orderId"]
        complaint = rr_warning(order)
        signature = (
            f"{order['price']}|{order.get('stopLoss') or ''}|{order.get('takeProfit') or ''}"
        )
        if complaint and _warned.get(order_id) != signature:
            _warned[order_id] = signature
            await send(f"⚠️ {symbol} {order.get('side', '?')} limit @ {order['price']}: {complaint}")
        instrument = await get_instrument(http, symbol)
        want = target_qty(order, equity, instrument)
        if want is None:
            continue
        have = Decimal(order["qty"])
        capped = False
        if want > have:
            # Growing the order needs free margin; shrinking always fits.
            cap = await margin_cap(http, symbol, Decimal(order["price"]), have)
            if cap is not None and cap < want:
                lot = instrument["lotSizeFilter"]
                cap = round_step(cap, Decimal(lot["qtyStep"]))
                if cap <= have or cap < Decimal(lot["minOrderQty"]):
                    log.info("%s %s: no margin to grow, leaving as is", symbol, order_id[:8])
                    continue
                want, capped = cap, True
        if have == want:
            continue
        if DRY_RUN:
            log.info("[dry-run] %s %s: qty %s -> %s", symbol, order_id[:8], have, want)
            continue
        await amend_qty(http, symbol, order_id, want)
        log.info(
            "%s %s: qty %s -> %s%s", symbol, order_id[:8], have, want, " (capped)" if capped else ""
        )

        entry_price = Decimal(order["price"])

        def described(qty: Decimal, entry: Decimal = entry_price) -> str:
            value = qty * entry
            dollars = f"{value:,.0f}$" if value >= 10 else f"{value:,.2f}$"
            return f"{qty} ({dollars}, {value / equity * 100:.1f}% депо)"

        await send(
            f"⚖️ {symbol} {order.get('side', '?')} limit @ {order['price']}\n"
            f"stop {order.get('stopLoss') or '?'} → qty {described(have)} → {described(want)}"
            + (" (урезано по марже — подними плечо инструмента)" if capped else "")
        )


async def poll(http: httpx.AsyncClient, send) -> None:
    """Resize orders until cancelled. Never lets one failure end the loop."""
    mode = "dry-run" if DRY_RUN else "LIVE"
    log.info("sizing orders every %ss | risk %s%% | %s", POLL, RISK_PCT * 100, mode)
    await send(f"⚖️ sizer up — {mode}, risk {RISK_PCT * 100}%, max leverage {MAX_LEVERAGE}x")
    last_error = ""
    while True:
        try:
            await tick(http, send)
            last_error = ""
        except asyncio.CancelledError:
            raise
        # Deliberately broad: the exchange refusing a call or the network
        # blinking must not end the loop — and must not become a message
        # every three seconds either, so a repeat is announced once.
        except Exception as exc:  # noqa: BLE001
            log.exception("sizer pass failed")
            if str(exc) != last_error:
                last_error = str(exc)
                await send(f"⚖️ sizer error: {exc}")
        await asyncio.sleep(POLL)
