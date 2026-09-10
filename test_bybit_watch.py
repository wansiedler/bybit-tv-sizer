"""Tests for the Bybit position watcher. No network: HTTP is faked wholesale."""

import asyncio
from typing import Any

import pytest

import bybit_watch
from bybit_watch import Position


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setattr(bybit_watch, "API_KEY", "key")
    monkeypatch.setattr(bybit_watch, "API_SECRET", "secret")


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class FakeHTTP:
    """Answers /v5/position/list and /v5/closed-pnl from scripted payloads."""

    def __init__(self):
        self.position_pages: list[list[dict[str, Any]]] = []
        self.pnl_rows: list[dict[str, Any]] = []
        self.kline_rows: list[list[str]] = []
        self.exec_rows: list[dict[str, Any]] = []
        self.equity_rows: list[dict[str, Any]] = []
        self.deposit_rows: list[dict[str, Any]] = []
        self.withdraw_rows: list[dict[str, Any]] = []
        self.transfer_rows: list[dict[str, Any]] = []
        self.order_rows: list[dict[str, Any]] = []
        self.instrument_pages: list[dict[str, Any]] = []
        self.ticker_rows: list[dict[str, Any]] = []
        self.history_rows: list[dict[str, Any]] = []
        self.requests: list[str] = []

    async def get(self, url, headers=None, timeout=None):
        self.requests.append(url)
        if "/v5/position/list" in url:
            rows = self.position_pages.pop(0) if self.position_pages else []
            return FakeResponse({"retCode": 0, "result": {"list": rows}})
        if "/v5/market/kline" in url:
            return FakeResponse({"retCode": 0, "result": {"list": self.kline_rows}})
        if "/v5/execution/list" in url:
            return FakeResponse({"retCode": 0, "result": {"list": self.exec_rows}})
        if "/v5/account/wallet-balance" in url:
            return FakeResponse({"retCode": 0, "result": {"list": self.equity_rows}})
        if "/v5/order/history" in url:
            return FakeResponse({"retCode": 0, "result": {"list": self.history_rows}})
        if "/v5/market/tickers" in url:
            return FakeResponse({"retCode": 0, "result": {"list": self.ticker_rows}})
        if "/v5/market/instruments-info" in url:
            page = self.instrument_pages.pop(0) if self.instrument_pages else {"list": []}
            return FakeResponse({"retCode": 0, "result": page})
        if "/v5/order/realtime" in url:
            return FakeResponse({"retCode": 0, "result": {"list": self.order_rows}})
        if "/v5/asset/deposit/query-record" in url:
            return FakeResponse({"retCode": 0, "result": {"rows": self.deposit_rows}})
        if "/v5/asset/withdraw/query-record" in url:
            return FakeResponse({"retCode": 0, "result": {"rows": self.withdraw_rows}})
        if "/v5/asset/transfer/query-inter-transfer-list" in url:
            return FakeResponse({"retCode": 0, "result": {"list": self.transfer_rows}})
        return FakeResponse({"retCode": 0, "result": {"list": self.pnl_rows}})


def row(
    symbol="FARTCOINUSDT",
    side="Buy",
    size="115661.3",
    price="0.1621",
    value="18748.7",
    tp="",
    sl="",
    leverage="",
    position_idx="",
):
    return {
        "symbol": symbol,
        "side": side,
        "size": size,
        "avgPrice": price,
        "positionValue": value,
        "takeProfit": tp,
        "stopLoss": sl,
        "leverage": leverage,
        "positionIdx": position_idx,
    }


# --------------------------------------------------------------------------- #
#  enabled / signature                                                         #
# --------------------------------------------------------------------------- #
def test_enabled_needs_both_keys(keyed, monkeypatch):
    assert bybit_watch.enabled() is True
    monkeypatch.setattr(bybit_watch, "API_SECRET", "")
    assert bybit_watch.enabled() is False


def test_sign_is_hmac_over_the_v5_payload(keyed):
    # Vector computed independently: HMAC-SHA256("secret", "1700000000000key5000a=1")
    assert (
        bybit_watch.sign("1700000000000", "a=1")
        == "d026b6d817e30bf57f231da2f2e4c6cbb5b776a49bc545459332eaabf806ca3a"  # pragma: allowlist secret
    )


# --------------------------------------------------------------------------- #
#  positions parsing                                                           #
# --------------------------------------------------------------------------- #
def test_positions_parses_open_longs_and_shorts(keyed):
    http = FakeHTTP()
    http.position_pages = [[row(), row(symbol="OPUSDT", side="Sell", size="10", value="1.2")]]

    got = asyncio.run(bybit_watch.positions(http))

    assert got["FARTCOINUSDT"] == Position("long", 115661.3, 0.1621, 18748.7)
    assert got["OPUSDT"].side == "short"


def test_positions_drops_zero_sizes(keyed):
    http = FakeHTTP()
    http.position_pages = [[row(size="0")]]

    assert asyncio.run(bybit_watch.positions(http)) == {}


def test_positions_parses_leverage_and_position_index(keyed):
    http = FakeHTTP()
    http.position_pages = [[row(leverage="3", position_idx="1")]]

    got = asyncio.run(bybit_watch.positions(http))

    assert got["FARTCOINUSDT"].leverage == 3.0
    assert got["FARTCOINUSDT"].position_idx == 1


def test_price_before_reads_the_previous_minute_close(keyed):
    http = FakeHTTP()
    http.kline_rows = [
        ["1700000060000", "2440", "2450", "2430", "2431", "1", "1"],  # forming
        ["1700000000000", "2500", "2510", "2490", "2500.5", "1", "1"],  # done
    ]

    assert asyncio.run(bybit_watch.price_before(http, "ETHUSDT")) == 2500.5


def test_price_before_is_none_without_history(keyed):
    assert asyncio.run(bybit_watch.price_before(FakeHTTP(), "NOPEUSDT")) is None


def test_get_raises_on_a_bybit_refusal(keyed):
    class Refusing(FakeHTTP):
        async def get(self, url, headers=None, timeout=None):
            return FakeResponse({"retCode": 10003, "retMsg": "API key is invalid."})

    with pytest.raises(RuntimeError, match="10003"):
        asyncio.run(bybit_watch.positions(Refusing()))


# --------------------------------------------------------------------------- #
#  diff / describe                                                             #
# --------------------------------------------------------------------------- #
LONG = Position("long", 100.0, 0.16, 16.0)
BIGGER = Position("long", 200.0, 0.16, 32.0)
SHORT = Position("short", 100.0, 0.16, 16.0)


def test_diff_sees_every_kind_of_change():
    before = {"AUSDT": LONG, "BUSDT": LONG, "CUSDT": LONG}
    after = {"BUSDT": BIGGER, "CUSDT": SHORT, "DUSDT": LONG}

    kinds = {(kind, symbol) for kind, symbol, _, _ in bybit_watch.diff(before, after)}

    assert kinds == {
        ("closed", "AUSDT"),
        ("changed", "BUSDT"),
        ("flipped", "CUSDT"),
        ("opened", "DUSDT"),
    }


def test_diff_quiet_when_nothing_moved():
    assert bybit_watch.diff({"AUSDT": LONG}, {"AUSDT": LONG}) == []


@pytest.mark.parametrize(
    ("kind", "was", "now", "line", "spoken"),
    [
        (
            "opened",
            None,
            Position("long", 115661.3, 0.1621, 18748.7),
            "💰📈FARTCOIN 18,749$@0.1621",
            "Fartcoin long opened",
        ),
        (
            "opened",
            None,
            Position("short", 100.0, 0.16, 16.0, stop_loss=0.17),
            "💰📉FARTCOIN 16$@0.16",
            "Fartcoin short opened",
        ),
        ("closed", LONG, None, "💸📈FARTCOIN", "Fartcoin long closed"),
        (
            "changed",
            LONG,
            BIGGER,
            "💰📈FARTCOIN 16→32",
            "Fartcoin long increased",
        ),
        (
            "changed",
            BIGGER,
            LONG,
            "💰📈FARTCOIN 32→16",
            "Fartcoin long reduced",
        ),
        (
            "flipped",
            LONG,
            SHORT,
            "💰FARTCOIN → short 16@0.16",
            "Fartcoin flipped to short",
        ),
    ],
)
def test_describe_reads_like_a_human(kind, was, now, line, spoken):
    assert bybit_watch.describe(kind, "FARTCOINUSDT", was, now) == (line, spoken)


