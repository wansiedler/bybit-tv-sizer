"""Auto-size the limit orders you draw on the TradingView chart.

Ported from wansiedler/bybit-tv-sizer into the relay: one process, one bot.
You place a limit order with any quantity through Bybit's broker panel on the
chart; this watcher rewrites the quantity so the distance between entry and
stop-loss always risks the same fixed percentage of account equity.

    DRY_RUN=1               # log and announce instead of amending
    RISK_PCT=0.5            # percent of the wallet balance between entry and stop
    RR_FULL_RISK=3          # RR at which the full percent applies; below, it shrinks
    RISK_EQUITY_MAX=0       # cap on the balance that percent is taken from
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

# relay.py imports this module before its own load_dotenv, same as speaker.
load_dotenv("bipboop")

DRY_RUN = os.getenv("DRY_RUN", "1").lower() not in ("0", "false", "no", "")
RISK_PCT = Decimal(os.getenv("RISK_PCT", "0.5")) / Decimal(100)
# Ceiling on the deposit the percent is taken from; 0 = no ceiling.
EQUITY_MAX = Decimal(os.getenv("RISK_EQUITY_MAX", "0"))
FALLBACK_SL_PCT = Decimal(os.getenv("FALLBACK_SL_PCT", "0")) / Decimal(100)
MAX_LEVERAGE = Decimal(os.getenv("MAX_LEVERAGE", "5"))
# The entry fills as a maker limit; the stop closes as a taker market order.
MAKER_FEE = Decimal(os.getenv("MAKER_FEE", "0.0002"))
TAKER_FEE = Decimal(os.getenv("TAKER_FEE", "0.00055"))
SYMBOLS = {s.strip().upper() for s in os.getenv("SYMBOLS", "").split(",") if s.strip()}
POLL = float(os.getenv("POLL_SEC", "3"))
CATEGORY = os.getenv("CATEGORY", "linear")
SETTLE_COIN = os.getenv("SETTLE_COIN", "USDT")
SETTLE_POLLS = int(os.getenv("SETTLE_POLLS", "2"))
ACCOUNT_TYPE = os.getenv("ACCOUNT_TYPE", "UNIFIED")

# Warn when a limit order's reward-to-risk sits below this. 0 disables.
MIN_RR = Decimal(os.getenv("MIN_RR", "2"))
# The reward-to-risk that earns the full RISK_PCT. A thinner RR scales the
# risk down in proportion: at RR_FULL_RISK=3, an RR 1.5 order risks half of
# RISK_PCT. 0 disables the scaling; an order without a take is unscaled.
RR_FULL_RISK = Decimal(os.getenv("RR_FULL_RISK", "3"))

# orderId -> (entry|stop signature, how many passes in a row have seen it)
_settling: dict[str, tuple[str, int]] = {}
_instruments: dict[str, dict] = {}
# orderId -> the entry|stop|tp signature already warned about, so dragging
# nothing does not repeat the same warning every pass.
_warned: dict[str, str] = {}
# orderId -> the quantity the exchange would not take. An amend the exchange
# has already refused is refused again on the next pass and the one after:
# at a half-second poll one stuck order filed 110007 until it was cancelled
# by hand. Asking once is diagnosis; asking forever is noise.
_refused: dict[str, Decimal] = {}


def enabled() -> bool:
    """Armed exactly like the original: the API key pair being present."""
    return bybit_watch.enabled()


async def get_equity(http: httpx.AsyncClient) -> Decimal:
    """The risk base: wallet balance, unrealised PnL excluded.

    The trimmer in bybit_watch measures against the same figure, so an
    order sized here fills into a position that is already the right size
    — sizing against equity would hand every fill straight to the knife
    whenever the open positions sit in profit.
    """
    result = await bybit_watch._get(
        http, "/v5/account/wallet-balance", {"accountType": ACCOUNT_TYPE}
    )
    accounts = result.get("list") or []
    if not accounts:
        raise RuntimeError("wallet-balance returned no accounts")
    balance = Decimal(accounts[0].get("totalWalletBalance") or accounts[0].get("totalEquity"))
    # RISK_EQUITY_MAX caps the base: past it the orders stop growing with
    # the account. The trimmer measures against the same ceiling.
    if EQUITY_MAX > 0:
        return min(balance, EQUITY_MAX)
    return balance


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


def reward_to_risk(order: dict) -> Decimal | None:
    """The order's RR from its price, stop and take; None when unmeasurable."""
    entry = Decimal(order["price"])
    stop_raw = order.get("stopLoss") or ""
    tp_raw = order.get("takeProfit") or ""
    if stop_raw in ("", "0") or tp_raw in ("", "0"):
        return None
    risk = abs(entry - Decimal(stop_raw))
    if risk <= 0:
        return None
    return abs(Decimal(tp_raw) - entry) / risk


