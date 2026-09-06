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
        self.requests: list[str] = []

    async def get(self, url, headers=None, timeout=None):
        self.requests.append(url)
        if "/v5/position/list" in url:
            rows = self.position_pages.pop(0) if self.position_pages else []
            return FakeResponse({"retCode": 0, "result": {"list": rows}})
        return FakeResponse({"retCode": 0, "result": {"list": self.pnl_rows}})


def row(symbol="FARTCOINUSDT", side="Buy", size="115661.3", price="0.1621", value="18748.7"):
    return {"symbol": symbol, "side": side, "size": size, "avgPrice": price, "positionValue": value}


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
        == "d026b6d817e30bf57f231da2f2e4c6cbb5b776a49bc545459332eaabf806ca3a"
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

    async def send(self, text):
        self.sent.append(text)

    async def speak(self, text):
        self.spoken.append(text)
        return True


def test_first_tick_primes_silently(keyed):
    http, out = FakeHTTP(), Recorder()
    http.position_pages = [[row()]]

    snapshot = asyncio.run(bybit_watch.tick(http, None, out.send, out.speak))

    assert "FARTCOINUSDT" in snapshot
    assert out.sent == []


def test_tick_announces_an_open(keyed):
    http, out = FakeHTTP(), Recorder()
    http.position_pages = [[row()]]

    asyncio.run(bybit_watch.tick(http, {}, out.send, out.speak))

    assert out.sent == ["💰 FARTCOIN long 18,749 USDT @ 0.1621"]
    assert out.spoken == ["Fartcoin long opened"]


def test_tick_reports_pnl_on_a_close(keyed):
    http, out = FakeHTTP(), Recorder()
    http.position_pages = [[]]
    http.pnl_rows = [{"closedPnl": "512.3"}]

    asyncio.run(bybit_watch.tick(http, {"FARTCOINUSDT": LONG}, out.send, out.speak))

    assert out.sent == ["💸 FARTCOIN long closed, PnL +512.30 USDT"]
    assert out.spoken == ["Fartcoin long closed, profit 512"]


def test_tick_close_survives_a_missing_pnl(keyed):
    http, out = FakeHTTP(), Recorder()
    http.position_pages = [[]]
    http.pnl_rows = []

    asyncio.run(bybit_watch.tick(http, {"FARTCOINUSDT": LONG}, out.send, out.speak))

    assert out.sent == ["💸 FARTCOIN long closed"]


# --------------------------------------------------------------------------- #
#  poll                                                                        #
# --------------------------------------------------------------------------- #
def test_poll_survives_failures_and_keeps_going(keyed, monkeypatch, caplog):
    """One bad poll must not end the loop, and must not wipe the snapshot."""
    out = Recorder()

    class Flaky(FakeHTTP):
        async def get(self, url, headers=None, timeout=None):
            if "/v5/position/list" in url and len(self.position_pages) == 2:
                self.position_pages.pop(0)
                raise OSError("bybit down")
            return await super().get(url, headers=headers, timeout=timeout)

    http = Flaky()
    # Prime with one long, fail once, then the position is gone -> "closed".
    http.position_pages = [[row()], ["fails"], []]

    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)
        if len(slept) >= 4:
            raise asyncio.CancelledError

    monkeypatch.setattr(bybit_watch.asyncio, "sleep", fake_sleep)

    with caplog.at_level("ERROR", logger="relay.bybit"), pytest.raises(asyncio.CancelledError):
        asyncio.run(bybit_watch.poll(http, out.send, out.speak))

    assert "bybit poll failed" in caplog.text
    assert any(seconds >= 30 for seconds in slept)  # backed off after the failure
    assert out.sent and out.sent[0].startswith("💸 FARTCOIN long closed")