# --------------------------------------------------------------------------- #
#  tick                                                                        #
# --------------------------------------------------------------------------- #
class Recorder:
    def __init__(self):
        self.sent: list[str] = []
        self.spoken: list[str] = []
        self.photos: list[str] = []
        self.photo_ok = True

    async def send(self, text):
        self.sent.append(text)

    async def speak(self, text):
        self.spoken.append(text)
        return True

    async def send_photo(self, caption, png):
        assert png.startswith(b"\x89PNG")
        self.photos.append(caption)
        return self.photo_ok


def test_first_tick_primes_silently(keyed):
    http, out = FakeHTTP(), Recorder()
    http.position_pages = [[row()]]

    snapshot = asyncio.run(bybit_watch.tick(http, None, out.send, out.speak, out.send_photo))

    assert "FARTCOINUSDT" in snapshot
    assert out.sent == []


def test_tick_announces_an_open(keyed):
    http, out = FakeHTTP(), Recorder()
    http.position_pages = [[row()]]

    asyncio.run(bybit_watch.tick(http, {}, out.send, out.speak, out.send_photo))

    assert out.sent == ["💰📈FARTCOIN 18,749$@0.1621\n⚠️ без стопа"]
    assert out.spoken == ["Fartcoin long opened"]


KLINES = [
    ["1700000900000", "0.163", "0.170", "0.161", "0.169", "1", "1"],
    ["1700000000000", "0.160", "0.165", "0.158", "0.163", "1", "1"],
]


WITH_STOP = Position("long", 100.0, 0.16, 16.0, take_profit=0.19, stop_loss=0.15)


def test_trade_warnings_flags_a_missing_stop():
    assert bybit_watch.trade_warnings(LONG, 1000.0) == ["⚠️ без стопа"]


def test_trade_warnings_quiet_on_a_disciplined_trade(monkeypatch):
    # Risk |0.16-0.15|*100 = 1 USDT on a 200 USDT depo = 0.5%, RR 3.
    assert bybit_watch.trade_warnings(WITH_STOP, 200.0) == []


def test_trade_warnings_flags_risk_drift():
    # 1 USDT of risk on 1000 USDT is 0.1% — far from the 0.5% target.
    assert bybit_watch.trade_warnings(WITH_STOP, 1000.0) == ["⚠️ риск 0.10% депо, цель 0.5%"]


def test_trade_warnings_flags_a_thin_rr():
    thin = Position("long", 100.0, 0.16, 16.0, take_profit=0.17, stop_loss=0.15)

    assert bybit_watch.trade_warnings(thin, 200.0) == ["⚠️ RR 1.00 < 2"]


def test_trade_warnings_skip_without_depo_target_or_tp(monkeypatch):
    # No equity figure and no TP: nothing measurable, nothing said.
    no_tp = Position("long", 100.0, 0.16, 16.0, stop_loss=0.15)
    assert bybit_watch.trade_warnings(no_tp, None) == []

    # Both checks disabled by configuration.
    monkeypatch.setattr(bybit_watch, "RISK_TARGET", 0.0)
    monkeypatch.setattr(bybit_watch, "MIN_RR", 0.0)
    assert bybit_watch.trade_warnings(WITH_STOP, 1000.0) == []


# --------------------------------------------------------------------------- #
#  /stopall                                                                    #
# --------------------------------------------------------------------------- #
@pytest.fixture
def disarmed(monkeypatch):
    monkeypatch.setattr(bybit_watch, "_stopall_armed", 0.0)


def test_stopall_without_keys(monkeypatch, disarmed):
    monkeypatch.setattr(bybit_watch, "API_KEY", "")

    assert "нет API-ключей" in asyncio.run(bybit_watch.close_everything(FakeHTTP()))


def test_stopall_survives_a_dead_api(keyed, disarmed, caplog):
    class Refusing(FakeHTTP):
        async def get(self, url, headers=None, timeout=None):
            raise OSError("bybit down")

    with caplog.at_level("ERROR", logger="relay.bybit"):
        assert "не ответил" in asyncio.run(bybit_watch.close_everything(Refusing()))


def test_stopall_with_nothing_open_disarms(keyed, monkeypatch):
    monkeypatch.setattr(bybit_watch, "_stopall_armed", 1e12)  # was armed
    http = FakeHTTP()
    http.position_pages = [[]]

    assert "закрывать нечего" in asyncio.run(bybit_watch.close_everything(http))
    assert bybit_watch._stopall_armed == 0.0


class ClosingHTTP(FakeHTTP):
    """FakeHTTP that also records order-create posts."""

    def __init__(self):
        super().__init__()
        self.orders: list[dict] = []
        self.refuse_order = False

    async def post(self, url, content=None, headers=None, timeout=None):
        import json as _json

        if self.refuse_order:
            raise OSError("order rejected")
        self.orders.append(_json.loads(content))
        return FakeResponse({"retCode": 0, "result": {}})


# --------------------------------------------------------------------------- #
#  risk guard                                                                  #
# --------------------------------------------------------------------------- #
NAKED = Position("long", 1.4, 91.25, 128.0)
LEVERED = Position("short", 1.0, 90.0, 90.0, stop_loss=92.0, leverage=3.0)
COVERED = Position("long", 1.4, 91.25, 128.0, stop_loss=90.0)


@pytest.fixture
def guarding(keyed, monkeypatch):
    monkeypatch.setattr(bybit_watch, "RISK_GUARD", True)


def test_guard_warns_once_and_waits_out_the_grace_window(guarding):
    http, out = ClosingHTTP(), Recorder()

    asyncio.run(bybit_watch.guard(http, {"CLUSDT": NAKED}, out.send, out.speak))
    asyncio.run(bybit_watch.guard(http, {"CLUSDT": NAKED}, out.send, out.speak))

    assert out.sent == ["🛑 CL: нет стопа — закрою маркетом через 45с"]
    assert out.spoken == ["CL risk breach"]
    assert http.orders == []


def test_guard_flags_leverage_above_the_cap(guarding):
    http, out = ClosingHTTP(), Recorder()

    asyncio.run(bybit_watch.guard(http, {"OPUSDT": LEVERED}, out.send, out.speak))

    assert out.sent == ["🛑 OP: плечо 3x > 1x — закрою маркетом через 45с"]


def test_guard_market_closes_instantly_without_a_grace_window(guarding, monkeypatch):
    monkeypatch.setattr(bybit_watch, "GUARD_GRACE", 0.0)
    http, out = ClosingHTTP(), Recorder()

    asyncio.run(bybit_watch.guard(http, {"CLUSDT": NAKED}, out.send, out.speak))

    assert http.orders == [
        {
            "category": "linear",
            "symbol": "CLUSDT",
            "side": "Sell",
            "orderType": "Market",
            "qty": "1.4",
            "reduceOnly": True,
            "positionIdx": 0,
        }
    ]
    assert out.sent[-1] == "🛑 CL закрыт маркетом риск-менеджером: нет стопа"


def test_guard_closes_once_the_grace_window_has_passed(guarding):
    http, out = ClosingHTTP(), Recorder()

    asyncio.run(bybit_watch.guard(http, {"CLUSDT": NAKED}, out.send, out.speak))
    bybit_watch._guard_seen["CLUSDT"] -= 60.0  # pretend the window elapsed
    asyncio.run(bybit_watch.guard(http, {"CLUSDT": NAKED}, out.send, out.speak))

    assert len(http.orders) == 1
    assert out.sent[-1] == "🛑 CL закрыт маркетом риск-менеджером: нет стопа"
    assert bybit_watch._guard_closed["CLUSDT"] == "нет стопа"


