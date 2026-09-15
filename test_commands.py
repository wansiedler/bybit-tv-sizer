"""Tests for the bot command surface.

Delivery and speech are injected, so nothing here reaches Telegram or a
speaker; `fetch_updates` is driven with canned getUpdates payloads.
"""

import asyncio
from typing import Any

import httpx
import pytest

import commands


@pytest.fixture
def owner(monkeypatch):
    monkeypatch.setattr(commands, "TARGET_CHAT_ID", "777")
    monkeypatch.setattr(commands, "OWNER_ID", "777")
    monkeypatch.setattr(commands, "BOT_TOKEN", "token")
    return "777"


class Recorder:
    """Collects what would have been sent, and what would have been spoken."""

    def __init__(self, spoke=True):
        self.sent: list[str] = []
        self.spoken: list[tuple[str, int]] = []
        self._spoke = spoke

    async def send(self, text, html=False):
        self.sent.append(text)
        return True

    async def speak(self, line, counter):
        self.spoken.append((line, counter))
        return self._spoke


# --------------------------------------------------------------------------- #
#  Stats                                                                       #
# --------------------------------------------------------------------------- #
def test_stats_record_counts_relayed_and_spoken():
    stats = commands.Stats()

    stats.record("OP 📈 1.0", spoke=True)
    stats.record("BOME 📉 2.0", spoke=False)

    assert (stats.relayed, stats.spoken, stats.last_line) == (2, 1, "BOME 📉 2.0")


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(45, "0m45s"), (3 * 3600 + 12 * 60, "3h12m"), (2 * 86400 + 5 * 3600, "2d5h")],
)
def test_uptime_formats(seconds, expected):
    assert commands.uptime(seconds) == expected


def test_status_text_reports_the_state(owner, monkeypatch):
    stats = commands.Stats(relayed=3, skipped=4, spoken=2, last_line="OP 📈 1.0")
    monkeypatch.setattr(commands.time, "time", lambda: stats.started + 90)
    monkeypatch.setattr(commands, "WATCH_USERS", ["some_trader", "second"])

    text = commands.status_text(stats, speaking=True)

    assert "up 1m30s" in text
    assert "source: source_bot" in text
    assert "watching: @some_trader, @second (all chats)" in text
    assert "relayed: 3 · watched: 0 · skipped: 4 · spoken: 2" in text
    assert "speaker: on" in text
    assert "last: OP 📈 1.0" in text


def test_status_text_without_a_speaker_or_alerts(owner, monkeypatch):
    monkeypatch.setattr(commands, "WATCH_USERS", [])

    text = commands.status_text(commands.Stats(), speaking=False)

    assert "watching: — (all chats)" in text
    assert "speaker: off" in text
    assert "last: —" in text


# --------------------------------------------------------------------------- #
#  parse_watch_users                                                           #
# --------------------------------------------------------------------------- #
def test_parse_watch_users_strips_prefixes_and_blanks():
    assert commands.parse_watch_users("some_trader, @second ,, ") == ["some_trader", "second"]


def test_parse_watch_users_empty_means_nobody():
    assert commands.parse_watch_users("") == []


# --------------------------------------------------------------------------- #
#  command_of                                                                  #
# --------------------------------------------------------------------------- #
def _update(text, chat_id="777", update_id=1, sender="777", bot=False):
    message = {"chat": {"id": chat_id}, "text": text}
    if sender is not None:
        message["from"] = {"id": sender, "is_bot": bot}
    return {"update_id": update_id, "message": message}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/ping", ("ping", "")),
        ("  /Status  ", ("status", "")),
        ("/status@your_bot", ("status", "")),
        ("/test now", ("test", "now")),
        ("/close CL", ("close", "CL")),
        ("/close@your_bot  cl ", ("close", "cl")),
    ],
)
def test_command_of_parses(owner, text, expected):
    assert commands.command_of(_update(text)) == expected


def test_command_of_ignores_plain_text(owner):
    assert commands.command_of(_update("hello")) is None


def test_command_of_ignores_non_messages(owner):
    assert commands.command_of({"update_id": 1, "edited_message": {}}) is None


