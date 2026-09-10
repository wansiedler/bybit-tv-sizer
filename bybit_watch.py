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
import json
import logging
import os
import time
from dataclasses import dataclass

import httpx
from dotenv import load_dotenv

import chart
import sheets
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
# The hard risk manager: positions breaking these rules get market-closed.
# A breach is announced first and enforced only if it survives the grace
# window — enough time to set a stop or drop the leverage after an entry.
RISK_GUARD = os.getenv("RISK_GUARD", "1") == "1"
GUARD_MAX_LEVERAGE = float(os.getenv("MAX_LEVERAGE", "1"))
GUARD_GRACE = float(os.getenv("GUARD_GRACE_SEC", "45"))

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
    leverage: float = 0.0
    position_idx: int = 0


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
            leverage=float(row.get("leverage") or 0),
            position_idx=int(row.get("positionIdx") or 0),
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


# When each still-open violation was first noticed, by symbol.
_guard_seen: dict[str, float] = {}
# Symbols the guard itself market-closed, with the rule they broke: the
# journal row carries the reason.
_guard_closed: dict[str, str] = {}


def _guard_violation(position: Position) -> str | None:
    """The rule a position breaks, or None. Leverage 0 means Bybit sent none."""
    if position.leverage > GUARD_MAX_LEVERAGE:
        return f"плечо {position.leverage:g}x > {GUARD_MAX_LEVERAGE:g}x"
    if position.stop_loss is None:
        return "нет стопа"
    return None


async def guard(http: httpx.AsyncClient, open_now: dict[str, Position], send, speak) -> None:
    """The risk manager: warn about a rule breach, then market-close it.

    A breach gets one warning and the grace window to fix itself (set the
    stop, drop the leverage). If it is still there afterwards the position
    is closed with a reduce-only market order.
    """
    if not RISK_GUARD:
        return
    for symbol in list(_guard_seen):
        fixed = open_now.get(symbol)
        if fixed is None or _guard_violation(fixed) is None:
            del _guard_seen[symbol]
    for symbol, position in open_now.items():
        reason = _guard_violation(position)
        if reason is None:
            continue
        now = time.monotonic()
        first = _guard_seen.get(symbol)
        if first is None:
            _guard_seen[symbol] = now
            if GUARD_GRACE > 0:
                await send(
                    f"🛑 {base_symbol(symbol)}: {reason} — закрою маркетом через {GUARD_GRACE:.0f}с"
                )
                await speak(
                    f"{COIN_NAMES.get(base_symbol(symbol), base_symbol(symbol))} risk breach"
                )
                continue
        # A refused close retries after a full window (half a minute when the
        # grace is zero) instead of hammering the API every poll.
        elif now - first < (GUARD_GRACE or 30.0):
            continue
        else:
            _guard_seen[symbol] = now
        try:
            await _close_market(
                http,
                symbol,
                "Buy" if position.side == "long" else "Sell",
                f"{position.size:g}",
                position.position_idx,
            )
            _guard_closed[symbol] = reason
            await send(f"🛑 {base_symbol(symbol)} закрыт маркетом риск-менеджером: {reason}")
        # Deliberately broad: the guard must keep watching even when one
        # close is refused (margin mode quirks, min qty, hedged legs).
        except Exception as exc:  # noqa: BLE001
            log.exception("guard close failed for %s", symbol)
            await send(f"❌ {base_symbol(symbol)}: риск-менеджер не смог закрыть ({exc})")


async def last_price(http: httpx.AsyncClient, symbol: str) -> float | None:
    """The instrument's last traded price, or None when Bybit lists none."""
    result = await _get(http, "/v5/market/tickers", {"category": "linear", "symbol": symbol})
    rows = result.get("list") or []
    return float(rows[0]["lastPrice"]) if rows else None