def test_guard_forgives_a_fixed_position(guarding):
    http, out = ClosingHTTP(), Recorder()

    asyncio.run(bybit_watch.guard(http, {"CLUSDT": NAKED}, out.send, out.speak))
    asyncio.run(bybit_watch.guard(http, {"CLUSDT": COVERED}, out.send, out.speak))
    asyncio.run(bybit_watch.guard(http, {"CLUSDT": NAKED}, out.send, out.speak))

    # Two warnings: the fix in between reset the breach timer.
    assert len(out.sent) == 2
    assert http.orders == []


def test_guard_forgets_a_position_that_closed_itself(guarding):
    http, out = ClosingHTTP(), Recorder()

    asyncio.run(bybit_watch.guard(http, {"CLUSDT": NAKED}, out.send, out.speak))
    asyncio.run(bybit_watch.guard(http, {}, out.send, out.speak))

    assert bybit_watch._guard_seen == {}


def test_guard_reports_a_refused_close_and_backs_off(guarding, monkeypatch):
    monkeypatch.setattr(bybit_watch, "GUARD_GRACE", 0.0)
    http, out = ClosingHTTP(), Recorder()
    http.refuse_order = True

    asyncio.run(bybit_watch.guard(http, {"CLUSDT": NAKED}, out.send, out.speak))
    asyncio.run(bybit_watch.guard(http, {"CLUSDT": NAKED}, out.send, out.speak))

    # One attempt, one complaint: the retry waits out the backoff window.
    assert out.sent == ["❌ CL: риск-менеджер не смог закрыть (order rejected)"]


def naked_order(order_id="n1", symbol="CLUSDT", **extra):
    return {"orderId": order_id, "symbol": symbol, "stopLoss": "", **extra}


def test_guard_cancels_a_naked_limit_instantly_without_grace(guarding, monkeypatch):
    monkeypatch.setattr(bybit_watch, "GUARD_GRACE", 0.0)
    http, out = ClosingHTTP(), Recorder()
    http.order_rows = [naked_order()]

    asyncio.run(bybit_watch.guard(http, {}, out.send, out.speak))

    assert http.orders == [{"category": "linear", "symbol": "CLUSDT", "orderId": "n1"}]
    assert out.sent == ["🛑 CL: лимитка без стопа отменена риск-менеджером"]


def test_guard_warns_about_a_naked_limit_inside_the_grace(guarding):
    http, out = ClosingHTTP(), Recorder()
    http.order_rows = [naked_order()]

    asyncio.run(bybit_watch.guard(http, {}, out.send, out.speak))

    assert out.sent == ["🛑 CL: лимитка без стопа — отменю через 45с"]
    assert out.spoken == ["CL naked order"]
    assert http.orders == []


def test_guard_cancels_once_the_order_grace_passed(guarding):
    http, out = ClosingHTTP(), Recorder()
    http.order_rows = [naked_order()]

    asyncio.run(bybit_watch.guard(http, {}, out.send, out.speak))
    bybit_watch._guard_seen["order:n1"] -= 60.0
    http.order_rows = [naked_order()]
    asyncio.run(bybit_watch.guard(http, {}, out.send, out.speak))

    assert len(http.orders) == 1
    assert out.sent[-1] == "🛑 CL: лимитка без стопа отменена риск-менеджером"


def test_guard_waits_out_the_order_grace(guarding):
    http, out = ClosingHTTP(), Recorder()
    http.order_rows = [naked_order()]

    asyncio.run(bybit_watch.guard(http, {}, out.send, out.speak))
    asyncio.run(bybit_watch.guard(http, {}, out.send, out.speak))

    assert len(out.sent) == 1  # one warning, no cancel yet
    assert http.orders == []


def test_guard_leaves_exits_and_stopped_orders_alone(guarding, monkeypatch):
    monkeypatch.setattr(bybit_watch, "GUARD_GRACE", 0.0)
    http, out = ClosingHTTP(), Recorder()
    http.order_rows = [
        naked_order("r1", reduceOnly=True),
        naked_order("c1", closeOnTrigger=True),
        naked_order("s1", stopOrderType="StopLoss"),
        naked_order("ok1", stopLoss="91.0"),
    ]

    asyncio.run(bybit_watch.guard(http, {}, out.send, out.speak))

    assert out.sent == []
    assert http.orders == []


def test_guard_forgets_a_cancelled_or_fixed_order(guarding):
    http, out = ClosingHTTP(), Recorder()
    http.order_rows = [naked_order()]

    asyncio.run(bybit_watch.guard(http, {}, out.send, out.speak))
    http.order_rows = []
    asyncio.run(bybit_watch.guard(http, {}, out.send, out.speak))

    assert "order:n1" not in bybit_watch._guard_seen


def test_guard_reports_a_refused_cancel(guarding, monkeypatch):
    monkeypatch.setattr(bybit_watch, "GUARD_GRACE", 0.0)
    http, out = ClosingHTTP(), Recorder()
    http.order_rows = [naked_order()]
    http.refuse_order = True

    asyncio.run(bybit_watch.guard(http, {}, out.send, out.speak))

    assert out.sent == ["❌ CL: не смог отменить лимитку (order rejected)"]


def test_guard_position_rules_survive_a_dead_order_listing(guarding, monkeypatch, caplog):
    monkeypatch.setattr(bybit_watch, "GUARD_GRACE", 0.0)

    class NoOrders(ClosingHTTP):
        async def get(self, url, headers=None, timeout=None):
            if "/v5/order/realtime" in url:
                raise OSError("down")
            return await super().get(url, headers=headers, timeout=timeout)

    http, out = NoOrders(), Recorder()

    with caplog.at_level("ERROR", logger="relay.bybit"):
        asyncio.run(bybit_watch.guard(http, {"CLUSDT": NAKED}, out.send, out.speak))

    assert "guard order listing failed" in caplog.text
    assert out.sent[-1] == "🛑 CL закрыт маркетом риск-менеджером: нет стопа"


def test_guard_stays_dormant_when_disabled(keyed):
    http, out = ClosingHTTP(), Recorder()

    asyncio.run(bybit_watch.guard(http, {"CLUSDT": NAKED}, out.send, out.speak))

    assert out.sent == []
    assert bybit_watch._guard_seen == {}


# --------------------------------------------------------------------------- #
#  force leverage 1x                                                           #
# --------------------------------------------------------------------------- #
def test_lev1_needs_keys():
    assert (
        asyncio.run(bybit_watch.force_leverage_one(FakeHTTP()))
        == "Bybit не подключён: нет API-ключей"
    )


def test_lev1_caps_positions_and_orders(keyed):
    http = ClosingHTTP()
    http.position_pages = [[row()]]
    http.order_rows = [{"symbol": "OPUSDT"}]

    text = asyncio.run(bybit_watch.force_leverage_one(http))

    assert text == "✅ FARTCOIN → 1x\n✅ OP → 1x"
    assert [o["symbol"] for o in http.orders] == ["FARTCOINUSDT", "OPUSDT"]
    assert http.orders[0]["buyLeverage"] == "1" and http.orders[0]["sellLeverage"] == "1"


def test_lev1_sweeps_the_rest_of_the_exchange_after_the_actives(keyed):
    class Mixed(ClosingHTTP):
        async def post(self, url, content=None, headers=None, timeout=None):
            import json as _json

            body = _json.loads(content)
            if body["symbol"] == "AUSDT":
                raise RuntimeError("110043 leverage not modified")
            if body["symbol"] == "BUSDT":
                raise OSError("margin mode conflict")
            return await super().post(url, content=content, headers=headers, timeout=timeout)

    http = Mixed()
    http.position_pages = [[row()]]
    http.order_rows = []
    http.instrument_pages = [
        {
            "list": [{"symbol": "AUSDT", "settleCoin": "USDT", "status": "Trading"}],
            "nextPageCursor": "page2",
        },
        {
            "list": [
                {"symbol": "BUSDT", "settleCoin": "USDT", "status": "Trading"},
                {"symbol": "CUSDT", "settleCoin": "USDT", "status": "Trading"},
                {"symbol": "DUSDC", "settleCoin": "USDC", "status": "Trading"},
                {"symbol": "EUSDT", "settleCoin": "USDT", "status": "Closed"},
                {"symbol": "FARTCOINUSDT", "settleCoin": "USDT", "status": "Trading"},
            ],
        },
    ]

    text = asyncio.run(bybit_watch.force_leverage_one(http))

    assert text == ("✅ FARTCOIN → 1x\nОстальные 3 инструментов: ✅ 1 · уже 1x 1 · ❌ 1")