def test_command_of_ignores_strangers(owner, caplog):
    with caplog.at_level("WARNING", logger="relay.commands"):
        assert commands.command_of(_update("/status", chat_id="999")) is None

    assert "ignoring command from chat 999" in caplog.text


def test_command_of_ignores_other_members_of_the_chat(owner, caplog):
    with caplog.at_level("WARNING", logger="relay.commands"):
        assert commands.command_of(_update("/stopall", sender="999")) is None

    assert "ignoring command from user 999" in caplog.text


def test_command_of_ignores_bots(owner):
    assert commands.command_of(_update("/stopall", bot=True)) is None


def test_command_of_ignores_a_message_without_a_sender(owner):
    assert commands.command_of(_update("/stopall", sender=None)) is None


def test_command_of_obeys_the_owner_inside_a_group(owner, monkeypatch):
    monkeypatch.setattr(commands, "TARGET_CHAT_ID", "-100777")
    monkeypatch.setattr(commands, "OWNER_ID", "42")

    assert commands.command_of(_update("/close CL", chat_id="-100777", sender="42")) == (
        "close",
        "CL",
    )
    assert commands.command_of(_update("/close CL", chat_id="-100777", sender="777")) is None


# --------------------------------------------------------------------------- #
#  dispatch                                                                    #
# --------------------------------------------------------------------------- #
def test_ping(owner):
    rec = Recorder()

    asyncio.run(commands.dispatch("ping", "", commands.Stats(), rec.send, rec.speak, True))

    assert rec.sent == ["pong"]


def test_links_answers_with_the_link_list(owner):
    rec = Recorder()

    asyncio.run(
        commands.dispatch(
            "links",
            "",
            commands.Stats(),
            rec.send,
            rec.speak,
            True,
            commands.Handlers(links="📒 a\n📡 b"),
        )
    )

    assert rec.sent == ["📒 a\n📡 b"]


def test_links_without_links_falls_back_to_help(owner):
    rec = Recorder()

    asyncio.run(commands.dispatch("links", "", commands.Stats(), rec.send, rec.speak, True))

    assert rec.sent == [commands.HELP]


def test_lev1_passes_the_ticker(owner):
    rec = Recorder()
    seen = []

    async def lev_one(arg):
        seen.append(arg)
        return "✅ CL → 1x"

    asyncio.run(
        commands.dispatch(
            "lev1",
            "CL",
            commands.Stats(),
            rec.send,
            rec.speak,
            True,
            commands.Handlers(lev_one=lev_one),
        )
    )

    assert seen == ["CL"]
    assert rec.sent == ["✅ CL → 1x"]


def test_lev1_without_a_handler_falls_back_to_help(owner):
    rec = Recorder()

    asyncio.run(commands.dispatch("lev1", "", commands.Stats(), rec.send, rec.speak, True))

    assert rec.sent == [commands.HELP]


def test_positions_answers_with_the_report(owner):
    rec = Recorder()

    async def report():
        return "📈 FARTCOIN long 18,749 USDT @ 0.1621 · uPnL +512.30"

    asyncio.run(
        commands.dispatch(
            "positions",
            "",
            commands.Stats(),
            rec.send,
            rec.speak,
            True,
            commands.Handlers(positions=report),
        )
    )

    assert rec.sent == ["📈 FARTCOIN long 18,749 USDT @ 0.1621 · uPnL +512.30"]


def test_positions_stays_silent_after_an_album(owner):
    rec = Recorder()

    async def report():
        return ""  # the media group already carried everything

    asyncio.run(
        commands.dispatch(
            "positions",
            "",
            commands.Stats(),
            rec.send,
            rec.speak,
            True,
            commands.Handlers(positions=report),
        )
    )

    assert rec.sent == []


def test_positions_without_a_reporter_falls_back_to_help(owner):
    rec = Recorder()

    asyncio.run(commands.dispatch("positions", "", commands.Stats(), rec.send, rec.speak, True))

    assert rec.sent == [commands.HELP]