def risk_share(order: dict) -> Decimal:
    """The share of equity this order may lose: RISK_PCT, scaled by its RR.

    Full RISK_PCT at RR_FULL_RISK and above; below it the share shrinks in
    proportion, so a thin setup costs less when it fails. Without a take the
    RR is unknown and the share is the plain RISK_PCT.
    """
    rr = reward_to_risk(order)
    if RR_FULL_RISK <= 0 or rr is None or rr >= RR_FULL_RISK:
        return RISK_PCT
    return RISK_PCT * rr / RR_FULL_RISK


def target_qty(order: dict, equity: Decimal, instrument: dict) -> Decimal | None:
    """Quantity whose stop-out costs exactly risk_share() of equity.

    The cost of stopping out is the price distance plus both fees — the
    maker entry and the stop's taker market close — so the realised loss
    stays inside the share, matching the trimmer's arithmetic.
    """
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
    qty = (equity * risk_share(order)) / (distance + entry * MAKER_FEE + stop * TAKER_FEE)
    # Notional ceiling. The entry fee is paid out of the same balance the
    # margin comes from, so a ceiling of exactly equity * MAX_LEVERAGE is one
    # the exchange can never fill at 1x: 100.0% of the deposit as margin
    # leaves nothing to pay the fee with, and the amend comes back 110007.
    qty = min(qty, (equity * MAX_LEVERAGE) / (entry * (1 + MAKER_FEE)))
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
    return Decimal(order.get("cumExecQty") or 0) <= 0


def has_settled(order: dict) -> bool:
    """True once entry, stop and take have held still for SETTLE_POLLS passes.

    Dragging a stop across the chart amends the order on every mouse step;
    chasing each intermediate value would burn the exchange's amend limit on
    prices that were only being passed through. The take counts too: it sets
    the RR, and the RR sets the risk share.
    """
    order_id = order["orderId"]
    signature = f"{order['price']}|{order.get('stopLoss') or ''}|{order.get('takeProfit') or ''}"
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
    rr = reward_to_risk(order)
    if rr is None or rr >= MIN_RR:
        return None
    return f"RR {rr:.2f} < {MIN_RR}"


# Keep this share of the margin headroom unused when capping — price can
# move between the check and the amend.
_MARGIN_SAFETY = Decimal("0.97")

# How far the target may drift before the order is worth rewriting, as a
# share of the order. At RISK_PCT 1% a thousandth of the quantity is a
# thousandth of a percent of the deposit at stake — below the resolution of
# every figure the bot reports.
_DEADBAND = Decimal("0.001")


def _free_from_coin(account: dict) -> str:
    """Free balance read off the settle coin's own row; "" when unreadable.

    `totalAvailableBalance` comes back empty on an isolated-margin account —
    Bybit fills the account-level margin fields only under cross margin, and
    an empty one used to mean "no cap", which let the sizer ask for a
    quantity the exchange refused on every pass for as long as the order
    rested. The coin row carries the same answer in parts: what the wallet
    holds, less what resting orders and open positions already hold against
    it.
    """
    for coin in account.get("coin") or []:
        if coin.get("coin") != SETTLE_COIN:
            continue
        wallet = Decimal(coin.get("walletBalance") or 0)
        held = Decimal(coin.get("totalOrderIM") or 0) + Decimal(coin.get("totalPositionIM") or 0)
        return str(wallet - held) if wallet > held else ""
    return ""


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
        account = accounts[0] if accounts else {}
        available = account.get("totalAvailableBalance") or _free_from_coin(account)
        if not available:
            return None
        result = await bybit_watch._get(
            http, "/v5/position/list", {"category": CATEGORY, "symbol": symbol}
        )
        rows = result.get("list") or []
        leverage = Decimal(rows[0].get("leverage") or "1") if rows else Decimal(1)
        # The safety margin applies to the whole position the cap allows, not
        # only to the part being added. Shaving the new part alone made the
        # cap a staircase: every pass exempted what the last one had already
        # placed, so one order climbed 18523.9 -> 18770.8 -> 18778.1 ->
        # 18778.3 in six seconds, four amends and four notices for a number
        # it was always going to reach. Applied to the total it is a fixed
        # point: amend once to it, and the next pass computes the same.
        return (have + Decimal(available) * leverage / entry) * _MARGIN_SAFETY
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
        _refused.pop(stale, None)

    orders = [order for order in open_orders if is_managed(order)]
    # has_settled() advances the counter, so it runs for every managed order
    # even once one of them is ready.
    orders = [order for order in orders if has_settled(order)]
    if not orders:
        return

    equity = await get_equity(http)
    by_symbol: dict[str, list[dict]] = {}
    for order in orders:
        by_symbol.setdefault(order["symbol"], []).append(order)
    for symbol, group in by_symbol.items():
        instrument = await get_instrument(http, symbol)
        wants = [(order, target_qty(order, equity, instrument)) for order in group]
        sized = [(order, qty) for order, qty in wants if qty is not None]
        # The cap is the symbol's, not each order's: two entries into one
        # idea must not risk twice over.
        scale = risk_scale(sized, equity)
        for order, qty in wants:
            await _resize_order(http, order, equity, qty if qty is None else qty * scale, send)