def test_lev1_takes_a_bare_ticker(keyed):
    http = ClosingHTTP()

    text = asyncio.run(bybit_watch.force_leverage_one(http, "cl"))

    assert text == "✅ CL → 1x"
    assert http.orders[0]["symbol"] == "CLUSDT"


def test_lev1_reports_an_already_capped_symbol(keyed):
    class Already(ClosingHTTP):
        async def post(self, url, content=None, headers=None, timeout=None):
            raise RuntimeError("bybit /v5/position/set-leverage: 110043 leverage not modified")

    text = asyncio.run(bybit_watch.force_leverage_one(Already(), "CLUSDT"))

    assert text == "· CL уже 1x"


def test_lev1_reports_a_refusal(keyed):
    http = ClosingHTTP()
    http.refuse_order = True

    text = asyncio.run(bybit_watch.force_leverage_one(http, "CL"))

    assert text.startswith("❌ CL:")


def test_lev1_with_nothing_open_says_so(keyed):
    http = ClosingHTTP()
    http.position_pages = [[]]

    text = asyncio.run(bybit_watch.force_leverage_one(http))

    assert "некому" in text
    assert http.orders == []


def test_lev1_survives_a_listing_failure(keyed):
    class Refusing(FakeHTTP):
        async def get(self, url, headers=None, timeout=None):
            raise OSError("down")

    text = asyncio.run(bybit_watch.force_leverage_one(Refusing()))

    assert text == "Bybit не ответил, попробуй ещё раз"


def test_stopall_arms_first_and_places_nothing(keyed, disarmed):
    http = ClosingHTTP()
    http.position_pages = [[row(), row(symbol="OPUSDT", side="Sell", size="10")]]

    text = asyncio.run(bybit_watch.close_everything(http))

    assert "Закрою МАРКЕТОМ 2 поз.: FARTCOIN, OP" in text
    assert "Повтори /stopall" in text
    assert http.orders == []


def test_stopall_confirmed_closes_reduce_only(keyed, disarmed):
    http = ClosingHTTP()
    http.position_pages = [
        [row(), row(symbol="OPUSDT", side="Sell", size="10")],
        [row(), row(symbol="OPUSDT", side="Sell", size="10")],
    ]

    asyncio.run(bybit_watch.close_everything(http))  # arm
    text = asyncio.run(bybit_watch.close_everything(http))  # fire

    assert text.count("✅") == 2
    assert "Отчёты 💸" in text
    assert http.orders == [
        {
            "category": "linear",
            "symbol": "FARTCOINUSDT",
            "side": "Sell",  # closes the long
            "orderType": "Market",
            "qty": "115661.3",
            "reduceOnly": True,
            "positionIdx": 0,
        },
        {
            "category": "linear",
            "symbol": "OPUSDT",
            "side": "Buy",  # closes the short
            "orderType": "Market",
            "qty": "10",
            "reduceOnly": True,
            "positionIdx": 0,
        },
    ]
    assert bybit_watch._stopall_armed == 0.0  # spent, next call re-arms


def test_stopall_confirmation_expires(keyed, disarmed, monkeypatch):
    # A zero-length window: by the second call the confirmation has lapsed.
    monkeypatch.setattr(bybit_watch, "_STOPALL_WINDOW", 0.0)
    http = ClosingHTTP()
    http.position_pages = [[row()], [row()]]

    asyncio.run(bybit_watch.close_everything(http))
    text = asyncio.run(bybit_watch.close_everything(http))

    assert "Повтори /stopall" in text  # re-armed instead of firing
    assert http.orders == []


def test_stopall_reports_a_refused_close(keyed, disarmed, caplog):
    http = ClosingHTTP()
    http.refuse_order = True
    http.position_pages = [[row()], [row()]]

    asyncio.run(bybit_watch.close_everything(http))
    with caplog.at_level("ERROR", logger="relay.bybit"):
        text = asyncio.run(bybit_watch.close_everything(http))

    assert "❌ FARTCOIN" in text
    assert "could not close" in caplog.text


# --------------------------------------------------------------------------- #
#  /close <ticker>                                                             #
# --------------------------------------------------------------------------- #
def test_close_without_keys(monkeypatch):
    monkeypatch.setattr(bybit_watch, "API_KEY", "")

    assert "нет API-ключей" in asyncio.run(bybit_watch.close_position(FakeHTTP(), "CL"))


def test_close_without_a_ticker_explains_itself(keyed):
    assert "/close CL" in asyncio.run(bybit_watch.close_position(FakeHTTP(), "  "))


def test_close_survives_a_dead_api(keyed, caplog):
    class Refusing(FakeHTTP):
        async def get(self, url, headers=None, timeout=None):
            raise OSError("bybit down")

    with caplog.at_level("ERROR", logger="relay.bybit"):
        assert "не ответил" in asyncio.run(bybit_watch.close_position(Refusing(), "CL"))


def test_close_unknown_ticker_lists_the_open_ones(keyed):
    http = ClosingHTTP()
    http.position_pages = [[row()]]

    text = asyncio.run(bybit_watch.close_position(http, "CL"))

    assert "Позиции CL нет" in text
    assert "FARTCOIN" in text
    assert http.orders == []


def test_close_matches_the_base_symbol_case_insensitively(keyed):
    http = ClosingHTTP()
    http.position_pages = [[row()]]

    text = asyncio.run(bybit_watch.close_position(http, "fartcoin"))

    assert text.startswith("✅ FARTCOIN закрывается")
    assert http.orders == [
        {
            "category": "linear",
            "symbol": "FARTCOINUSDT",
            "side": "Sell",
            "orderType": "Market",
            "qty": "115661.3",
            "reduceOnly": True,
            "positionIdx": 0,
        }
    ]


def test_close_reports_a_refusal(keyed, caplog):
    http = ClosingHTTP()
    http.refuse_order = True
    http.position_pages = [[row(symbol="OPUSDT", side="Sell", size="10")]]

    with caplog.at_level("ERROR", logger="relay.bybit"):
        text = asyncio.run(bybit_watch.close_position(http, "OPUSDT"))

    assert text.startswith("❌ OP")
    assert "could not close" in caplog.text


# --------------------------------------------------------------------------- #
#  /positions report                                                           #
# --------------------------------------------------------------------------- #
def test_positions_report_sends_one_album(keyed):
    http = ClosingHTTP()
    http.position_pages = [[dict(row(tp="0.19", sl="0.15"), unrealisedPnl="512.3")]]
    http.kline_rows = KLINES
    albums = []

    async def send_album(caption, pngs):
        assert all(png.startswith(b"\x89PNG") for png in pngs)
        albums.append((caption, len(pngs)))
        return True

    text = asyncio.run(bybit_watch.positions_report(http, send_album))

    assert text == ""  # everything travelled inside the single message
    caption, count = albums[0]
    assert count == 1
    assert caption.startswith("📈FARTCOIN 18,749$@0.1621")
    assert "<b>" in caption
    assert "Σ" not in caption  # one position: no repeating total line


def test_positions_report_falls_back_to_text_without_candles(keyed):
    http = ClosingHTTP()
    http.position_pages = [[dict(row(), unrealisedPnl="1")]]  # no klines -> no chart

    async def send_album(caption, pngs):
        raise AssertionError("no album should have been sent")

    text = asyncio.run(bybit_watch.positions_report(http, send_album))

    assert "📈FARTCOIN 18,749$@" in text


