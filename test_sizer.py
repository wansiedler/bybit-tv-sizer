"""Tests for the order auto-sizer, ported with the module from bybit-tv-sizer.

The sizing math decides how much real money is at risk on every order, so it
is pinned line by line; the transport is faked at the HTTP client.
"""

import asyncio
from decimal import Decimal

import pytest

import bybit_watch
import sizer

BTC_LOT = {"lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001", "maxOrderQty": "100"}}

ORDER = {
    "symbol": "BTCUSDT",
    "orderId": "abc12345",
    "side": "Buy",
    "price": "60000",
    "stopLoss": "59000",
    "qty": "0.001",
    "orderType": "Limit",
    "orderStatus": "New",
    "reduceOnly": False,
    "triggerPrice": "",
    "cumExecQty": "0",
}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Every test starts from a known config and empty module state."""
    monkeypatch.setattr(bybit_watch, "API_KEY", "key")
    monkeypatch.setattr(bybit_watch, "API_SECRET", "secret")
    monkeypatch.setattr(sizer, "DRY_RUN", True)
    monkeypatch.setattr(sizer, "RISK_PCT", Decimal("0.005"))
    monkeypatch.setattr(sizer, "FALLBACK_SL_PCT", Decimal("0"))
    monkeypatch.setattr(sizer, "MAX_LEVERAGE", Decimal("5"))
    monkeypatch.setattr(sizer, "SYMBOLS", set())
    # Debounce off by default: the settling behaviour has its own tests.
    monkeypatch.setattr(sizer, "SETTLE_POLLS", 1)
    monkeypatch.setattr(sizer, "MIN_RR", Decimal("2"))
    sizer._settling.clear()
    sizer._instruments.clear()
    sizer._warned.clear()
    yield
    sizer._settling.clear()
    sizer._instruments.clear()
    sizer._warned.clear()


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class FakeHTTP:
    """Answers the sizer's endpoints from scripted payloads, records writes."""

    def __init__(self):
        self.orders: list[dict] = []
        self.equity = "10000"
        self.instruments = [BTC_LOT]
        self.amended: list[dict] = []
        self.gets: list[str] = []

    async def get(self, url, headers=None, timeout=None):
        self.gets.append(url)
        if "/v5/order/realtime" in url:
            return FakeResponse({"retCode": 0, "result": {"list": self.orders}})
        if "/v5/account/wallet-balance" in url:
            rows = [{"totalEquity": self.equity}]
            return FakeResponse({"retCode": 0, "result": {"list": rows}})
        return FakeResponse({"retCode": 0, "result": {"list": self.instruments}})

    async def post(self, url, content=None, headers=None, timeout=None):
        import json

        self.amended.append(json.loads(content))
        return FakeResponse({"retCode": 0, "result": {}})


class Recorder:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, text):
        self.sent.append(text)


# ------------------------------------------------------------------ config
def test_enabled_follows_the_keys_like_the_original(monkeypatch):
    assert sizer.enabled() is True
    monkeypatch.setattr(bybit_watch, "API_KEY", "")
    assert sizer.enabled() is False


def test_api_url_matches_the_original_selection(monkeypatch):
    monkeypatch.delenv("BYBIT_API_URL", raising=False)
    monkeypatch.setenv("BYBIT_TESTNET", "1")
    assert bybit_watch._api_url() == "https://api-testnet.bybit.com"
    monkeypatch.setenv("BYBIT_TESTNET", "0")
    assert bybit_watch._api_url() == "https://api.bybit.com"
    monkeypatch.setenv("BYBIT_API_URL", "http://127.0.0.1:9")
    assert bybit_watch._api_url() == "http://127.0.0.1:9"


# ------------------------------------------------------------------ transport
def test_post_signs_the_json_body(monkeypatch):
    seen = {}

    class Capturing(FakeHTTP):
        async def post(self, url, content=None, headers=None, timeout=None):
            seen.update({"url": url, "content": content, "headers": headers})
            return FakeResponse({"retCode": 0, "result": {}})

    asyncio.run(bybit_watch._post(Capturing(), "/v5/order/amend", {"a": "1"}))

    assert seen["url"].endswith("/v5/order/amend")
    assert seen["content"] == '{"a":"1"}'
    assert seen["headers"]["Content-Type"] == "application/json"
    assert seen["headers"]["X-BAPI-SIGN"]


