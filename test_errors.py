"""Errors reach the chat, once per reason, without ever flooding it."""

import asyncio
import logging

import pytest

import errors


def record(name="relay.sheets", msg="sheets refused: 403", exc=None):
    return logging.LogRecord(name, logging.ERROR, __file__, 1, msg, (), exc)


# --------------------------------------------------------------------------- #
#  what counts as the same error                                               #
# --------------------------------------------------------------------------- #
def test_the_same_fault_from_the_same_module_is_one_reason():
    assert errors.reason(record()) == errors.reason(record())


def test_a_different_module_is_a_different_reason():
    assert errors.reason(record(name="relay.bybit")) != errors.reason(record())


def test_an_unformattable_record_still_has_a_reason():
    """A broken %-format must not take down the reporting of other faults."""
    bad = logging.LogRecord("relay.x", logging.ERROR, __file__, 1, "%d", ("not a number",), None)

    assert errors.reason(bad).endswith("<unformattable>")
    assert "would not format" in errors.describe(bad)


# --------------------------------------------------------------------------- #
#  the line that goes out                                                      #
# --------------------------------------------------------------------------- #
def test_the_line_names_the_module_and_the_fault():
    assert errors.describe(record()) == "⛔️ relay.sheets: sheets refused: 403"


def test_the_exception_is_named_but_its_traceback_is_not():
    """One line that says what broke; the log keeps the full story."""
    try:
        raise RuntimeError("bybit /v5/order/create: 110007 ab not enough")
    except RuntimeError as exc:
        line = errors.describe(record(msg="trim failed", exc=(type(exc), exc, exc.__traceback__)))

    assert line == "⛔️ relay.sheets: trim failed — bybit /v5/order/create: 110007 ab not enough"
    assert "Traceback" not in line


def test_a_huge_message_is_cut_to_a_readable_length():
    """403 from Google is seventeen kilobytes of HTML."""
    line = errors.describe(record(msg="sheets refused: " + "<div>" * 5000))

    assert len(line) < errors.MAX_LEN + 40
    assert line.endswith("…")


# --------------------------------------------------------------------------- #
#  the gate                                                                    #
# --------------------------------------------------------------------------- #
def test_one_standing_fault_says_itself_once():
    """The journal refused every pass for five days; that is one piece of news."""
    gate = errors.Gate(burst=4, window=60.0, repeat=900.0)

    said = [gate.admit("sheets:403", t) for t in (0.0, 1.0, 11.0, 300.0, 899.0)]

    assert said == [True, False, False, False, False]


def test_it_speaks_again_once_the_silence_has_outlived_its_reason():
    gate = errors.Gate(burst=4, window=60.0, repeat=900.0)

    assert gate.admit("sheets:403", 0.0) is True
    assert gate.admit("sheets:403", 901.0) is True


def test_different_faults_do_not_silence_each_other():
    gate = errors.Gate(burst=4, window=60.0, repeat=900.0)

    assert gate.admit("sheets:403", 0.0) is True
    assert gate.admit("bybit:110007", 0.0) is True


def test_a_storm_is_capped_and_the_rest_are_counted():
    gate = errors.Gate(burst=2, window=60.0, repeat=900.0)

    admitted = [gate.admit(f"fault-{n}", 0.0) for n in range(6)]

    assert admitted == [True, True, False, False, False, False]
    assert gate.held() == 4
    assert gate.held() == 0  # reported once, not every time


def test_the_backlog_survives_the_window_it_was_held_in():
    """Zeroing the count on a new window loses what nobody was ever told."""
    gate = errors.Gate(burst=1, window=60.0, repeat=900.0)
    gate.admit("a", 0.0)
    gate.admit("b", 1.0)  # held
    gate.admit("c", 2.0)  # held

    assert gate.admit("d", 61.0) is True  # a fresh window
    assert gate.held() == 2