def test_stopall_answers_with_the_result(owner):
    rec = Recorder()

    async def stop_all():
        return "⚠️ Закрою МАРКЕТОМ 2 поз.: CL, GRAM"

    asyncio.run(
        commands.dispatch(
            "stopall",
            "",
            commands.Stats(),
            rec.send,
            rec.speak,
            True,
            commands.Handlers(stop_all=stop_all),
        )
    )

    assert rec.sent == ["⚠️ Закрою МАРКЕТОМ 2 поз.: CL, GRAM"]


def test_stopall_without_a_closer_falls_back_to_help(owner):
    rec = Recorder()

    asyncio.run(commands.dispatch("stopall", "", commands.Stats(), rec.send, rec.speak, True))

    assert rec.sent == [commands.HELP]


def test_close_passes_the_ticker(owner):
    rec = Recorder()
    seen = []

    async def close_one(arg):
        seen.append(arg)
        return "✅ CL закрывается — отчёт 💸 придёт следом"

    asyncio.run(
        commands.dispatch(
            "close",
            "CL",
            commands.Stats(),
            rec.send,
            rec.speak,
            True,
            commands.Handlers(close_one=close_one),
        )
    )

    assert seen == ["CL"]
    assert rec.sent == ["✅ CL закрывается — отчёт 💸 придёт следом"]


def test_close_without_a_closer_falls_back_to_help(owner):
    rec = Recorder()

    asyncio.run(commands.dispatch("close", "CL", commands.Stats(), rec.send, rec.speak, True))

    assert rec.sent == [commands.HELP]


def test_status(owner):
    rec = Recorder()

    asyncio.run(commands.dispatch("status", "", commands.Stats(), rec.send, rec.speak, True))

    assert rec.sent[0].startswith("🟢 up")


def test_test_command_exercises_delivery_and_speech(owner):
    rec = Recorder(spoke=True)
    stats = commands.Stats(relayed=4)

    asyncio.run(commands.dispatch("test", "", stats, rec.send, rec.speak, True))

    assert rec.sent == [f"{commands.SAMPLE_ALERT} (test)", "spoke it"]
    assert rec.spoken == [(commands.SAMPLE_ALERT, 5)]


def test_test_command_reports_a_silent_speaker(owner):
    rec = Recorder(spoke=False)

    asyncio.run(commands.dispatch("test", "", commands.Stats(), rec.send, rec.speak, False))

    assert rec.sent[-1] == "speaker silent"


@pytest.mark.parametrize("command", ["help", "nonsense"])
def test_unknown_commands_get_help(owner, command):
    rec = Recorder()

    asyncio.run(commands.dispatch(command, "", commands.Stats(), rec.send, rec.speak, True))

    assert rec.sent == [commands.HELP]


# --------------------------------------------------------------------------- #
#  fetch_updates                                                               #
# --------------------------------------------------------------------------- #
class FakeHTTP:
    def __init__(self, payload):
        self.payload = payload
        self.params: list[dict] = []

    async def get(self, url, params=None, timeout=None):
        self.params.append(params or {})

        class Response:
            def __init__(self, payload):
                self._payload = payload

            def json(self):
                return self._payload

        return Response(self.payload)


def test_fetch_updates_passes_the_offset(owner):
    http = FakeHTTP({"ok": True, "result": [{"update_id": 7}]})

    updates = asyncio.run(commands.fetch_updates(http, 5))

    assert updates == [{"update_id": 7}]
    assert http.params[0]["offset"] == 5


def test_fetch_updates_omits_a_missing_offset(owner):
    http = FakeHTTP({"ok": True, "result": []})

    asyncio.run(commands.fetch_updates(http, None))

    assert "offset" not in http.params[0]


def test_fetch_updates_raises_on_refusal(owner):
    # An empty list would send poll() straight back for more, hammering the API.
    http = FakeHTTP({"ok": False, "description": "Unauthorized"})
    polling = commands.fetch_updates(http, None)

    with pytest.raises(commands.Refused):
        asyncio.run(polling)


def test_poll_backs_off_when_telegram_refuses(owner, monkeypatch):
    rec = Recorder()

    _run_poll(monkeypatch, [commands.Refused("Unauthorized"), [_update("/ping", update_id=1)]], rec)

    assert rec.sent == ["pong"]


