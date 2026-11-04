"""Tests for the TradingView webhook receiver: real ephemeral port, loopback."""

import asyncio
import urllib.error
import urllib.request

import pytest

import tv_alerts


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setattr(tv_alerts, "TV_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setattr(tv_alerts, "TV_PORT", 0)  # ephemeral: no clash in CI


def test_enabled_follows_the_secret(monkeypatch):
    assert tv_alerts.enabled() is True
    monkeypatch.setattr(tv_alerts, "TV_WEBHOOK_SECRET", "")
    assert tv_alerts.enabled() is False


def post(port, path, body=b"CL broke 92"):
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body, method="POST")
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status


def test_webhook_queues_the_alert_text():
    async def run():
        queue: asyncio.Queue = asyncio.Queue()
        httpd = tv_alerts.serve(asyncio.get_running_loop(), queue)
        try:
            port = httpd.server_address[1]
            status = await asyncio.to_thread(post, port, "/tv/s3cret")
            assert status == 200
            return await asyncio.wait_for(queue.get(), timeout=5)
        finally:
            httpd.shutdown()

    assert asyncio.run(run()) == "CL broke 92"


def test_webhook_hangs_up_on_a_wrong_secret():
    async def run():
        queue: asyncio.Queue = asyncio.Queue()
        httpd = tv_alerts.serve(asyncio.get_running_loop(), queue)
        try:
            port = httpd.server_address[1]
            # Not an HTTP error: the connection dies without a single byte.
            with pytest.raises((ConnectionError, urllib.error.URLError)):
                await asyncio.to_thread(post, port, "/tv/wrong")
            assert queue.empty()
        finally:
            httpd.shutdown()

    asyncio.run(run())


def test_webhook_ignores_an_empty_body():
    async def run():
        queue: asyncio.Queue = asyncio.Queue()
        httpd = tv_alerts.serve(asyncio.get_running_loop(), queue)
        try:
            port = httpd.server_address[1]
            assert await asyncio.to_thread(post, port, "/tv/s3cret", b"") == 200
            assert queue.empty()
        finally:
            httpd.shutdown()

    asyncio.run(run())


def test_webhook_caps_a_huge_body():
    async def run():
        queue: asyncio.Queue = asyncio.Queue()
        httpd = tv_alerts.serve(asyncio.get_running_loop(), queue)
        try:
            port = httpd.server_address[1]
            await asyncio.to_thread(post, port, "/tv/s3cret", b"x" * 10_000)
            return await asyncio.wait_for(queue.get(), timeout=5)
        finally:
            httpd.shutdown()

    assert len(asyncio.run(run())) == tv_alerts.MAX_BODY


def test_get_shows_the_alive_page_only_on_the_secret_path():
    async def run():
        queue: asyncio.Queue = asyncio.Queue()
        httpd = tv_alerts.serve(asyncio.get_running_loop(), queue)
        try:
            port = httpd.server_address[1]

            def get(path):
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}{path}", timeout=5
                ) as response:
                    return response.status, response.read()

            status, body = await asyncio.to_thread(get, "/tv/s3cret")
            assert status == 200
            assert b"bybit-tv-sizer" in body

            # Every other path gets no HTTP answer at all.
            for path in ("/", "/tv/wrong", "/anything", "/tv/s3cret/"):
                with pytest.raises((ConnectionError, urllib.error.URLError)):
                    await asyncio.to_thread(get, path)
            assert queue.empty()
        finally:
            httpd.shutdown()

    asyncio.run(run())


def test_request_logging_goes_to_the_logger(caplog):
    handler = tv_alerts._Handler.__new__(tv_alerts._Handler)

    with caplog.at_level("DEBUG", logger="relay.tv"):
        tv_alerts._Handler.log_message(handler, "%s hit", "/tv/x")

    assert "/tv/x hit" in caplog.text


# --------------------------------------------------------------------------- #
#  pump                                                                        #
# --------------------------------------------------------------------------- #
class Recorder:
    def __init__(self):
        self.sent: list[str] = []
        self.spoken: list[str] = []
        self.fail_once = False

    async def send(self, text):
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("telegram down")
        self.sent.append(text)

    async def speak(self, text):
        self.spoken.append(text)
        return True


def drive(queue_items, out):
    async def run():
        queue: asyncio.Queue = asyncio.Queue()
        for item in queue_items:
            queue.put_nowait(item)
        task = asyncio.create_task(tv_alerts.pump(queue, out.send, out.speak))
        await asyncio.sleep(0.05)  # let the pump drain the queue
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())