def test_post_raises_on_a_refusal():
    class Refusing(FakeHTTP):
        async def post(self, url, content=None, headers=None, timeout=None):
            return FakeResponse({"retCode": 110007, "retMsg": "insufficient balance"})

    with pytest.raises(RuntimeError, match="insufficient balance"):
        asyncio.run(bybit_watch._post(Refusing(), "/v5/order/amend", {}))


# ------------------------------------------------------------------ api calls
def test_get_equity_prefers_total_equity():
    http = FakeHTTP()
    http.equity = "10000.5"

    assert asyncio.run(sizer.get_equity(http)) == Decimal("10000.5")


def test_get_equity_falls_back_to_wallet_balance():
    class Fallback(FakeHTTP):
        async def get(self, url, headers=None, timeout=None):
            rows = [{"totalEquity": "", "totalWalletBalance": "42"}]
            return FakeResponse({"retCode": 0, "result": {"list": rows}})

    assert asyncio.run(sizer.get_equity(Fallback())) == Decimal("42")


def test_get_equity_without_accounts_raises():
    class Empty(FakeHTTP):
        async def get(self, url, headers=None, timeout=None):
            return FakeResponse({"retCode": 0, "result": {"list": []}})

    with pytest.raises(RuntimeError, match="no accounts"):
        asyncio.run(sizer.get_equity(Empty()))


def test_get_instrument_caches_the_lookup():
    http = FakeHTTP()

    assert asyncio.run(sizer.get_instrument(http, "BTCUSDT")) == BTC_LOT
    assert asyncio.run(sizer.get_instrument(http, "BTCUSDT")) == BTC_LOT
    assert len(http.gets) == 1


def test_get_instrument_without_a_match_raises():
    http = FakeHTTP()
    http.instruments = []

    with pytest.raises(RuntimeError, match="no instrument info"):
        asyncio.run(sizer.get_instrument(http, "NOPEUSDT"))


def test_amend_qty_posts_the_new_quantity():
    http = FakeHTTP()

    asyncio.run(sizer.amend_qty(http, "BTCUSDT", "abc", Decimal("0.05")))

    assert http.amended == [
        {"category": "linear", "symbol": "BTCUSDT", "orderId": "abc", "qty": "0.05"}
    ]


# ------------------------------------------------------------------ sizing
def test_round_step_rounds_down():
    assert sizer.round_step(Decimal("0.0519"), Decimal("0.001")) == Decimal("0.051")
    assert sizer.round_step(Decimal("7"), Decimal("5")) == Decimal("5")


def size(order, equity="10000", lot=BTC_LOT):
    return sizer.target_qty(order, Decimal(equity), lot)


def test_risk_percent_sets_the_quantity():
    # 0.5% of 10000 = 50 USDT risked over a 1000-point stop -> 0.05 BTC.
    assert size(ORDER) == Decimal("0.050")


def test_leverage_ceiling_caps_a_tight_stop():
    # A 10-point stop asks for 5 BTC; 5x on 10000 USDT allows 0.833 at 60000.
    assert size(dict(ORDER, stopLoss="59990")) == Decimal("0.833")


def test_exchange_maximum_caps_the_quantity():
    tiny = {"lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001", "maxOrderQty": "0.01"}}

    assert size(ORDER, lot=tiny) == Decimal("0.01")


def test_order_without_a_stop_is_skipped():
    assert size(dict(ORDER, stopLoss="")) is None


def test_fallback_stop_is_used_when_configured(monkeypatch):
    monkeypatch.setattr(sizer, "FALLBACK_SL_PCT", Decimal("0.01"))

    # Sell side: the assumed stop sits 1% above 60000, i.e. 600 points away.
    assert size(dict(ORDER, stopLoss="0", side="Sell")) == Decimal("0.083")


def test_fallback_stop_sits_below_a_buy(monkeypatch):
    monkeypatch.setattr(sizer, "FALLBACK_SL_PCT", Decimal("0.01"))

    assert size(dict(ORDER, stopLoss="")) == Decimal("0.083")


def test_stop_equal_to_entry_is_skipped():
    assert size(dict(ORDER, stopLoss="60000")) is None