# --------------------------------------------------------------------------- #
#  poll                                                                        #
# --------------------------------------------------------------------------- #
def _run_poll(monkeypatch, batches, rec, stats=None, http=None, handlers=None):
    """Drive poll() through a fixed script of getUpdates results, then stop."""
    calls: dict[str, Any] = {"offsets": []}
    script = list(batches)

    async def fake_fetch(http, offset):
        calls["offsets"].append(offset)
        if not script:
            raise asyncio.CancelledError
        result = script.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(commands, "fetch_updates", fake_fetch)

    async def no_sleep(_seconds):
        # commands.asyncio *is* asyncio; a stub that called sleep would recurse.
        return None

    monkeypatch.setattr(commands.asyncio, "sleep", no_sleep)

    counters = stats or commands.Stats()

    async def go():
        with pytest.raises(asyncio.CancelledError):
            await commands.poll(http, counters, rec.send, rec.speak, True, handlers)

    asyncio.run(go())
    return calls


def test_poll_answers_and_advances_the_offset(owner, monkeypatch):
    rec = Recorder()

    calls = _run_poll(monkeypatch, [[_update("/ping", update_id=41)]], rec)

    assert rec.sent == ["pong"]
    assert calls["offsets"] == [None, 42]


def test_poll_skips_updates_that_are_not_commands(owner, monkeypatch):
    rec = Recorder()

    _run_poll(monkeypatch, [[_update("just chatting", update_id=1)]], rec)

    assert rec.sent == []


def test_poll_survives_a_transport_failure(owner, monkeypatch):
    rec = Recorder()

    _run_poll(
        monkeypatch,
        [httpx.ConnectError("no route"), [_update("/ping", update_id=1)]],
        rec,
    )

    assert rec.sent == ["pong"]


def test_poll_survives_a_failing_command(owner, monkeypatch, caplog):
    class Exploding(Recorder):
        async def send(self, text, html=False):
            raise RuntimeError("telegram is down")

    rec = Exploding()

    with caplog.at_level("ERROR", logger="relay.commands"):
        _run_poll(monkeypatch, [[_update("/ping", update_id=1)]], rec)

    assert "/ping failed" in caplog.text


def test_status_also_sends_the_market_snapshot(owner):
    rec = Recorder()
    shots = []

    async def market():
        shots.append(True)

    asyncio.run(
        commands.dispatch(
            "status",
            "",
            commands.Stats(),
            rec.send,
            rec.speak,
            True,
            commands.Handlers(market=market),
        )
    )

    assert rec.sent[0].startswith("🟢 up")
    assert shots == [True]


def test_statistics_sends_the_report(owner):
    rec = Recorder()

    async def statistics(arg):
        return "📊 за 30 дн.: сделок 5"

    asyncio.run(
        commands.dispatch(
            "statistics",
            "",
            commands.Stats(),
            rec.send,
            rec.speak,
            True,
            commands.Handlers(statistics=statistics),
        )
    )

    assert rec.sent == ["📊 за 30 дн.: сделок 5"]


def test_statistics_stays_silent_after_the_chart(owner):
    rec = Recorder()

    async def statistics(arg):
        return ""

    asyncio.run(
        commands.dispatch(
            "stats",
            "",
            commands.Stats(),
            rec.send,
            rec.speak,
            True,
            commands.Handlers(statistics=statistics),
        )
    )

    assert rec.sent == []


def test_statistics_without_a_reporter_falls_back_to_help(owner):
    rec = Recorder()

    asyncio.run(commands.dispatch("statistics", "", commands.Stats(), rec.send, rec.speak, True))

    assert rec.sent == [commands.HELP]


def test_status_appends_the_external_ip(owner):
    rec = Recorder()

    async def ip():
        return "203.0.113.7"

    asyncio.run(
        commands.dispatch(
            "status", "", commands.Stats(), rec.send, rec.speak, True, commands.Handlers(ip=ip)
        )
    )

    assert rec.sent[0].endswith("🌐 203.0.113.7")


def test_status_survives_a_failed_ip_lookup(owner):
    rec = Recorder()

    async def ip():
        raise OSError("down")

    asyncio.run(
        commands.dispatch(
            "status", "", commands.Stats(), rec.send, rec.speak, True, commands.Handlers(ip=ip)
        )
    )

    assert rec.sent[0].endswith("🌐 IP недоступен")