def test_positions_report_falls_back_when_telegram_refuses_the_album(keyed):
    http = ClosingHTTP()
    http.position_pages = [[dict(row(), unrealisedPnl="1")]]
    http.kline_rows = KLINES

    async def send_album(caption, pngs):
        return False

    text = asyncio.run(bybit_watch.positions_report(http, send_album))

    assert "📈FARTCOIN 18,749$@" in text  # the text answer still goes out


def test_positions_report_without_keys(monkeypatch):
    monkeypatch.setattr(bybit_watch, "API_KEY", "")

    assert "нет API-ключей" in asyncio.run(bybit_watch.positions_report(FakeHTTP()))


def test_positions_report_survives_a_dead_api(keyed, caplog):
    class Refusing(FakeHTTP):
        async def get(self, url, headers=None, timeout=None):
            raise OSError("bybit down")

    with caplog.at_level("ERROR", logger="relay.bybit"):
        text = asyncio.run(bybit_watch.positions_report(Refusing()))

    assert "не ответил" in text


def test_positions_report_with_nothing_open(keyed):
    http = FakeHTTP()
    http.position_pages = [[]]

    assert asyncio.run(bybit_watch.positions_report(http)) == "Открытых позиций нет"


def test_positions_report_lists_positions_with_depo_share(keyed):
    http = FakeHTTP()
    http.position_pages = [
        [
            dict(row(tp="0.19", sl="0.15"), unrealisedPnl="512.3"),
            dict(
                row(symbol="OPUSDT", side="Sell", size="10", price="1.2", value="12"),
                unrealisedPnl="-1.5",
            ),
        ]
    ]
    http.equity_rows = [{"totalEquity": "10000"}]

    text = asyncio.run(bybit_watch.positions_report(http))

    assert text.splitlines() == [
        "📈FARTCOIN 18,749$@0.1621 | RR2.31",
        "sl0.15:<b>-1,420.13(-14.20%)</b>",
        "tp0.19:<b>+3,206.33(+32.06%)=деп13,206.33$</b>",
        "PnL+512.30−комса20.62=<b>+491.68(+4.92%)</b>",
        "📉OP 12$@1.2",
        "PnL-1.50−комса0.01=<b>-1.51(-0.02%)</b>",
        "ΣPnL+510.80=<b>+490.16(+4.90%)</b>",
    ]


def test_positions_report_without_depo_keeps_plain_numbers(keyed):
    http = FakeHTTP()
    http.position_pages = [[dict(row(), unrealisedPnl="1")]]

    text = asyncio.run(bybit_watch.positions_report(http))

    assert "%" not in text  # no percent shares without an equity figure
    assert "Σ" not in text  # a single position needs no total


def test_sl_without_tp_shows_risk_but_no_rr(keyed):
    # /positions side of it.
    http = FakeHTTP()
    http.position_pages = [[dict(row(sl="0.15"), unrealisedPnl="1")]]

    report = asyncio.run(bybit_watch.positions_report(http))

    assert "sl0.15:<b>-1,420.13</b>" in report
    assert "RR" not in report

    # And the entry-notice side.
    http2, out = FakeHTTP(), Recorder()
    http2.position_pages = [[row(sl="0.15")]]

    asyncio.run(bybit_watch.tick(http2, {}, out.send, out.speak, out.send_photo))

    assert "sl0.15:" in out.sent[0]
    assert "RR" not in out.sent[0]


def test_positions_report_totals_without_depo(keyed):
    http = FakeHTTP()
    http.position_pages = [
        [dict(row(), unrealisedPnl="1"), dict(row(symbol="OPUSDT"), unrealisedPnl="2")]
    ]

    text = asyncio.run(bybit_watch.positions_report(http))

    assert text.endswith("ΣPnL+3.00=<b>-38.25</b>")


def test_klines_pages_past_bybits_request_cap(keyed):
    """More than 1000 bars must arrive via backward pagination on `end`."""

    class Paged(FakeHTTP):
        def __init__(self):
            super().__init__()
            # A full page of 1000, then the requested remainder of 500 —
            # newest first inside each page, like the real API.
            self.pages = [
                [
                    [str(2_000_999 - i * 1000), "1", "2", "0.5", "1.5", "1", "1"]
                    for i in range(1000)
                ],
                [[str(1_000_999 - i * 1000), "1", "2", "0.5", "1.5", "1", "1"] for i in range(500)],
            ]

        async def get(self, url, headers=None, timeout=None):
            self.requests.append(url)
            return FakeResponse({"retCode": 0, "result": {"list": self.pages.pop(0)}})

    http = Paged()
    times, candles = asyncio.run(bybit_watch._klines(http, "CLUSDT", "15", 1500))

    assert len(candles) == 1500  # trimmed nothing: both pages consumed
    assert times == sorted(times)  # oldest first
    assert len(http.requests) == 2
    assert "end=1001998" in http.requests[1]  # one ms below page one's oldest bar


def test_klines_stops_on_an_exhausted_range(keyed):
    http = FakeHTTP()
    http.kline_rows = KLINES  # two bars, fewer than the page asks for

    times, candles = asyncio.run(bybit_watch._klines(http, "CLUSDT", "15", 1500))

    assert len(candles) == 2
    assert len([u for u in http.requests if "kline" in u]) == 1


def test_klines_stops_at_the_start_bound(keyed):
    http = FakeHTTP()

    class AtStart(FakeHTTP):
        async def get(self, url, headers=None, timeout=None):
            self.requests.append(url)
            # Newest first; the oldest bar in the page sits at `start`.
            rows = [[str(2499 - i), "1", "2", "0.5", "1.5", "1", "1"] for i in range(1000)]
            return FakeResponse({"retCode": 0, "result": {"list": rows}})

    http = AtStart()
    _, candles = asyncio.run(bybit_watch._klines(http, "CLUSDT", "15", 2000, start=1500, end=99999))

    assert len(candles) == 1000  # the oldest bar reached `start`; no second page
    assert len(http.requests) == 1


def test_klines_handles_an_empty_answer(keyed):
    http = FakeHTTP()  # kline_rows empty

    times, candles = asyncio.run(bybit_watch._klines(http, "CLUSDT", "15", 100))

    assert (times, candles) == ([], [])


def test_tick_appends_the_entry_fee_when_fills_are_fresh(keyed):
    import time

    http, out = FakeHTTP(), Recorder()
    http.position_pages = [[row(tp="0.2621", sl="0.15")]]
    now_ms = int(time.time() * 1000)
    http.exec_rows = [
        {"execFee": "0.05", "execTime": str(now_ms)},
        {"execFee": "0.023", "execTime": str(now_ms - 1000)},
        {"execFee": "9.99", "execTime": str(now_ms - 600_000)},  # stale, not the entry
    ]

    asyncio.run(bybit_watch.tick(http, {}, out.send, out.speak, out.send_photo))

    assert out.sent == [
        "💰📈FARTCOIN 18,749$@0.1621 | RR8.26\n"
        "sl0.15:<b>-1,399.65</b>\ntp0.2621:<b>+11,565.98</b>\nкомса0.073"
    ]


def test_tick_reports_the_take_target_as_a_share_of_equity(keyed):
    http, out = FakeHTTP(), Recorder()
    # Clean numbers: 0.1 to the TP on 115661.3 coins = 11,566.13 USDT gross.
    http.position_pages = [[row(tp="0.2621", sl="0.15")]]
    http.equity_rows = [{"totalEquity": "20000"}]

    asyncio.run(bybit_watch.tick(http, {}, out.send, out.speak, out.send_photo))

    assert out.sent == [
        "💰📈FARTCOIN 18,749$@0.1621 | RR8.26\n"
        "sl0.15:<b>-1,420.13(-7.10%)</b>\ntp0.2621:<b>+11,545.51(+57.73%)=деп31,545.51$</b>"
        "\n⚠️ риск 7.00% депо, цель 0.5%"
    ]