def test_non_positive_price_is_skipped():
    assert size(dict(ORDER, price="0")) is None


def test_quantity_below_the_exchange_minimum_is_skipped():
    # 0.5% of 1 USDT over a 1000-point stop rounds to nothing tradable.
    assert size(ORDER, equity="1") is None


# ------------------------------------------------------------------ order filter
@pytest.mark.parametrize(
    ("patch", "expected"),
    [
        ({}, True),
        ({"orderType": "Market"}, False),
        ({"orderStatus": "Filled"}, False),
        ({"reduceOnly": True}, False),
        ({"triggerPrice": "59000"}, False),
        ({"cumExecQty": "0.002"}, False),
    ],
)
def test_is_managed(patch, expected):
    assert sizer.is_managed(dict(ORDER, **patch)) is expected


def test_symbol_allowlist_filters_other_markets(monkeypatch):
    monkeypatch.setattr(sizer, "SYMBOLS", {"ETHUSDT"})

    assert sizer.is_managed(ORDER) is False
    assert sizer.is_managed(dict(ORDER, symbol="ETHUSDT")) is True


# ------------------------------------------------------------------ the pass
def run_tick(http, out):
    asyncio.run(sizer.tick(http, out.send))


def test_tick_without_managed_orders_never_asks_for_equity():
    http, out = FakeHTTP(), Recorder()
    http.orders = [dict(ORDER, orderType="Market")]

    run_tick(http, out)

    assert not any("wallet-balance" in url for url in http.gets)


def test_tick_dry_run_reports_without_amending(caplog):
    http, out = FakeHTTP(), Recorder()
    http.orders = [ORDER]

    with caplog.at_level("INFO", logger="relay.sizer"):
        run_tick(http, out)

    assert "[dry-run]" in caplog.text
    assert http.amended == []
    assert out.sent == []


def test_tick_amends_and_announces_when_live(monkeypatch):
    monkeypatch.setattr(sizer, "DRY_RUN", False)
    http, out = FakeHTTP(), Recorder()
    http.orders = [ORDER]

    run_tick(http, out)

    assert http.amended[0]["qty"] == "0.050"
    assert out.sent == ["⚖️ BTCUSDT Buy limit @ 60000\nstop 59000 → qty 0.001 → 0.050"]


def test_tick_leaves_a_correctly_sized_order_alone(monkeypatch):
    monkeypatch.setattr(sizer, "DRY_RUN", False)
    http, out = FakeHTTP(), Recorder()
    http.orders = [dict(ORDER, qty="0.050")]

    run_tick(http, out)

    assert http.amended == []


def test_tick_skips_an_unsizable_order(monkeypatch):
    monkeypatch.setattr(sizer, "DRY_RUN", False)
    http, out = FakeHTTP(), Recorder()
    http.orders = [dict(ORDER, stopLoss="")]

    run_tick(http, out)

    assert http.amended == []


# ------------------------------------------------------------------ RR gate
@pytest.mark.parametrize(
    ("patch", "expected"),
    [
        ({"takeProfit": "61000"}, "RR 1.00 < 2"),  # 1000 up vs 1000 down
        ({"takeProfit": "63000"}, None),  # RR 3: fine
        ({"takeProfit": ""}, None),  # no TP: nothing to measure
        ({"stopLoss": "", "takeProfit": "63000"}, None),  # no SL either
        ({"stopLoss": "60000", "takeProfit": "61000"}, None),  # zero distance
    ],
)
def test_rr_warning(patch, expected):
    assert sizer.rr_warning(dict(ORDER, **patch)) == expected


def test_rr_warning_can_be_disabled(monkeypatch):
    monkeypatch.setattr(sizer, "MIN_RR", Decimal("0"))

    assert sizer.rr_warning(dict(ORDER, takeProfit="61000")) is None


def test_tick_warns_about_a_thin_rr_once(monkeypatch):
    http, out = FakeHTTP(), Recorder()
    http.orders = [dict(ORDER, takeProfit="61000")]

    run_tick(http, out)
    run_tick(http, out)  # nothing changed: no second ping

    assert out.sent == ["⚠️ BTCUSDT Buy limit @ 60000: RR 1.00 < 2"]