# --------------------------------------------------------------------------- #
#  exit buttons                                                                #
# --------------------------------------------------------------------------- #
def _tap(data="x:CLUSDT:25", sender="777", query_id="q1", update_id=1):
    return {
        "update_id": update_id,
        "callback_query": {"id": query_id, "from": {"id": sender}, "data": data},
    }


class TapHTTP:
    """Records the answerCallbackQuery posts a tap triggers."""

    def __init__(self):
        self.posts: list[dict] = []

    async def post(self, url, json=None, timeout=None):
        self.posts.append({"url": url, "json": json})
        return None


def test_exit_tap_reads_the_symbol_and_the_share(owner):
    assert commands.exit_tap(_tap()) == ("q1", "CLUSDT", 25.0)


def test_exit_tap_ignores_an_ordinary_message(owner):
    assert commands.exit_tap(_update("/ping")) is None


def test_exit_tap_refuses_a_stranger(owner, caplog):
    with caplog.at_level("WARNING", logger="relay.commands"):
        assert commands.exit_tap(_tap(sender="999")) is None

    assert "button tap from user 999" in caplog.text


@pytest.mark.parametrize("data", ["", "y:CLUSDT:25", "x::25"])
def test_exit_tap_ignores_foreign_button_data(owner, data):
    assert commands.exit_tap(_tap(data=data)) is None


def test_exit_tap_ignores_a_share_that_is_not_a_number(owner, caplog):
    with caplog.at_level("WARNING", logger="relay.commands"):
        assert commands.exit_tap(_tap(data="x:CLUSDT:half")) is None

    assert "no percent" in caplog.text


def test_answer_tap_stops_the_spinner(owner):
    http = TapHTTP()

    asyncio.run(commands.answer_tap(http, "q1", "25%"))

    assert http.posts[0]["url"].endswith("/answerCallbackQuery")
    assert http.posts[0]["json"] == {"callback_query_id": "q1", "text": "25%"}


def test_answer_tap_survives_a_dead_telegram(owner, caplog):
    class Refusing:
        async def post(self, url, json=None, timeout=None):
            raise httpx.ConnectError("no route")

    with caplog.at_level("ERROR", logger="relay.commands"):
        asyncio.run(commands.answer_tap(Refusing(), "q1"))

    assert "answerCallbackQuery failed" in caplog.text


def test_poll_closes_the_share_a_tap_asked_for(owner, monkeypatch):
    rec, http = Recorder(), TapHTTP()
    asked = []

    async def close_part(symbol, percent):
        asked.append((symbol, percent))
        return f"✂️ {symbol} {percent:g}%"

    _run_poll(
        monkeypatch,
        [[_tap()]],
        rec,
        http=http,
        handlers=commands.Handlers(close_part=close_part),
    )

    assert asked == [("CLUSDT", 25.0)]
    assert rec.sent == ["✂️ CLUSDT 25%"]


def test_poll_answers_a_tap_even_without_a_handler(owner, monkeypatch):
    rec, http = Recorder(), TapHTTP()

    _run_poll(monkeypatch, [[_tap()]], rec, http=http)

    assert rec.sent == []
    assert len(http.posts) == 1  # the spinner still stops


def test_poll_swallows_a_refused_tap(owner, monkeypatch):
    """A stranger's tap is neither obeyed nor treated as a command."""
    rec, http = Recorder(), TapHTTP()

    _run_poll(monkeypatch, [[_tap(sender="999")]], rec, http=http)

    assert rec.sent == []
    assert http.posts == []


def test_poll_survives_a_failing_tap(owner, monkeypatch, caplog):
    rec, http = Recorder(), TapHTTP()

    async def close_part(symbol, percent):
        raise RuntimeError("bybit is down")

    with caplog.at_level("ERROR", logger="relay.commands"):
        _run_poll(
            monkeypatch,
            [[_tap()]],
            rec,
            http=http,
            handlers=commands.Handlers(close_part=close_part),
        )

    assert "exit button failed" in caplog.text
