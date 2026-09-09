"""Tests for the external-IP watcher. HTTP is faked wholesale."""

import asyncio

import pytest

import ip_watch


class FakeHTTP:
    def __init__(self, answers: list[str]):
        self.answers = answers

    async def get(self, url, timeout=None):
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer

        class R:
            text = answer

        return R()


class Recorder:
    def __init__(self):
        self.sent: list[str] = []
        self.spoken: list[str] = []

    async def send(self, text):
        self.sent.append(text)

    async def speak(self, text):
        self.spoken.append(text)
        return True


def run_poll(http, out, monkeypatch, naps):
    async def fake_sleep(seconds):
        naps.append(seconds)
        if not http.answers:
            raise asyncio.CancelledError

    monkeypatch.setattr(ip_watch.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(ip_watch.poll(http, out.send, out.speak))


def test_enabled_follows_the_flag(monkeypatch):
    monkeypatch.setattr(ip_watch, "ENABLED", True)
    assert ip_watch.enabled() is True
    monkeypatch.setattr(ip_watch, "ENABLED", False)
    assert ip_watch.enabled() is False


def test_current_strips_the_answer():
    assert asyncio.run(ip_watch.current(FakeHTTP(["1.2.3.4\n"]))) == "1.2.3.4"


def test_poll_stays_quiet_while_the_ip_holds(monkeypatch):
    out, naps = Recorder(), []

    run_poll(FakeHTTP(["1.2.3.4", "1.2.3.4"]), out, monkeypatch, naps)

    assert out.sent == []


def test_poll_shouts_on_a_change(monkeypatch):
    out, naps = Recorder(), []

    run_poll(FakeHTTP(["1.2.3.4", "5.6.7.8"]), out, monkeypatch, naps)

    assert out.sent == [
        "⚠️ Внешний IP сменился: 1.2.3.4 → 5.6.7.8\n"
        "Обнови whitelist API-ключа на Bybit, иначе бот отвалится!"
    ]
    assert out.spoken == ["External IP changed, update the Bybit whitelist"]


def test_poll_survives_a_failed_check(monkeypatch, caplog):
    out, naps = Recorder(), []

    with caplog.at_level("ERROR", logger="relay.ip"):
        run_poll(FakeHTTP(["1.2.3.4", OSError("down"), "1.2.3.4"]), out, monkeypatch, naps)

    assert "ip check failed" in caplog.text
    assert out.sent == []


def test_poll_lets_cancellation_through():
    class Cancelling:
        async def get(self, url, timeout=None):
            raise asyncio.CancelledError

    out = Recorder()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(ip_watch.poll(Cancelling(), out.send, out.speak))

    assert out.sent == []