def test_tick_warns_again_after_the_order_changes(monkeypatch):
    http, out = FakeHTTP(), Recorder()
    http.orders = [dict(ORDER, takeProfit="61000")]
    run_tick(http, out)

    http.orders = [dict(ORDER, takeProfit="60500")]
    run_tick(http, out)

    assert len(out.sent) == 2
    assert "RR 0.50 < 2" in out.sent[1]


def test_warned_marker_does_not_outlive_the_order():
    http, out = FakeHTTP(), Recorder()
    http.orders = [dict(ORDER, takeProfit="61000")]
    run_tick(http, out)
    assert "abc12345" in sizer._warned

    http.orders = []
    run_tick(http, out)
    assert sizer._warned == {}


# ------------------------------------------------------------------ settling
def test_a_moving_stop_is_left_alone_until_it_holds_still(monkeypatch):
    """Dragging a stop across the chart must not produce an amend per step."""
    monkeypatch.setattr(sizer, "DRY_RUN", False)
    monkeypatch.setattr(sizer, "SETTLE_POLLS", 2)
    http, out = FakeHTTP(), Recorder()

    for stop in ("59000", "58000", "57000"):
        http.orders = [dict(ORDER, stopLoss=stop)]
        run_tick(http, out)
    assert http.amended == []

    run_tick(http, out)  # the stop finally holds at 57000
    # 0.5% of 10000 over a 3000-point stop, rounded to the lot size.
    assert http.amended[0]["qty"] == "0.016"


def test_settle_counter_does_not_outlive_the_order():
    http, out = FakeHTTP(), Recorder()
    http.orders = [ORDER]
    run_tick(http, out)
    assert "abc12345" in sizer._settling

    http.orders = []
    run_tick(http, out)
    assert sizer._settling == {}


def test_every_order_keeps_settling_while_another_is_amended(monkeypatch):
    """One ready order must not stop the counter of the one still moving."""
    monkeypatch.setattr(sizer, "SETTLE_POLLS", 2)
    http, out = FakeHTTP(), Recorder()
    http.orders = [ORDER, dict(ORDER, orderId="def67890")]

    run_tick(http, out)
    run_tick(http, out)

    assert sizer._settling["abc12345"][1] == 2
    assert sizer._settling["def67890"][1] == 2


# ------------------------------------------------------------------ poll
def test_poll_announces_itself_and_survives_failures(monkeypatch, caplog):
    out = Recorder()

    class Flaky(FakeHTTP):
        calls = 0

        async def get(self, url, headers=None, timeout=None):
            type(self).calls += 1
            raise OSError("bybit down")

    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)
        if len(slept) >= 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(sizer.asyncio, "sleep", fake_sleep)

    with caplog.at_level("ERROR", logger="relay.sizer"), pytest.raises(asyncio.CancelledError):
        asyncio.run(sizer.poll(Flaky(), out.send))

    assert out.sent[0].startswith("⚖️ sizer up — dry-run")
    # Three failing passes, but the same error is announced only once.
    assert out.sent[1:] == ["⚖️ sizer error: bybit down"]
    assert "sizer pass failed" in caplog.text


def test_poll_lets_cancellation_through():
    """Being cancelled mid-pass must end the loop, not count as a bad pass."""

    class Cancelling(FakeHTTP):
        async def get(self, url, headers=None, timeout=None):
            raise asyncio.CancelledError

    out = Recorder()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(sizer.poll(Cancelling(), out.send))

    assert out.sent[0].startswith("⚖️ sizer up")
    assert len(out.sent) == 1  # no error notice for a cancellation


def test_poll_announces_a_recovery_relapse(monkeypatch):
    """After a clean pass the same error is news again."""
    out = Recorder()
    state = {"n": 0}

    class Blinking(FakeHTTP):
        async def get(self, url, headers=None, timeout=None):
            state["n"] += 1
            if state["n"] != 2:  # fail, work, fail
                raise OSError("bybit down")
            return await super().get(url, headers=headers, timeout=timeout)

    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)
        if len(slept) >= 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(sizer.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(sizer.poll(Blinking(), out.send))

    assert out.sent[1:] == ["⚖️ sizer error: bybit down", "⚖️ sizer error: bybit down"]
