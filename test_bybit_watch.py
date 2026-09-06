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
        self.requests: list[str] = []

    async def get(self, url, headers=None, timeout=None):
        self.requests.append(url)
        if "/v5/position/list" in url:
            rows = self.position_pages.pop(0) if self.position_pages else []
            return FakeResponse({"retCode": 0, "result": {"list": rows}})
        if "/v5/market/kline" in url:
            return FakeResponse({"retCode": 0, "result": {"list": self.kline_rows}})
        return FakeResponse({"retCode": 0, "result": {"list": self.pnl_rows}})


def row(
    symbol="FARTCOINUSDT",
    side="Buy",
    size="115661.3",
    price="0.1621",
    value="18748.7",
    tp="",
    sl="",
):
    return {
        "symbol": symbol,
        "side": side,
        "size": size,
        "avgPrice": price,
        "positionValue": value,
        "takeProfit": tp,
        "stopLoss": sl,
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
            "💰 FARTCOIN long 18,749 USDT @ 0.1621",
            "Fartcoin long opened",
        ),
        ("closed", LONG, None, "💸 FARTCOIN long closed", "Fartcoin long closed"),
        (
            "changed",
            LONG,
            BIGGER,
            "💰 FARTCOIN long increased 16 → 32 USDT",
            "Fartcoin long increased",
        ),
        (
            "changed",
            BIGGER,
            LONG,
            "💰 FARTCOIN long reduced 32 → 16 USDT",
            "Fartcoin long reduced",
        ),
        (
            "flipped",
            LONG,
            SHORT,
            "💰 FARTCOIN flipped to short 16 USDT @ 0.16",
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

    assert out.sent == ["💰 FARTCOIN long 18,749 USDT @ 0.1621"]
    assert out.spoken == ["Fartcoin long opened"]


KLINES = [
    ["1700000900000", "0.163", "0.170", "0.161", "0.169", "1", "1"],
    ["1700000000000", "0.160", "0.165", "0.158", "0.163", "1", "1"],
]


def test_tick_sends_an_entry_chart_when_candles_exist(keyed):
    http, out = FakeHTTP(), Recorder()
    http.position_pages = [[row(tp="0.19", sl="0.15")]]
    http.kline_rows = KLINES

    asyncio.run(bybit_watch.tick(http, {}, out.send, out.speak, out.send_photo))

    assert out.photos == ["💰 FARTCOIN long 18,749 USDT @ 0.1621"]
    assert out.sent == []  # the caption carries the notice
    assert out.spoken == ["Fartcoin long opened"]


def test_tick_falls_back_to_text_when_telegram_refuses_the_photo(keyed):
    http, out = FakeHTTP(), Recorder()
    http.position_pages = [[row()]]
    http.kline_rows = KLINES
    out.photo_ok = False

    asyncio.run(bybit_watch.tick(http, {}, out.send, out.speak, out.send_photo))

    assert out.photos == ["💰 FARTCOIN long 18,749 USDT @ 0.1621"]
    assert out.sent == ["💰 FARTCOIN long 18,749 USDT @ 0.1621"]


def test_tick_sends_a_close_chart_with_the_pnl_caption(keyed):
    http, out = FakeHTTP(), Recorder()
    http.position_pages = [[]]
    http.pnl_rows = [closed()]
    http.kline_rows = KLINES

    asyncio.run(bybit_watch.tick(http, {"FARTCOINUSDT": LONG}, out.send, out.speak, out.send_photo))

    assert out.photos == ["💸 FARTCOIN long closed, PnL +512.30 USDT"]
    assert out.sent == []
    assert out.spoken == ["Fartcoin long closed, profit 512"]


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


def closed(pnl="512.3", entry="0.16", exit_price="0.17"):
    return {
        "closedPnl": pnl,
        "createdTime": "1700000000000",
        "updatedTime": "1700003600000",
        "avgEntryPrice": entry,
        "avgExitPrice": exit_price,
    }


def test_tick_reports_a_resize_as_plain_text(keyed):
    http, out = FakeHTTP(), Recorder()
    http.position_pages = [[row(size="231322.6", value="37497.4")]]

    asyncio.run(bybit_watch.tick(http, {"FARTCOINUSDT": LONG}, out.send, out.speak, out.send_photo))

    assert out.photos == []
    assert out.sent == ["💰 FARTCOIN long increased 16 → 37,497 USDT"]


def test_tick_reports_pnl_on_a_close(keyed):
    http, out = FakeHTTP(), Recorder()
    http.position_pages = [[]]
    http.pnl_rows = [closed()]

    asyncio.run(bybit_watch.tick(http, {"FARTCOINUSDT": LONG}, out.send, out.speak, out.send_photo))

    assert out.sent == ["💸 FARTCOIN long closed, PnL +512.30 USDT"]
    assert out.spoken == ["Fartcoin long closed, profit 512"]


def test_tick_close_survives_a_missing_pnl(keyed):
    http, out = FakeHTTP(), Recorder()
    http.position_pages = [[]]
    http.pnl_rows = []

    asyncio.run(bybit_watch.tick(http, {"FARTCOINUSDT": LONG}, out.send, out.speak, out.send_photo))

    assert out.sent == ["💸 FARTCOIN long closed"]


# --------------------------------------------------------------------------- #
#  poll                                                                        #
# --------------------------------------------------------------------------- #
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
    assert out.sent and out.sent[0].startswith("💸 FARTCOIN long closed")


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