def test_the_memory_of_reasons_does_not_grow_without_end():
    """A relay runs for weeks; it should not remember every fault it ever saw."""
    gate = errors.Gate(burst=10_000, window=1e9, repeat=10.0)
    for n in range(400):
        gate.admit(f"fault-{n}", float(n))

    assert len(gate._said) < 300


# --------------------------------------------------------------------------- #
#  the handler                                                                 #
# --------------------------------------------------------------------------- #
class Chat:
    def __init__(self, blow_up=False):
        self.sent: list[str] = []
        self.blow_up = blow_up

    async def send(self, text):
        if self.blow_up:
            raise OSError("telegram is down")
        self.sent.append(text)


def drive(handler, records, chat):
    async def go():
        for rec in records:
            handler.emit(rec)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(go())
    return chat.sent


def test_an_error_reaches_the_chat():
    async def go():
        chat = Chat()
        handler = errors.Telegram(asyncio.get_running_loop(), chat.send)
        handler.emit(record())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return chat.sent

    assert asyncio.run(go()) == ["⛔️ relay.sheets: sheets refused: 403"]


def test_the_reporter_does_not_report_itself():
    """A failing send that logged an error would call this handler again."""

    async def go():
        chat = Chat()
        handler = errors.Telegram(asyncio.get_running_loop(), chat.send)
        handler.emit(record(name="relay.errors", msg="could not report"))
        await asyncio.sleep(0)
        return chat.sent

    assert asyncio.run(go()) == []


def test_a_chat_that_will_not_take_it_is_survived(caplog):
    async def go():
        chat = Chat(blow_up=True)
        handler = errors.Telegram(asyncio.get_running_loop(), chat.send)
        with caplog.at_level("ERROR", logger="relay.errors"):
            handler.emit(record())
            await asyncio.sleep(0)
            await asyncio.sleep(0)

    asyncio.run(go())

    assert "could not report an error to the chat" in caplog.text


def test_a_closed_loop_does_not_raise_out_of_logging():
    """Logging is called from everywhere, including shutdown."""
    loop = asyncio.new_event_loop()
    loop.close()
    handler = errors.Telegram(loop, Chat().send)

    handler.emit(record())  # must not raise


def test_the_held_count_rides_along(monkeypatch):
    async def go():
        chat = Chat()
        handler = errors.Telegram(asyncio.get_running_loop(), chat.send)
        handler._gate = errors.Gate(burst=1, window=1e9, repeat=1e9)
        handler.emit(record(name="a"))
        handler.emit(record(name="b"))  # held
        handler.emit(record(name="c"))  # held
        handler._gate = errors.Gate(burst=1, window=1e9, repeat=1e9)
        handler._gate._held = 2
        handler.emit(record(name="d"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return chat.sent

    sent = asyncio.run(go())

    assert sent[-1].endswith("…и ещё 2 — смотрите лог")


# --------------------------------------------------------------------------- #
#  installing it                                                               #
# --------------------------------------------------------------------------- #
def test_watch_attaches_to_the_root_so_every_module_is_covered():
    async def go():
        handler = errors.watch(asyncio.get_running_loop(), Chat().send)
        assert handler is not None
        try:
            assert handler in logging.getLogger().handlers
        finally:
            logging.getLogger().removeHandler(handler)

    asyncio.run(go())


def test_watch_stays_out_of_the_way_when_switched_off(monkeypatch):
    monkeypatch.setattr(errors, "NOTIFY_ERRORS", False)

    async def go():
        before = list(logging.getLogger().handlers)
        assert errors.watch(asyncio.get_running_loop(), Chat().send) is None
        assert logging.getLogger().handlers == before

    asyncio.run(go())


@pytest.mark.parametrize("raw,on", [("1", True), ("0", False), ("no", False), ("", False)])
def test_the_switch_reads_like_every_other_one(raw, on, monkeypatch):
    monkeypatch.setenv("NOTIFY_ERRORS", raw)
    assert (raw.lower() not in ("0", "false", "no", "")) is on