async def _warn_thin_rr(order: dict, send) -> None:
    """Complain once per (price, stop, take) signature about a thin RR."""
    complaint = rr_warning(order)
    signature = f"{order['price']}|{order.get('stopLoss') or ''}|{order.get('takeProfit') or ''}"
    if complaint and _warned.get(order["orderId"]) != signature:
        _warned[order["orderId"]] = signature
        await send(
            f"⚠️ {order['symbol']} {order.get('side', '?')} limit @ {order['price']}: {complaint}"
        )


async def _fit_margin(
    http: httpx.AsyncClient, order: dict, instrument: dict, want: Decimal, have: Decimal
) -> tuple[Decimal, bool] | None:
    """(quantity, was it capped) under the margin ceiling; None = leave it.

    Growing the order needs free margin; shrinking always fits.
    """
    if want <= have:
        return want, False
    cap = await margin_cap(http, order["symbol"], Decimal(order["price"]), have)
    if cap is None or cap >= want:
        return want, False
    lot = instrument["lotSizeFilter"]
    cap = round_step(cap, Decimal(lot["qtyStep"]))
    if cap <= have or cap < Decimal(lot["minOrderQty"]):
        log.info("%s %s: no margin to grow, leaving as is", order["symbol"], order["orderId"][:8])
        return None
    return cap, True


def order_risk(order: dict, qty: Decimal) -> Decimal:
    """What `qty` of this order loses if its stop fires, fees included."""
    entry = Decimal(order["price"])
    stop_raw = order.get("stopLoss") or ""
    if entry <= 0 or stop_raw in ("", "0"):
        return Decimal(0)
    stop = Decimal(stop_raw)
    return (abs(entry - stop) + entry * MAKER_FEE + stop * TAKER_FEE) * qty


def risk_scale(wants: list[tuple[dict, Decimal]], equity: Decimal) -> Decimal:
    """How much to shrink a symbol's orders so together they risk the cap.

    Each order is sized on its own to RISK_PCT, which is right for one entry
    and wrong for two: an idea entered in two places then risks twice. Two
    ONDO limits came to 1.68% of the deposit between them, each correct
    alone.

    One factor for the whole set, so the sizes drawn keep their proportions
    — 45,108 against 131,406 stays 45,108 against 131,406, scaled. Which of
    the two entries should be larger is not the bot's opinion to have.

    1 when the set already fits, and when there is nothing to measure: a
    cap cannot be shared out over orders whose risk is unknown.
    """
    total = sum((order_risk(order, qty) for order, qty in wants), Decimal(0))
    budget = equity * RISK_PCT
    if total <= 0 or budget <= 0 or total <= budget:
        return Decimal(1)
    return budget / total