async def _active_symbols(http: httpx.AsyncClient) -> set[str]:
    """Symbols with an open position or a live order."""
    symbols: set[str] = set()
    result = await _get(http, "/v5/position/list", {"category": "linear", "settleCoin": "USDT"})
    symbols |= {r["symbol"] for r in result.get("list", []) if float(r.get("size") or 0) != 0}
    result = await _get(http, "/v5/order/realtime", {"category": "linear", "settleCoin": "USDT"})
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
        return "Bybit не подключён: нет API-ключей"
    query = query.strip()
    if query and query.lower() not in ("all", "все", "всё"):
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
            return "Bybit не ответил, попробуй ещё раз"
    lines = []
    for symbol in active:
        verdict = await _cap_leverage(http, symbol)
        if verdict == "done":
            lines.append(f"✅ {base_symbol(symbol)} → 1x")
        elif verdict == "already":
            lines.append(f"· {base_symbol(symbol)} уже 1x")
        else:
            lines.append(f"❌ {base_symbol(symbol)}: {verdict}")
    if not active and not rest:
        return "Нет открытых позиций и ордеров — плечо ставить некому"
    if rest:
        done = already = failed = 0
        for symbol in rest:
            verdict = await _cap_leverage(http, symbol)
            if verdict == "done":
                done += 1
            elif verdict == "already":
                already += 1
            else:
                failed += 1
        lines.append(
            f"Остальные {len(rest)} инструментов: ✅ {done} · уже 1x {already} · ❌ {failed}"
        )
    return "\n".join(lines)


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
        return "Bybit не подключён: нет API-ключей"
    try:
        days = max(1, min(int(arg), 90)) if arg.strip() else 30
    except ValueError:
        return "Сколько дней? Например: /statistics 7"
    try:
        rows = await closed_history(http, days)
    # Deliberately broad: a chat command must answer, not crash the poller.
    except Exception:  # noqa: BLE001
        log.exception("stats history failed")
        return "Bybit не ответил, попробуй ещё раз"
    if not rows:
        return f"За {days} дн. закрытых сделок нет"
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
        f"📊 за {days} дн.: сделок {len(pnls)} · win {wins}/loss {len(pnls) - wins}"
        f" ({wins / len(pnls) * 100:.0f}%)\n"
        f"PnL <b>{_usd(total)}"
    )
    if depo:
        text += f" ({total / depo * 100:+.2f}% депо {depo:,.0f})"
    text += f"</b> · лучший {_usd(max(pnls))} · худший {_usd(min(pnls))}"

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
        return f"({amount / depo * 100:+.2f}%)" if depo else ""

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
        head = f"{arrow}{base_symbol(symbol)} {_val(position.value)}$@{position.price:g}"
        rr = None
        if position.stop_loss and position.take_profit:
            rr = abs(position.take_profit - position.price) / abs(
                position.price - position.stop_loss
            )
            head += f" | RR{rr:.2f}"
        exits = []
        at_sl = at_tp = None
        if position.stop_loss:
            # What the stop costs if it fires, fees included.
            at_sl = -abs(position.price - position.stop_loss) * position.size - fees
            exits.append(f"sl{position.stop_loss:g}:<b>{_usd(at_sl)}{share(at_sl)}</b>")
        if position.take_profit:
            sign = 1 if position.side == "long" else -1
            at_tp = sign * (position.take_profit - position.price) * position.size - fees
            tp_depo = f"=деп{depo + at_tp:,.2f}$" if depo else ""
            exits.append(f"tp{position.take_profit:g}:<b>{_usd(at_tp)}{share(at_tp)}{tp_depo}</b>")
        block = [head, *exits]
        block.append(
            f"PnL{position.unrealised:+,.2f}−комса{fees:.2f}=<b>{net:+,.2f}{share(net)}</b>"
        )
        total += position.unrealised
        total_net += net
        lines.append("\n".join(block))
        if send_album is not None:
            png = await entry_chart(
                http,
                symbol,
                position,
                entry_note=(
                    f"{_val(position.value)}$"
                    + (f" ({position.value / depo * 100:.1f}% depo)" if depo else "")
                    + (f" | RR {rr:.2f}" if rr is not None else "")
                    + f" | PnL {position.unrealised:+,.2f} - fee {fees:.2f}"
                    + f" = {net:+,.2f}{share(net)}"
                ),
                tp_note=(
                    f"{at_tp:+,.2f}{share(at_tp)}" + (f" = {depo + at_tp:,.2f}$" if depo else "")
                    if at_tp is not None
                    else ""
                ),
                sl_note=(
                    f"{at_sl:+,.2f}{share(at_sl)}" + (f" = {depo + at_sl:,.2f}$" if depo else "")
                    if at_sl is not None
                    else ""
                ),
            )
            if png is not None:
                pngs.append(png)
    # A total of one position would just repeat its line.
    if len(lines) > 1:
        lines.append(f"ΣPnL{_usd(total)}=<b>{_usd(total_net)}{share(total_net)}</b>")
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


async def entry_chart(
    http: httpx.AsyncClient,
    symbol: str,
    position: Position,
    entry_note: str = "",
    tp_note: str = "",
    sl_note: str = "",
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
            entry_note=entry_note,
            tp_note=tp_note,
            sl_note=sl_note,
        )
    # Deliberately broad: a chart is garnish, never worth losing the notice.
    except Exception:  # noqa: BLE001
        log.exception("no chart for %s", symbol)
        return None