def test_pump_announces_and_speaks():
    out = Recorder()

    drive(["CL broke 92"], out)

    assert out.sent == ["CL broke 92"]
    assert out.spoken == ["CL broke 92"]


def test_pump_survives_a_failing_send(caplog):
    out = Recorder()
    out.fail_once = True

    with caplog.at_level("ERROR", logger="relay.tv"):
        drive(["first", "second"], out)

    assert out.sent == ["second"]  # the first failed, the loop lived
    assert "could not announce" in caplog.text


def test_pump_lets_cancellation_through():
    class Cancelling:
        async def send(self, text):
            raise asyncio.CancelledError

        async def speak(self, text):
            return True

    async def run():
        queue: asyncio.Queue = asyncio.Queue()
        queue.put_nowait("x")
        pumping = tv_alerts.pump(queue, Cancelling().send, Cancelling().speak)
        with pytest.raises(asyncio.CancelledError):
            await pumping

    asyncio.run(run())


# --------------------------------------------------------------------------- #
#  format_alert                                                                #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "price", "line", "spoken"),
    [
        ("ETHUSDT.P Crossing 2,440.85", 2500.0, "ETH 📉 2,440", "Ethereum down, 2440"),
        ("ETHUSDT.P Crossing 2,440.85", 2400.0, "ETH 📈 2,440", "Ethereum up, 2440"),
        ("ETHUSDT.P Crossing 2,440.85", None, "ETH 2,440", "Ethereum, 2440"),
        ("BTCUSDT Crossing Up 76,893.7", None, "BTC 📈 76,893", "Bitcoin up, 76900"),
        ("op Crossing down 0.1027", None, "OP 📉 0.1027", "Optimism down, 0.103"),
    ],
)
def test_format_alert_compacts_crossings(text, price, line, spoken):
    assert tv_alerts.format_alert(text, price) == (line, spoken)


# --- cents on a four-figure price ------------------------------------------
#
# 04.11.2026: "BTC 📈 78,787.0" — a tenth of a dollar on seventy-eight
# thousand, two characters that say nothing and crowd the number that does.


@pytest.mark.parametrize(
    ("level", "shown"),
    [
        ("78,787.90", "78,787"),  # cents on BTC: gone
        ("78,787.0", "78,787"),
        ("1,000.5", "1,000"),  # exactly at the threshold
        ("999.99", "999.99"),  # just below it: every digit kept
        ("0.1027", "0.1027"),  # a cheap coin is all fraction
        ("78,787", "78,787"),  # nothing to drop
    ],
)
def test_a_big_price_loses_its_fraction(level, shown):
    assert tv_alerts._whole(level) == shown


def test_a_level_that_is_not_a_number_is_left_alone():
    """The regex admits a lone comma; it must not become an int()."""
    assert tv_alerts._whole(",.5") == ",.5"


# --- dominance -------------------------------------------------------------
#
# "USDT.D Crossing 6.651%" used to fall through raw: the regex knew .P but
# not .D, and the trailing percent ended the level where it expected digits.


@pytest.mark.parametrize(
    ("text", "price", "line", "spoken"),
    [
        ("USDT.D Crossing 6.651%", None, "USDT.D 6.651%", "Tether dominance, 6.65"),
        ("BTC.D Crossing Up 58.2%", None, "BTC.D 📈 58.2%", "Bitcoin dominance up, 58.2"),
        # The percent is stripped before the level is read as a number.
        ("USDT.D Crossing 6.651%", 7.0, "USDT.D 📉 6.651%", "Tether dominance down, 6.65"),
        # A dominance quoted without its percent sign is still a dominance.
        ("btc.d Crossing Down 58.2", None, "BTC.D 📉 58.2", "Bitcoin dominance down, 58.2"),
    ],
)
def test_format_alert_reads_a_dominance(text, price, line, spoken):
    assert tv_alerts.format_alert(text, price) == (line, spoken)


def test_pump_does_not_ask_the_price_of_a_dominance():
    """Nothing trades USDT.D, so the lookup could only fail — loudly."""

    async def run():
        queue: asyncio.Queue = asyncio.Queue()
        out = Recorder()
        asked = []

        async def price_of(symbol):
            asked.append(symbol)  # pragma: no cover - the point is it is not called
            return 1.0

        task = asyncio.create_task(tv_alerts.pump(queue, out.send, out.speak, price_of))
        await queue.put("USDT.D Crossing 6.651%")
        while not out.sent:
            await asyncio.sleep(0)
        task.cancel()
        return asked, out

    asked, out = asyncio.run(run())

    assert asked == []
    assert out.sent == ["USDT.D 6.651%"]