def test_entry_fee_swallows_api_errors(keyed, caplog):
    class Refusing(FakeHTTP):
        async def get(self, url, headers=None, timeout=None):
            raise OSError("bybit down")

    with caplog.at_level("ERROR", logger="relay.bybit"):
        assert asyncio.run(bybit_watch.entry_fee(Refusing(), "FARTCOINUSDT")) is None

    assert "no entry fee" in caplog.text


def test_tick_sends_an_entry_chart_when_candles_exist(keyed):
    http, out = FakeHTTP(), Recorder()
    http.position_pages = [[row(tp="0.19", sl="0.15")]]
    http.kline_rows = KLINES

    asyncio.run(bybit_watch.tick(http, {}, out.send, out.speak, out.send_photo))

    assert out.photos == [
        "💰📈FARTCOIN 18,749$@0.1621 | RR2.31\nsl0.15:<b>-1,420.13</b>\ntp0.19:<b>+3,206.33</b>"
    ]
    assert out.sent == []  # the caption carries the notice
    assert out.spoken == ["Fartcoin long opened"]


def test_tick_falls_back_to_text_when_telegram_refuses_the_photo(keyed):
    http, out = FakeHTTP(), Recorder()
    http.position_pages = [[row()]]
    http.kline_rows = KLINES
    out.photo_ok = False

    asyncio.run(bybit_watch.tick(http, {}, out.send, out.speak, out.send_photo))

    assert out.photos == ["💰📈FARTCOIN 18,749$@0.1621\n⚠️ без стопа"]
    assert out.sent == ["💰📈FARTCOIN 18,749$@0.1621\n⚠️ без стопа"]


def test_tick_sends_a_close_chart_with_the_pnl_caption(keyed):
    http, out = FakeHTTP(), Recorder()
    http.position_pages = [[]]
    http.pnl_rows = [closed()]
    http.kline_rows = KLINES

    asyncio.run(bybit_watch.tick(http, {"FARTCOINUSDT": LONG}, out.send, out.speak, out.send_photo))

    assert out.photos == ["💸📈FARTCOIN<b>+512.30</b>-(0.073+0.078)"]
    assert out.sent == []
    assert out.spoken == ["Fartcoin long closed, profit 512"]


def test_journal_row_matches_the_diary_format():
    was = Position("short", 1.42, 92.0, 131.0, take_profit=84.1, stop_loss=92.88)
    record = {"avgEntryPrice": "92.04", "createdTime": "1788719433000", "closedPnl": "x"}

    row = bybit_watch.journal_row("CLUSDT", was, record, -1.19)

    assert row[1:6] == ["CLUSDT", "Шорт", "stop", "1к9.5", -1.0]
    assert row[0].count("/") == 2  # DD/MM/YYYY
    assert row[6] == ""  # the screenshot chip is the script's to fill
    assert row[7:] == [131.0, 92.04, "", -1.19, "", "", "", "", ""]


@pytest.mark.parametrize(
    ("history", "expected"),
    [
        ([{"createType": "CreateByStopLoss", "stopOrderType": "StopLoss"}], "стоп"),
        ([{"createType": "CreateByTakeProfit", "stopOrderType": "TakeProfit"}], "тейк"),
        ([{"createType": "CreateByUser", "stopOrderType": ""}], "руками"),
        ([{"createType": "CreateByAdl", "stopOrderType": ""}], ""),
        ([], ""),
    ],
)
def test_close_kind_reads_the_creating_order(keyed, history, expected):
    http = FakeHTTP()
    http.history_rows = history

    assert asyncio.run(bybit_watch.close_kind(http, {"orderId": "x1"})) == expected


def test_close_kind_without_an_order_id(keyed):
    assert asyncio.run(bybit_watch.close_kind(FakeHTTP(), {})) == ""


def test_close_kind_survives_a_refusal(keyed):
    class Refusing(FakeHTTP):
        async def get(self, url, headers=None, timeout=None):
            raise OSError("down")

    assert asyncio.run(bybit_watch.close_kind(Refusing(), {"orderId": "x1"})) == ""


def test_journal_row_marks_a_guard_close():
    was = Position("long", 1.0, 199.12, 44.0)

    row = bybit_watch.journal_row("SAMSUNGUSDT", was, {}, -0.04, forced="нет стопа")

    assert row[4] == "принудительно остановлено: нет стопа"


def test_journal_row_extras_carry_the_full_arithmetic():
    was = Position("short", 1.42, 92.0, 131.0, stop_loss=92.88)
    record = {
        "avgEntryPrice": "92.04",
        "avgExitPrice": "91.9",
        "openFee": "0.0199",
        "closeFee": "0.0547",
    }

    row = bybit_watch.journal_row("CLUSDT", was, record, -1.19, 128.16, kind="руками")

    assert row[7:] == [131.0, 92.04, 91.9, -1.19, -0.93, "0.0199+0.0547", 128.16, "", "руками"]


def test_journal_row_without_a_stop_falls_back_to_usdt():
    was = Position("long", 1.0, 100.0, 100.0)

    row = bybit_watch.journal_row("CLUSDT", was, {}, 5.4321)

    assert row[0] == "" and row[4] == ""
    assert row[3] == "win"
    assert row[5] == 5.43  # plain net USDT, no risk to divide by


def test_bar_of_clamps_to_the_fetched_range(keyed):
    times = [100, 200, 300]

    assert bybit_watch._bar_of(times, 250) == 1
    assert bybit_watch._bar_of(times, 999) == 2
    assert bybit_watch._bar_of(times, 50) == 0  # before the first bar


def test_close_chart_stays_on_the_configured_timeframe(keyed):
    # A month of trade: the timeframe holds at 15m, the bar count caps at
    # Bybit's 1000-per-request ceiling.
    http = FakeHTTP()
    http.kline_rows = KLINES
    record = closed()
    record["updatedTime"] = str(int(record["createdTime"]) + 30 * 24 * 3600 * 1000)

    png = asyncio.run(bybit_watch.close_chart(http, "FARTCOINUSDT", LONG, record))

    assert png is not None and png.startswith(b"\x89PNG")
    assert any("interval=15" in url and "limit=1000" in url for url in http.requests)


def test_close_chart_gives_up_quietly(keyed, caplog):
    # No candles in range: render refuses, the notice must still go out.
    http = FakeHTTP()

    with caplog.at_level("ERROR", logger="relay.bybit"):
        png = asyncio.run(bybit_watch.close_chart(http, "FARTCOINUSDT", LONG, closed()))

    assert png is None
    assert "no close chart" in caplog.text


def test_entry_chart_gives_up_quietly_without_candles(keyed, caplog):
    http = FakeHTTP()

    with caplog.at_level("ERROR", logger="relay.bybit"):
        png = asyncio.run(bybit_watch.entry_chart(http, "FARTCOINUSDT", LONG))

    assert png is None
    assert "no chart" in caplog.text


def test_positions_reads_tp_and_sl(keyed):
    http = FakeHTTP()
    http.position_pages = [[row(tp="0.19", sl="0.15")]]

    got = asyncio.run(bybit_watch.positions(http))

    assert got["FARTCOINUSDT"].take_profit == 0.19
    assert got["FARTCOINUSDT"].stop_loss == 0.15


def closed(pnl="512.3", entry="0.16", exit_price="0.17", open_fee="0.073", close_fee="0.078"):
    return {
        "closedPnl": pnl,
        "createdTime": "1700000000000",
        "updatedTime": "1700003600000",
        "avgEntryPrice": entry,
        "avgExitPrice": exit_price,
        "openFee": open_fee,
        "closeFee": close_fee,
    }


def test_tick_reports_a_resize_as_plain_text(keyed):
    http, out = FakeHTTP(), Recorder()
    http.position_pages = [[row(size="231322.6", value="37497.4")]]

    asyncio.run(bybit_watch.tick(http, {"FARTCOINUSDT": LONG}, out.send, out.speak, out.send_photo))

    assert out.photos == []
    assert out.sent == ["💰📈FARTCOIN 16→37,497"]


