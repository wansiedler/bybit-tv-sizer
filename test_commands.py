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
    monkeypatch.setattr(commands, "BOT_TOKEN", "token")
    return "777"


class Recorder:
    """Collects what would have been sent, and what would have been spoken."""

    def __init__(self, spoke=True):
        self.sent: list[str] = []
        self.spoken: list[tuple[str, int]] = []
        self._spoke = spoke

    async def send(self, text):
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

    text = commands.status_text(stats, speaking=True)

    assert "up 1m30s" in text
    assert "relayed: 3 · skipped: 4 · spoken: 2" in text
    assert "speaker: on" in text
    assert "last: OP 📈 1.0" in text


def test_status_text_without_a_speaker_or_alerts(owner):
    text = commands.status_text(commands.Stats(), speaking=False)

    assert "speaker: off" in text
    assert "last: —" in text


# --------------------------------------------------------------------------- #
#  command_of                                                                  #
# --------------------------------------------------------------------------- #
def _update(text, chat_id="777", update_id=1):
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/ping", "ping"),
        ("  /Status  ", "status"),
        ("/status@your_bot", "status"),
        ("/test now", "test"),
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


# --------------------------------------------------------------------------- #
#  dispatch                                                                    #
# --------------------------------------------------------------------------- #
def test_ping(owner):
    rec = Recorder()

    asyncio.run(commands.dispatch("ping", commands.Stats(), rec.send, rec.speak, True))

    assert rec.sent == ["pong"]


def test_status(owner):
    rec = Recorder()

    asyncio.run(commands.dispatch("status", commands.Stats(), rec.send, rec.speak, True))

    assert rec.sent[0].startswith("🟢 up")


def test_test_command_exercises_delivery_and_speech(owner):
    rec = Recorder(spoke=True)
    stats = commands.Stats(relayed=4)

    asyncio.run(commands.dispatch("test", stats, rec.send, rec.speak, True))

    assert rec.sent == [f"{commands.SAMPLE_ALERT} (test)", "spoke it"]
    assert rec.spoken == [(commands.SAMPLE_ALERT, 5)]


def test_test_command_reports_a_silent_speaker(owner):
    rec = Recorder(spoke=False)

    asyncio.run(commands.dispatch("test", commands.Stats(), rec.send, rec.speak, False))

    assert rec.sent[-1] == "speaker silent"


@pytest.mark.parametrize("command", ["help", "nonsense"])
def test_unknown_commands_get_help(owner, command):
    rec = Recorder()

    asyncio.run(commands.dispatch(command, commands.Stats(), rec.send, rec.speak, True))

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


def test_fetch_updates_on_refusal(owner, caplog):
    http = FakeHTTP({"ok": False, "description": "Unauthorized"})

    with caplog.at_level("WARNING", logger="relay.commands"):
        assert asyncio.run(commands.fetch_updates(http, None)) == []

    assert "getUpdates refused" in caplog.text


# --------------------------------------------------------------------------- #
#  poll                                                                        #
# --------------------------------------------------------------------------- #
def _run_poll(monkeypatch, batches, rec, stats=None):
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

    async def go():
        with pytest.raises(asyncio.CancelledError):
            await commands.poll(None, stats or commands.Stats(), rec.send, rec.speak, True)

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
        async def send(self, text):
            raise RuntimeError("telegram is down")

    rec = Exploding()

    with caplog.at_level("ERROR", logger="relay.commands"):
        _run_poll(monkeypatch, [[_update("/ping", update_id=1)]], rec)

    assert "/ping failed" in caplog.text