async def _resize_order(
    http: httpx.AsyncClient, order: dict, equity: Decimal, want: Decimal | None, send
) -> None:
    """Fit one managed order to the target risk, margin and lot allowing."""
    symbol, order_id = order["symbol"], order["orderId"]
    await _warn_thin_rr(order, send)
    instrument = await get_instrument(http, symbol)
    if want is None:
        return
    want = round_step(want, Decimal(instrument["lotSizeFilter"]["qtyStep"]))
    have = Decimal(order["qty"])
    # Sizing down is the guardrail; sizing up is the bot taking a bigger
    # trade than the one that was drawn. A 0.27% stop asks for three
    # deposits to put 0.84% at risk, and the order drawn at 10,843$ came
    # back at 87,652$ — correct arithmetic, and not what was meant by it.
    # With RISK_GROW=0 an order risking less than the target is left alone.
    if not bybit_watch.RISK_GROW and want > have:
        log.info("%s %s: under target and growing is off, leaving %s", symbol, order_id[:8], have)
        return
    fitted = await _fit_margin(http, order, instrument, want, have)
    if fitted is None:
        return
    want, capped = fitted
    # Equity breathes with every open position's unrealised PnL, and that
    # walks the target across a rounding boundary and back: one ZEC order
    # was amended 370.28 -> .29 -> .28 -> .29 -> .28 in eleven seconds.
    #
    # A lot step alone is not the width. The jitter scales with the order,
    # the step does not: a 286,831 DOGE order wobbled by five units on a
    # step of one, and sailed straight through a deadband one step wide.
    # A thousandth of the order is wider than the breathing and far
    # narrower than any resize worth an amend — the DOGE order's real one
    # was +169%.
    step = Decimal(instrument["lotSizeFilter"]["qtyStep"])
    if abs(want - have) <= max(step, have * _DEADBAND):
        return
    if DRY_RUN:
        log.info("[dry-run] %s %s: qty %s -> %s", symbol, order_id[:8], have, want)
        return
    if _refused.get(order_id) == want:
        return
    try:
        await amend_qty(http, symbol, order_id, want)
    except Exception:
        # Remembered, not swallowed: the caller still announces it once. What
        # stops is asking the exchange the same refused question twice a
        # second until the order is cancelled by hand.
        _refused[order_id] = want
        raise
    _refused.pop(order_id, None)
    log.info(
        "%s %s: qty %s -> %s%s", symbol, order_id[:8], have, want, " (capped)" if capped else ""
    )
    await _announce_resize(order, equity, have, want, capped, send)


async def _announce_resize(
    order: dict, equity: Decimal, have: Decimal, want: Decimal, capped: bool, send
) -> None:
    """The ⚖️ notice: stop distance, RR-scaled risk, quantity before and after."""
    entry_price = Decimal(order["price"])
    share = risk_share(order)
    risk_text = f"risk {share * 100:.2f}%"
    if share != RISK_PCT:
        risk_text += f" (RR {reward_to_risk(order):.2f} of {RR_FULL_RISK:g})"

    def described(qty: Decimal) -> str:
        value = qty * entry_price
        dollars = f"{value:,.0f}$" if value >= 10 else f"{value:,.2f}$"
        return f"{qty} ({dollars}, {value / equity * 100:.1f}% depo)"

    stop_raw = order.get("stopLoss") or ""
    stop_text = stop_raw or "?"
    if stop_raw not in ("", "0"):
        distance_pct = abs(entry_price - Decimal(stop_raw)) / entry_price * 100
        stop_text += f" ({distance_pct:.2f}%)"
    await send(
        f"⚖️ {order['symbol']} {order.get('side', '?')} limit @ {order['price']}\n"
        f"stop {stop_text} | {risk_text}\n"
        f"qty {described(have)} → {described(want)}"
        + (" (capped by margin — raise the instrument's leverage)" if capped else "")
    )


async def poll(http: httpx.AsyncClient, send) -> None:
    """Resize orders until cancelled. Never lets one failure end the loop."""
    mode = "dry-run" if DRY_RUN else "LIVE"
    log.info("sizing orders every %ss | risk %s%% | %s", POLL, RISK_PCT * 100, mode)
    # The ceiling is worth announcing: it silently changes every size.
    ceiling = f", base ≤ {EQUITY_MAX:,.0f}$" if EQUITY_MAX > 0 else ""
    scaling = f" at RR ≥ {RR_FULL_RISK:g}, less below" if RR_FULL_RISK > 0 else ""
    await send(
        f"⚖️ sizer up — {mode}, risk {RISK_PCT * 100}%{scaling}{ceiling}, "
        f"max leverage {MAX_LEVERAGE}x"
    )
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