def test_tick_reports_pnl_on_a_close(keyed):
    http, out = FakeHTTP(), Recorder()
    http.position_pages = [[]]
    http.pnl_rows = [closed()]

    asyncio.run(bybit_watch.tick(http, {"FARTCOINUSDT": LONG}, out.send, out.speak, out.send_photo))

    assert out.sent == ["💸📈FARTCOIN<b>+512.30</b>-(0.073+0.078)"]
    assert out.spoken == ["Fartcoin long closed, profit 512"]


def test_tick_close_reports_the_pnl_as_a_share_of_equity(keyed):
    http, out = FakeHTTP(), Recorder()
    http.position_pages = [[]]
    http.pnl_rows = [closed() | {"orderId": "o1"}]
    http.equity_rows = [{"totalEquity": "10000"}]
    http.history_rows = [{"createType": "CreateByUser", "stopOrderType": ""}]

    asyncio.run(bybit_watch.tick(http, {"FARTCOINUSDT": LONG}, out.send, out.speak, out.send_photo))

    assert out.sent == ["💸📈FARTCOIN<b>+512.30(+5.12%)</b>-(0.073+0.078)=<b>10,000.00$</b>·руками"]


@pytest.mark.parametrize(
    "rows",
    [
        [],  # no accounts at all
        [{"totalEquity": "", "totalWalletBalance": ""}],  # accounts without figures
    ],
)
def test_equity_absent_figures_return_none(keyed, rows):
    http = FakeHTTP()
    http.equity_rows = rows

    assert asyncio.run(bybit_watch.equity(http)) is None


def test_equity_falls_back_to_wallet_balance(keyed):
    http = FakeHTTP()
    http.equity_rows = [{"totalEquity": "", "totalWalletBalance": "42"}]

    assert asyncio.run(bybit_watch.equity(http)) == 42.0


def test_equity_swallows_api_errors(keyed, caplog):
    class Refusing(FakeHTTP):
        async def get(self, url, headers=None, timeout=None):
            raise OSError("bybit down")

    with caplog.at_level("ERROR", logger="relay.bybit"):
        assert asyncio.run(bybit_watch.equity(Refusing())) is None

    assert "no equity" in caplog.text


def test_tick_close_without_fee_fields_stays_plain(keyed):
    http, out = FakeHTTP(), Recorder()
    http.position_pages = [[]]
    http.pnl_rows = [closed(open_fee="", close_fee="")]

    asyncio.run(bybit_watch.tick(http, {"FARTCOINUSDT": LONG}, out.send, out.speak, out.send_photo))

    assert out.sent == ["💸📈FARTCOIN<b>+512.30</b>"]


def test_tick_close_survives_a_missing_pnl(keyed):
    http, out = FakeHTTP(), Recorder()
    http.position_pages = [[]]
    http.pnl_rows = []

    asyncio.run(bybit_watch.tick(http, {"FARTCOINUSDT": LONG}, out.send, out.speak, out.send_photo))

    assert out.sent == ["💸📈FARTCOIN"]


# --------------------------------------------------------------------------- #
#  poll                                                                        #
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
#  money moves                                                                 #
# --------------------------------------------------------------------------- #
def money_http():
    http = FakeHTTP()
    http.deposit_rows = [
        {"txID": "t1", "amount": "50", "coin": "USDT", "status": "3"},
        {"txID": "t2", "amount": "10", "coin": "USDT", "status": "1"},  # pending
    ]
    http.withdraw_rows = [
        {"withdrawId": "w1", "amount": "100", "coin": "USDT", "status": "success"},
        {"withdrawId": "w2", "amount": "5", "coin": "USDT", "status": "Reject"},
    ]
    http.transfer_rows = [
        {
            "transferId": "a",
            "amount": "20",
            "coin": "USDT",
            "status": "SUCCESS",
            "fromAccountType": "FUND",
            "toAccountType": "UNIFIED",
        },
        {
            "transferId": "b",
            "amount": "30",
            "coin": "USDT",
            "status": "SUCCESS",
            "fromAccountType": "UNIFIED",
            "toAccountType": "FUND",
        },
        {
            "transferId": "c",
            "amount": "7",
            "coin": "USDT",
            "status": "SUCCESS",
            "fromAccountType": "FUND",
            "toAccountType": "SPOT",
        },  # not ours
        {
            "transferId": "d",
            "amount": "9",
            "coin": "USDT",
            "status": "PENDING",
            "fromAccountType": "FUND",
            "toAccountType": "UNIFIED",
        },
    ]
    return http


def test_money_moves_reads_finished_flows_only(keyed):
    moves = asyncio.run(bybit_watch.money_moves(money_http()))

    assert {(m["id"], m["amount"]) for m in moves} == {
        ("dep-t1", 50.0),
        ("wd-w1", -100.0),
        ("tr-a", 20.0),
        ("tr-b", -30.0),
    }


def test_money_tick_primes_silently(keyed):
    out = Recorder()

    seen = asyncio.run(bybit_watch.money_tick(money_http(), None, out.send))

    assert seen == {"dep-t1", "wd-w1", "tr-a", "tr-b"}
    assert out.sent == []


def test_money_tick_announces_and_journals_new_moves(keyed, monkeypatch):
    out = Recorder()
    journaled = []

    async def fake_log_close(http, entry):
        journaled.append(entry)
        return True

    monkeypatch.setattr(bybit_watch.sheets, "log_close", fake_log_close)
    http = money_http()
    http.equity_rows = [{"totalEquity": "128.0", "totalAvailableBalance": "50"}]

    seen = asyncio.run(bybit_watch.money_tick(http, {"dep-t1", "wd-w1", "tr-b"}, out.send))

    assert out.sent == ["💵 завел +20.00 USDT · деп 128.00$"]
    assert seen == {"dep-t1", "wd-w1", "tr-a", "tr-b"}
    row = journaled[0]["row"]
    assert row[1] == "перевод"
    assert row[13:] == [128.0, 20.0]


def test_money_tick_without_equity_skips_the_depo(keyed, monkeypatch):
    out = Recorder()

    async def fake_log_close(http, entry):
        return True

    monkeypatch.setattr(bybit_watch.sheets, "log_close", fake_log_close)

    asyncio.run(bybit_watch.money_tick(money_http(), {"dep-t1", "tr-a", "tr-b"}, out.send))

    assert out.sent == ["💵 вывел -100.00 USDT"]


def test_money_poll_survives_failures(keyed, monkeypatch, caplog):
    class Refusing(FakeHTTP):
        async def get(self, url, headers=None, timeout=None):
            raise OSError("no assets permission")

    naps = []

    async def fake_sleep(seconds):
        naps.append(seconds)
        if len(naps) >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(bybit_watch.asyncio, "sleep", fake_sleep)
    out = Recorder()

    with caplog.at_level("ERROR", logger="relay.bybit"), pytest.raises(asyncio.CancelledError):
        asyncio.run(bybit_watch.money_poll(Refusing(), out.send))

    assert "money poll failed" in caplog.text
    assert out.sent == []


def test_money_poll_lets_cancellation_through(keyed):
    class Cancelling(FakeHTTP):
        async def get(self, url, headers=None, timeout=None):
            raise asyncio.CancelledError

    out = Recorder()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bybit_watch.money_poll(Cancelling(), out.send))

    assert out.sent == []


def test_money_poll_hands_ticks_through(keyed, monkeypatch):
    ticks = []

    async def fake_tick(http, seen, send):
        ticks.append(seen)
        return {"x"}

    async def fake_sleep(seconds):
        if len(ticks) >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(bybit_watch, "money_tick", fake_tick)
    monkeypatch.setattr(bybit_watch.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bybit_watch.money_poll(FakeHTTP(), Recorder().send))

    assert ticks == [None, {"x"}]