def test_format_alert_passes_unknown_shapes_through():
    line, spoken = tv_alerts.format_alert("strategy fired on something")

    assert line == "strategy fired on something"
    assert spoken == "strategy fired on something"


def test_pump_orients_the_arrow_by_market_price():
    async def run():
        queue: asyncio.Queue = asyncio.Queue()
        out = Recorder()
        asked = []

        async def price_of(symbol):
            asked.append(symbol)
            return 2500.0  # the market came from above: a downward cross

        task = asyncio.create_task(tv_alerts.pump(queue, out.send, out.speak, price_of))
        await queue.put("ETHUSDT.P Crossing 2,440.85")
        while not out.sent:
            await asyncio.sleep(0)
        task.cancel()
        return asked, out

    asked, out = asyncio.run(run())

    assert asked == ["ETH"]
    assert out.sent == ["ETH 📉 2,440"]
    assert out.spoken == ["Ethereum down, 2440"]


def test_pump_survives_a_failing_price_lookup(caplog):
    async def run():
        queue: asyncio.Queue = asyncio.Queue()
        out = Recorder()

        async def price_of(symbol):
            raise OSError("tickers down")

        task = asyncio.create_task(tv_alerts.pump(queue, out.send, out.speak, price_of))
        await queue.put("ETHUSDT.P Crossing 2,440.85")
        while not out.sent:
            await asyncio.sleep(0)
        task.cancel()
        return out

    with caplog.at_level("ERROR", logger="relay.tv"):
        out = asyncio.run(run())

    assert out.sent == ["ETH 2,440"]  # no arrow, alert still delivered
    assert "price lookup failed" in caplog.text


# --- the same alert twice ---------------------------------------------------
#
# 02.11.2026: TradingView sent "Тень Binance спот вылезла за Bybit" twice,
# forty seconds apart. Both landed in the chat stamped 13:02 and looked for
# all the world like the bot had hiccuped.


def test_the_same_alert_twice_is_announced_once(caplog):
    out = Recorder()

    with caplog.at_level("INFO", logger="relay.tv"):
        drive(["Тень Binance спот вылезла за Bybit"] * 2, out)

    assert out.sent == ["Тень Binance спот вылезла за Bybit"]
    assert out.spoken == ["Тень Binance спот вылезла за Bybit"]
    assert "not repeating" in caplog.text


def test_two_different_alerts_both_get_through():
    """Crossing 6.708% and 6.730% are two things happening, not one twice."""
    out = Recorder()

    drive(["USDT.D Crossing 6.708%", "USDT.D Crossing 6.730%"], out)

    assert len(out.sent) == 2


def test_the_window_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(tv_alerts, "TV_REPEAT_SEC", 0.0)
    out = Recorder()

    drive(["CL broke 92"] * 3, out)

    assert len(out.sent) == 3


def test_a_repeat_after_the_window_is_news_again():
    said: dict[str, float] = {}

    assert tv_alerts._already_said(said, "same", 0.0) is False
    tv_alerts._mark_said(said, "same", 0.0)
    assert tv_alerts._already_said(said, "same", 10.0) is True
    # TV_REPEAT_SEC later, the alert is a new event rather than an echo.
    assert tv_alerts._already_said(said, "same", tv_alerts.TV_REPEAT_SEC + 1) is False


def test_the_memory_does_not_grow_without_end():
    """A relay runs for weeks; it must not remember every alert it saw."""
    said = {f"alert {n}": 0.0 for n in range(300)}

    tv_alerts._mark_said(said, "one more", tv_alerts.TV_REPEAT_SEC + 1)

    assert len(said) == 1  # the stale ones went, the new one stayed


def test_an_alert_that_never_reached_the_chat_is_not_suppressed():
    """A failed send must not silence the retry that would have carried it.

    Found by review: marking before delivery meant the one delivery this
    whole thing exists to collapse — TradingView repeating itself — was
    also the one it would swallow after a failure.
    """

    class Breaking(Recorder):
        def __init__(self):
            super().__init__()
            self.tries = 0

        async def send(self, text):
            self.tries += 1
            if self.tries == 1:
                raise OSError("telegram refused")
            self.sent.append(text)

    out = Breaking()

    drive(["CL broke 92"] * 2, out)

    assert out.tries == 2  # the second attempt was made, not skipped
    assert out.sent == ["CL broke 92"]  # and it arrived
