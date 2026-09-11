"""Tests for the external-IP watcher. HTTP is faked wholesale."""

import asyncio
from typing import cast

import httpx
import pytest

import ip_watch


def as_client(fake: object) -> httpx.AsyncClient:
    """The fakes implement only what the watcher calls; that is enough."""
    return cast(httpx.AsyncClient, fake)


class FakeHTTP:
    def __init__(self, answers: list[str | Exception]):
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
        asyncio.run(ip_watch.poll(as_client(http), out.send, out.speak))


def test_enabled_follows_the_flag(monkeypatch):
    monkeypatch.setattr(ip_watch, "ENABLED", True)
    assert ip_watch.enabled() is True
    monkeypatch.setattr(ip_watch, "ENABLED", False)
    assert ip_watch.enabled() is False


def test_current_strips_the_answer():
    assert asyncio.run(ip_watch.current(as_client(FakeHTTP(["1.2.3.4\n"])))) == "1.2.3.4"


def test_poll_stays_quiet_while_the_ip_holds(monkeypatch):
    out, naps = Recorder(), list[float]()

    run_poll(FakeHTTP(["1.2.3.4", "1.2.3.4"]), out, monkeypatch, naps)

    assert out.sent == []


def test_poll_shouts_on_a_change(monkeypatch):
    out, naps = Recorder(), list[float]()

    run_poll(FakeHTTP(["1.2.3.4", "5.6.7.8"]), out, monkeypatch, naps)

    assert out.sent == [
        "⚠️ Внешний IP сменился: 1.2.3.4 → 5.6.7.8\n"
        "Обнови whitelist API-ключа на Bybit, иначе бот отвалится!"
    ]
    assert out.spoken == ["External IP changed, update the Bybit whitelist"]


def test_poll_survives_a_failed_check(monkeypatch, caplog):
    out, naps = Recorder(), list[float]()

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
        asyncio.run(ip_watch.poll(as_client(Cancelling()), out.send, out.speak))

    assert out.sent == []


def run_rounds(http, out, monkeypatch, rounds):
    """Drive the loop for a fixed number of turns; a down tunnel drains nothing."""
    naps: list[float] = []

    async def fake_sleep(seconds):
        naps.append(seconds)
        if len(naps) >= rounds:
            raise asyncio.CancelledError

    monkeypatch.setattr(ip_watch.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(ip_watch.poll(as_client(http), out.send, out.speak))


def test_proxy_endpoint_reads_the_variable(monkeypatch):
    monkeypatch.setattr(ip_watch, "PROXY", "http://10.77.0.1:8888")
    assert ip_watch.proxy_endpoint() == ("10.77.0.1", 8888)


def test_proxy_endpoint_accepts_a_bare_host_and_port(monkeypatch):
    monkeypatch.setattr(ip_watch, "PROXY", "10.77.0.1:8888")
    assert ip_watch.proxy_endpoint() == ("10.77.0.1", 8888)


def test_proxy_endpoint_defaults_the_port(monkeypatch):
    monkeypatch.setattr(ip_watch, "PROXY", "http://10.77.0.1")
    assert ip_watch.proxy_endpoint() == ("10.77.0.1", 80)


def test_proxy_endpoint_is_none_without_a_proxy(monkeypatch):
    monkeypatch.setattr(ip_watch, "PROXY", "")
    assert ip_watch.proxy_endpoint() is None


def test_proxy_endpoint_is_none_when_the_value_names_no_host(monkeypatch):
    monkeypatch.setattr(ip_watch, "PROXY", "http://")
    assert ip_watch.proxy_endpoint() is None


def test_reachable_finds_a_listening_socket():
    async def scenario():
        server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            return await ip_watch.reachable("127.0.0.1", port)
        finally:
            server.close()
            await server.wait_closed()

    assert asyncio.run(scenario()) is True


def test_reachable_says_no_when_nothing_listens():
    async def scenario():
        # Bind and drop it: the port is known free, so the connect is refused
        # rather than left hanging.
        server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        server.close()
        await server.wait_closed()
        return await ip_watch.reachable("127.0.0.1", port)

    assert asyncio.run(scenario()) is False


def _tunnel(monkeypatch, *states):
    """Point the watcher at a proxy whose reachability follows `states`."""
    monkeypatch.setattr(ip_watch, "PROXY", "http://10.77.0.1:8888")
    answers = list(states)

    async def reachable(host, port):
        return answers.pop(0) if len(answers) > 1 else answers[0]

    monkeypatch.setattr(ip_watch, "reachable", reachable)


def test_poll_stays_quiet_while_the_tunnel_holds(monkeypatch):
    out, naps = Recorder(), list[float]()
    _tunnel(monkeypatch, True)

    run_poll(FakeHTTP(["1.2.3.4", "1.2.3.4"]), out, monkeypatch, naps)

    assert out.sent == []


def test_poll_shouts_once_when_the_tunnel_dies(monkeypatch):
    out = Recorder()
    _tunnel(monkeypatch, False)

    run_rounds(FakeHTTP([]), out, monkeypatch, 3)

    assert out.sent == [
        "🚨 Туннель до 10.77.0.1:8888 не отвечает — бот отрезан от Bybit.\n"
        "API-ключ ходит только через него. Проверь WireGuard на VPS!"
    ]
    assert out.spoken == ["Tunnel down, Bybit unreachable"]


def test_poll_skips_the_ip_check_while_the_tunnel_is_down(monkeypatch):
    out = Recorder()
    _tunnel(monkeypatch, False)
    http = FakeHTTP(["1.2.3.4"])

    run_rounds(http, out, monkeypatch, 2)

    assert http.answers == ["1.2.3.4"]  # never asked


def test_poll_announces_the_tunnel_coming_back(monkeypatch):
    out = Recorder()
    _tunnel(monkeypatch, False, True)

    run_rounds(FakeHTTP(["1.2.3.4"]), out, monkeypatch, 2)

    assert out.sent[-1] == "✅ Туннель до 10.77.0.1:8888 снова на связи"
    assert out.spoken[-1] == "Tunnel back up"


def test_poll_says_nothing_on_a_first_check_that_works(monkeypatch):
    out = Recorder()
    _tunnel(monkeypatch, True)

    run_rounds(FakeHTTP(["1.2.3.4"]), out, monkeypatch, 1)

    assert out.sent == []


def test_reachable_lets_cancellation_through(monkeypatch):
    async def cancelling(host, port):
        raise asyncio.CancelledError

    monkeypatch.setattr(ip_watch.asyncio, "open_connection", cancelling)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(ip_watch.reachable("10.77.0.1", 8888))