async def close_chart(
    http: httpx.AsyncClient, symbol: str, was: Position, record: dict, exit_note: str = ""
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
            exit_note=exit_note,
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


def journal_row(
    symbol: str,
    was: Position,
    record: dict,
    pnl: float,
    depo: float | None = None,
    forced: str = "",
) -> list:
    """One trade as a row of the trading-diary sheet.

    Columns: Дата, Пара, Позиция, Результат, RR, Результат (R), Скрин,
    Объём $, Вход, Выход, PnL, % депо, Комиссии, Деп.
    The result figure is the R multiple — PnL over the risk the stop
    carried; without a stop it falls back to net USDT. Free-text columns
    stay empty for hand-written notes.
    """
    from datetime import datetime

    entry = float(record.get("avgEntryPrice") or was.price)
    opened_ms = record.get("createdTime")
    opened = datetime.fromtimestamp(int(opened_ms) / 1000).strftime("%d/%m/%Y") if opened_ms else ""
    rr = f"принудительно остановлено: {forced}" if forced else ""
    if was.stop_loss and was.take_profit:
        rr = f"1к{abs(was.take_profit - entry) / abs(entry - was.stop_loss):.1f}"
    risk = abs(entry - was.stop_loss) * was.size if was.stop_loss else 0.0
    fact: float = round(pnl / risk, 2) if risk else round(pnl, 2)
    # Columns: Дата, Пара, Позиция, Результат, RR, Результат (R), Скрин,
    # Объём $, Вход, Выход, PnL, % депо, Комиссии, Деп.
    opened_fee = float(record.get("openFee") or 0)
    closed_fee = float(record.get("closeFee") or 0)
    exit_price = float(record.get("avgExitPrice") or 0)
    return [
        opened,
        symbol,
        "Лонг" if was.side == "long" else "Шорт",
        "win" if pnl >= 0 else "stop",
        rr,
        fact,
        "",
        round(was.value, 2),
        entry,
        exit_price or "",
        round(pnl, 4),
        round(pnl / depo * 100, 2) if depo else "",
        f"{opened_fee:.4g}+{closed_fee:.4g}" if opened_fee or closed_fee else "",
        round(depo, 2) if depo else "",
    ]


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


def _usd(amount: float) -> str:
    """Signed money: cents normally, four decimals for dust below a dollar."""
    return f"{amount:+,.2f}" if abs(amount) >= 0.995 else f"{amount:+.4f}"


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


async def tick(
    http: httpx.AsyncClient, before: dict[str, Position] | None, send, speak, send_photo
) -> dict[str, Position]:
    """One poll: fetch, announce every change, hand back the new snapshot.

    The first call primes the snapshot silently — positions that were already
    open when the relay started are not news. An entry (or flip) goes out as
    a chart with the notice as its caption; everything else stays text.
    """
    after = await positions(http)
    await guard(http, after, send, speak)
    if before is None:
        return after

    def share(amount: float, depo: float | None) -> str:
        return f"({amount / depo * 100:+.2f}%)" if depo else ""

    for kind, symbol, was, now in diff(before, after):
        line, spoken_line = describe(kind, symbol, was, now)
        png = None
        if kind in ("opened", "flipped") and now is not None:
            fee = await entry_fee(http, symbol)
            depo = await equity(http)
            fees = 2 * (fee or TAKER_FEE * now.value)
            at_sl = target = rr = None
            if now.stop_loss is not None and now.take_profit is not None:
                rr = abs(now.take_profit - now.price) / abs(now.price - now.stop_loss)
                line += f" | RR{rr:.2f}"
            exits = []
            if now.stop_loss is not None:
                # What the stop costs if it fires, fees included.
                at_sl = -abs(now.price - now.stop_loss) * now.size - fees
                exits.append(f"sl{now.stop_loss:g}:<b>{_usd(at_sl)}{share(at_sl, depo)}</b>")
            if now.take_profit is not None:
                # What reaching the TP pays, net of both fees: the entry fee
                # just paid and a like-sized one for the exit.
                target = abs(now.take_profit - now.price) * now.size - fees
                tp_depo = f"=деп{depo + target:,.2f}$" if depo else ""
                exits.append(
                    f"tp{now.take_profit:g}:<b>{_usd(target)}{share(target, depo)}{tp_depo}</b>"
                )
            if exits:
                line += "\n" + "\n".join(exits)
            if fee:
                line += f"\nкомса{fee:.4g}"
            for warn in trade_warnings(now, depo):
                line += f"\n{warn}"
            png = await entry_chart(
                http,
                symbol,
                now,
                entry_note=(
                    f"{_val(now.value)}$"
                    + (f" ({now.value / depo * 100:.1f}% depo)" if depo else "")
                    + (f" | RR {rr:.2f}" if rr is not None else "")
                    + (f" | fee {fee:.2f}" if fee else "")
                ),
                tp_note=(
                    f"{target:+,.2f}{share(target, depo)}"
                    + (f" = {depo + target:,.2f}$" if depo else "")
                    if target is not None
                    else ""
                ),
                sl_note=(
                    f"{at_sl:+,.2f}{share(at_sl, depo)}"
                    + (f" = {depo + at_sl:,.2f}$" if depo else "")
                    if at_sl is not None
                    else ""
                ),
            )
        elif kind == "closed" and was is not None:
            record = await closed_record(http, symbol)
            if record is not None:
                # Bybit's closedPnl is already net of both fees.
                pnl = float(record["closedPnl"])
                depo = await equity(http)
                line += f"<b>{_usd(pnl)}{share(pnl, depo)}</b>"
                opened_fee = float(record.get("openFee") or 0)
                closed_fee = float(record.get("closeFee") or 0)
                if opened_fee or closed_fee:
                    line += f"-({opened_fee:.4g}+{closed_fee:.4g})"
                if depo:
                    line += f"=<b>{depo:,.2f}$</b>"
                spoken_line += f", {'profit' if pnl >= 0 else 'loss'} {abs(pnl):.0f}"
                total_fees = opened_fee + closed_fee
                png = await close_chart(
                    http,
                    symbol,
                    was,
                    record,
                    exit_note=(
                        f"PnL {pnl + total_fees:+,.2f} - fee {total_fees:.2f}"
                        f" = {pnl:+,.2f}{share(pnl, depo)}"
                    ),
                )
                forced = _guard_closed.pop(symbol, "")
                entry: dict = {"row": journal_row(symbol, was, record, pnl, depo, forced)}
                if png is not None:
                    # The Apps Script saves it to Drive and writes the link
                    # into the «Ссылка» column of the same row.
                    entry["png"] = base64.b64encode(png).decode()
                    entry["name"] = f"{base_symbol(symbol)}-{record.get('updatedTime', '')}"
                await sheets.log_close(http, entry)
        if png is None or not await send_photo(line, png):
            await send(line)
        await speak(spoken_line)
    return after


async def money_moves(http: httpx.AsyncClient) -> list[dict]:
    """Finished deposits, withdrawals and unified-account transfers, 7 days.

    Each move: an `id` stable across polls, a signed `amount` (into the
    trading account positive, out of it negative) and the `coin`.
    """
    since = str(int((time.time() - 7 * 86400) * 1000))
    moves: list[dict] = []
    result = await _get(http, "/v5/asset/deposit/query-record", {"startTime": since, "limit": "50"})
    for r in result.get("rows", []):
        if str(r.get("status")) == "3":  # 3 = success
            moves.append(
                {
                    "id": f"dep-{r.get('txID') or r.get('successAt')}",
                    "amount": float(r.get("amount") or 0),
                    "coin": r.get("coin", ""),
                }
            )
    result = await _get(
        http, "/v5/asset/withdraw/query-record", {"startTime": since, "limit": "50"}
    )
    for r in result.get("rows", []):
        if r.get("status") == "success":
            moves.append(
                {
                    "id": f"wd-{r.get('withdrawId')}",
                    "amount": -float(r.get("amount") or 0),
                    "coin": r.get("coin", ""),
                }
            )
    result = await _get(http, "/v5/asset/transfer/query-inter-transfer-list", {"limit": "50"})
    for r in result.get("list", []):
        into = r.get("toAccountType") == "UNIFIED"
        out_of = r.get("fromAccountType") == "UNIFIED"
        # Only finished transfers that cross the trading account's border.
        if r.get("status") != "SUCCESS" or into == out_of:
            continue
        moves.append(
            {
                "id": f"tr-{r.get('transferId')}",
                "amount": float(r.get("amount") or 0) * (1 if into else -1),
                "coin": r.get("coin", ""),
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
        word = "завел" if amount > 0 else "вывел"
        depo = await equity(http)
        line = f"💵 {word} {amount:+,.2f} {m['coin']}"
        if depo:
            line += f" · деп {depo:,.2f}$"
        await send(line)
        row = [
            datetime.now().strftime("%d/%m/%Y"),
            "перевод",
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
            "",
            round(depo, 2) if depo else "",
            round(amount, 2),
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