def test_poll_survives_failures_and_keeps_going(keyed, monkeypatch, caplog):
    """One bad poll must not end the loop, and must not wipe the snapshot."""
    out = Recorder()

    class Flaky(FakeHTTP):
        def __init__(self):
            super().__init__()
            self.list_calls = 0

        async def get(self, url, headers=None, timeout=None):
            if "/v5/position/list" in url:
                self.list_calls += 1
                if self.list_calls == 2:
                    raise OSError("bybit down")
            return await super().get(url, headers=headers, timeout=timeout)

    http = Flaky()
    # Prime with one long, fail once, then the position is gone -> "closed".
    http.position_pages = [[row()], []]

    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)
        if len(slept) >= 4:
            raise asyncio.CancelledError

    monkeypatch.setattr(bybit_watch.asyncio, "sleep", fake_sleep)

    with caplog.at_level("ERROR", logger="relay.bybit"), pytest.raises(asyncio.CancelledError):
        asyncio.run(bybit_watch.poll(http, out.send, out.speak, out.send_photo))

    assert "bybit poll failed" in caplog.text
    assert any(seconds >= 30 for seconds in slept)  # backed off after the failure
    assert out.sent and out.sent[0].startswith("💸📈FARTCOIN")


def test_poll_lets_cancellation_through(keyed, monkeypatch):
    """Being cancelled mid-fetch must end the loop, not count as a bad poll."""

    class Cancelling(FakeHTTP):
        async def get(self, url, headers=None, timeout=None):
            raise asyncio.CancelledError

    out = Recorder()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bybit_watch.poll(Cancelling(), out.send, out.speak, out.send_photo))

    assert out.sent == []


def test_closed_record_swallows_api_errors(keyed, caplog):
    class Refusing(FakeHTTP):
        async def get(self, url, headers=None, timeout=None):
            raise OSError("bybit down")

    with caplog.at_level("ERROR", logger="relay.bybit"):
        assert asyncio.run(bybit_watch.closed_record(Refusing(), "FARTCOINUSDT")) is None

    assert "no closed pnl" in caplog.text


def test_market_report_sends_btc_and_eth_in_one_shot(keyed):
    http = FakeHTTP()
    http.kline_rows = KLINES
    shots = []

    async def send_photo(caption, png):
        shots.append((caption, png[:8]))
        return True

    asyncio.run(bybit_watch.market_report(http, send_photo))

    assert len(shots) == 1
    caption, magic = shots[0]
    assert magic == b"\x89PNG\r\n\x1a\n"
    assert caption.startswith("BTC 0 · ETH 0")  # test candles close at 0.169


def test_market_report_survives_a_dead_api(keyed, caplog):
    class Refusing(FakeHTTP):
        async def get(self, url, headers=None, timeout=None):
            raise OSError("bybit down")

    async def send_photo(caption, png):
        raise AssertionError("nothing should be sent")

    with caplog.at_level("ERROR", logger="relay.bybit"):
        asyncio.run(bybit_watch.market_report(Refusing(), send_photo))

    assert "no market snapshot" in caplog.text


# --------------------------------------------------------------------------- #
#  /statistics                                                                 #
# --------------------------------------------------------------------------- #
class HistoryHTTP(FakeHTTP):
    """Serves closed-pnl pages with one cursor hop on the first window."""

    def __init__(self):
        super().__init__()
        self.history_calls = 0

    async def get(self, url, headers=None, timeout=None):
        if "/v5/position/closed-pnl" in url:
            self.history_calls += 1
            if "cursor=" not in url and self.history_calls == 1:
                return FakeResponse(
                    {
                        "retCode": 0,
                        "result": {"list": [closed()], "nextPageCursor": "abc"},
                    }
                )
            if "cursor=abc" in url:
                return FakeResponse({"retCode": 0, "result": {"list": [closed(pnl="-3")]}})
            return FakeResponse({"retCode": 0, "result": {"list": []}})
        return await super().get(url, headers=headers, timeout=timeout)


def test_closed_history_walks_windows_and_cursors(keyed):
    http = HistoryHTTP()

    rows = asyncio.run(bybit_watch.closed_history(http, days=30))

    assert len(rows) == 2  # the page plus its cursor continuation
    assert http.history_calls >= 6  # five 7-day windows, one with an extra page


def test_stats_report_without_keys(monkeypatch):
    monkeypatch.setattr(bybit_watch, "API_KEY", "")

    async def send_photo(caption, png):
        raise AssertionError("nothing to send")

    assert "нет API-ключей" in asyncio.run(bybit_watch.stats_report(FakeHTTP(), send_photo))


def test_stats_report_survives_a_dead_api(keyed, caplog, monkeypatch):
    async def boom(http, days=30):
        raise OSError("bybit down")

    monkeypatch.setattr(bybit_watch, "closed_history", boom)

    async def send_photo(caption, png):
        raise AssertionError("nothing to send")

    with caplog.at_level("ERROR", logger="relay.bybit"):
        assert "не ответил" in asyncio.run(bybit_watch.stats_report(FakeHTTP(), send_photo))


def test_stats_report_with_no_trades(keyed, monkeypatch):
    async def empty(http, days=30):
        return []

    monkeypatch.setattr(bybit_watch, "closed_history", empty)

    async def send_photo(caption, png):
        raise AssertionError("nothing to send")

    assert "закрытых сделок нет" in asyncio.run(bybit_watch.stats_report(FakeHTTP(), send_photo))


def test_stats_report_sends_the_curve_with_the_figures(keyed, monkeypatch):
    import time as _time

    now_ms = int(_time.time() * 1000)

    async def history(http, days=30):
        return [
            {"closedPnl": "5", "updatedTime": str(now_ms)},
            {"closedPnl": "-2", "updatedTime": str(now_ms - 86_400_000)},
        ]

    monkeypatch.setattr(bybit_watch, "closed_history", history)
    http = FakeHTTP()
    http.equity_rows = [{"totalEquity": "133"}]
    shots = []

    async def send_photo(caption, png):
        assert png.startswith(b"\x89PNG")
        shots.append(caption)
        return True

    text = asyncio.run(bybit_watch.stats_report(http, send_photo))

    assert text == ""
    assert shots[0].startswith("📊 за 30 дн.: сделок 2 · win 1/loss 1 (50%)")
    assert "+3.00 (+2.26% депо 133)" in shots[0]
    assert "лучший +5.00 · худший -2.00" in shots[0]


def test_stats_report_falls_back_to_text_when_the_photo_fails(keyed, monkeypatch):
    async def history(http, days=30):
        return [{"closedPnl": "1", "updatedTime": "0"}]

    monkeypatch.setattr(bybit_watch, "closed_history", history)

    async def send_photo(caption, png):
        return False

    text = asyncio.run(bybit_watch.stats_report(FakeHTTP(), send_photo))

    assert text.startswith("📊 за 30 дн.")


def test_stats_report_survives_a_broken_curve(keyed, monkeypatch, caplog):
    async def history(http, days=30):
        return [{"closedPnl": "1", "updatedTime": "0"}]

    monkeypatch.setattr(bybit_watch, "closed_history", history)
    monkeypatch.setattr(
        bybit_watch.chart, "equity_curve", lambda daily: (_ for _ in ()).throw(OSError("boom"))
    )

    async def send_photo(caption, png):
        raise AssertionError("no chart to send")

    with caplog.at_level("ERROR", logger="relay.bybit"):
        text = asyncio.run(bybit_watch.stats_report(FakeHTTP(), send_photo))

    assert text.startswith("📊 за 30 дн.")
    assert "no equity curve" in caplog.text


def test_stats_report_takes_a_day_count(keyed, monkeypatch):
    seen = []

    async def history(http, days=30):
        seen.append(days)
        return []

    monkeypatch.setattr(bybit_watch, "closed_history", history)

    async def send_photo(caption, png):
        raise AssertionError("nothing to send")

    assert "За 7 дн." in asyncio.run(bybit_watch.stats_report(FakeHTTP(), send_photo, "7"))
    assert seen == [7]
    assert "За 90 дн." in asyncio.run(bybit_watch.stats_report(FakeHTTP(), send_photo, "500"))
    assert "Например" in asyncio.run(bybit_watch.stats_report(FakeHTTP(), send_photo, "неделя"))
