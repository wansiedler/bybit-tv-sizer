"""Tests for the relay itself: config, delivery, lifecycle and shutdown.

Nothing here touches the network or Telegram. `TelegramClient` and
`httpx.AsyncClient` are replaced with fakes, and the signal handlers the relay
installs are captured through a recording event loop so the shutdown path can
be driven deterministically instead of by actually killing the test process.
"""

import asyncio
import signal
from types import SimpleNamespace
from typing import cast

import httpx
import pytest

import relay


# --------------------------------------------------------------------------- #
#  Fakes                                                                       #
# --------------------------------------------------------------------------- #
class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        # Telegram answers {"ok": true} on success; the relay checks it.
        self._payload = payload if payload is not None else {"ok": True}
        self.text = text

    def json(self):
        return self._payload


class FakeHTTP:
    """Stands in for httpx.AsyncClient: records posts, replays canned answers."""

    def __init__(self, post_response=None, post_error=None, get_response=None):
        self._post_response = post_response or FakeResponse()
        self._post_error = post_error
        self._get_response = get_response or FakeResponse(
            payload={"ok": True, "result": {"username": "bot"}}
        )
        self.posted: list[object] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, data=None, files=None, timeout=None):
        if self._post_error is not None:
            raise self._post_error
        if json is not None:
            self.posted.append(json["text"])
        elif "media" in (data or {}):
            import json as _json

            media = _json.loads(data["media"])
            self.posted.append((media[0].get("caption", ""), len(media), len(files or {})))
        else:
            self.posted.append(data["caption"])
        return self._post_response

    async def get(self, url, timeout=None):
        return self._get_response


class FakeClient:
    """Stands in for telethon's TelegramClient."""

    instances: list["FakeClient"] = []

    def __init__(self, session, api_id, api_hash):
        self.session = session
        self.api_id = api_id
        self.api_hash = api_hash
        self.handlers: list = []
        self.disconnected = False
        self.entity_error = None
        self.on_run = None
        FakeClient.instances.append(self)

    @property
    def handler(self):
        """The first registered handler — the alert relay."""
        return self.handlers[0]

    def on(self, event):
        def register(fn):
            self.handlers.append(fn)
            return fn

        return register

    async def start(self):
        return self

    async def get_me(self):
        return SimpleNamespace(username="tester", id=42)

    async def get_entity(self, name):
        if self.entity_error is not None:
            raise self.entity_error
        return SimpleNamespace(id=99)

    async def run_until_disconnected(self):
        if self.on_run is not None:
            await self.on_run(self)

    async def disconnect(self):
        self.disconnected = True


def as_client(fake: FakeHTTP) -> httpx.AsyncClient:
    """The fake implements only what the relay calls; tell mypy that is enough."""
    return cast(httpx.AsyncClient, fake)


class RecordingLoop:
    """Wraps the real loop so the relay's signal handlers can be called by hand."""

    def __init__(self, real, sink):
        self._real = real
        self._sink = sink

    def add_signal_handler(self, sig, callback, *args):
        self._sink[sig] = (callback, args)

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.fixture
def config(monkeypatch):
    """A complete, valid configuration."""
    monkeypatch.setattr(relay, "API_ID", "12345")
    monkeypatch.setattr(relay, "API_HASH", "cafe")
    monkeypatch.setattr(relay, "BOT_TOKEN", "token")
    monkeypatch.setattr(relay, "TARGET_CHAT_ID", "777")
    monkeypatch.setattr(relay, "NOTIFY_LIFECYCLE", True)
    monkeypatch.setattr(relay, "WATCH_USERS", ["some_trader"])


@pytest.fixture(autouse=True)
def _reset_clients():
    FakeClient.instances.clear()
    yield
    FakeClient.instances.clear()


# --------------------------------------------------------------------------- #
#  require_config                                                              #
# --------------------------------------------------------------------------- #
def test_require_config_returns_credentials(config):
    assert relay.require_config() == (12345, "cafe")


def test_require_config_lists_every_missing_name(monkeypatch):
    monkeypatch.setattr(relay, "API_ID", None)
    monkeypatch.setattr(relay, "API_HASH", "")
    monkeypatch.setattr(relay, "BOT_TOKEN", "token")
    monkeypatch.setattr(relay, "TARGET_CHAT_ID", None)

    with pytest.raises(SystemExit) as exit_info:
        relay.require_config()

    message = str(exit_info.value)
    assert "TG_API_ID" in message
    assert "TG_API_HASH" in message
    assert "TARGET_CHAT_ID" in message
    assert "BOT_TOKEN" not in message


def test_require_config_rejects_non_numeric_api_id(config, monkeypatch):
    monkeypatch.setattr(relay, "API_ID", "not-a-number")

    with pytest.raises(SystemExit) as exit_info:
        relay.require_config()

    assert "must be numeric" in str(exit_info.value)


# --------------------------------------------------------------------------- #
#  send_via_bot                                                                #
# --------------------------------------------------------------------------- #
def test_send_via_bot_accepted(config):
    http = FakeHTTP()

    assert asyncio.run(relay.send_via_bot(as_client(http), "OP 📈 1.0")) is True
    assert http.posted == ["OP 📈 1.0"]


def test_send_via_bot_rejected_by_telegram(config):
    http = FakeHTTP(post_response=FakeResponse(status_code=400, text="bad chat"))

    assert asyncio.run(relay.send_via_bot(as_client(http), "OP 📈 1.0")) is False


def test_send_via_bot_rejects_a_200_that_says_no(config):
    # Telegram reports refusals in the body; a 200 alone is not delivery.
    http = FakeHTTP(post_response=FakeResponse(payload={"ok": False, "description": "blocked"}))

    assert asyncio.run(relay.send_via_bot(as_client(http), "OP 📈 1.0")) is False


def test_send_via_bot_rejects_a_body_that_is_not_json(config):
    class NotJSON(FakeResponse):
        def json(self):
            raise ValueError("no json here")

    http = FakeHTTP(post_response=NotJSON(text="<html>gateway</html>"))

    assert asyncio.run(relay.send_via_bot(as_client(http), "OP 📈 1.0")) is False


def test_send_via_bot_survives_transport_error(config):
    http = FakeHTTP(post_error=httpx.ConnectError("no route"))

    assert asyncio.run(relay.send_via_bot(as_client(http), "OP 📈 1.0")) is False


# --------------------------------------------------------------------------- #
#  human                                                                       #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (9, "9s"),
        (65, "1m5s"),
        (3 * 3600 + 12 * 60, "3h12m"),
        (2 * 86400 + 5 * 3600, "2d5h"),
    ],
)
def test_human_uptime(seconds, expected):
    assert relay.human(seconds) == expected


# --------------------------------------------------------------------------- #
#  send_photo_via_bot                                                          #
# --------------------------------------------------------------------------- #
def test_send_photo_posts_the_caption_and_accepts(config):
    http = FakeHTTP()

    ok = asyncio.run(relay.send_photo_via_bot(as_client(http), "💰 entry", b"\x89PNGfake"))

    assert ok is True
    assert http.posted == ["💰 entry"]


def test_send_photo_reports_transport_failure(config, caplog):
    http = FakeHTTP(post_error=relay.httpx.ConnectError("no route"))

    with caplog.at_level("ERROR", logger="relay"):
        ok = asyncio.run(relay.send_photo_via_bot(as_client(http), "💰 entry", b"png"))

    assert ok is False
    assert "sendPhoto failed" in caplog.text


def test_send_photo_reports_a_refusal(config, caplog):
    http = FakeHTTP(post_response=FakeResponse(payload={"ok": False, "description": "too big"}))

    with caplog.at_level("ERROR", logger="relay"):
        ok = asyncio.run(relay.send_photo_via_bot(as_client(http), "💰 entry", b"png"))

    assert ok is False
    assert "sendPhoto refused" in caplog.text


def test_send_via_bot_html_mode_sets_parse_mode(config):
    class CapturingHTTP(FakeHTTP):
        def __init__(self):
            super().__init__()
            self.payloads: list[dict] = []

        async def post(self, url, json=None, data=None, files=None, timeout=None):
            self.payloads.append(json)
            return self._post_response

    http = CapturingHTTP()

    assert asyncio.run(relay.send_via_bot(as_client(http), "<b>x</b>", html=True)) is True
    assert http.payloads[0]["parse_mode"] == "HTML"


# --------------------------------------------------------------------------- #
#  send_album_via_bot                                                          #
# --------------------------------------------------------------------------- #
def test_send_album_puts_the_caption_on_the_first_photo(config):
    http = FakeHTTP()

    ok = asyncio.run(relay.send_album_via_bot(as_client(http), "report", [b"png1", b"png2"]))

    assert ok is True
    assert http.posted == [("report", 2, 2)]


def test_send_album_reports_transport_failure(config, caplog):
    http = FakeHTTP(post_error=relay.httpx.ConnectError("no route"))

    with caplog.at_level("ERROR", logger="relay"):
        ok = asyncio.run(relay.send_album_via_bot(as_client(http), "report", [b"png"]))

    assert ok is False
    assert "sendMediaGroup failed" in caplog.text


def test_send_album_reports_a_refusal(config, caplog):
    http = FakeHTTP(post_response=FakeResponse(payload={"ok": False, "description": "nope"}))

    with caplog.at_level("ERROR", logger="relay"):
        ok = asyncio.run(relay.send_album_via_bot(as_client(http), "report", [b"png"]))

    assert ok is False
    assert "sendMediaGroup refused" in caplog.text


# --------------------------------------------------------------------------- #
#  notify                                                                      #
# --------------------------------------------------------------------------- #
def test_notify_posts_when_enabled(config):
    http = FakeHTTP()

    asyncio.run(relay.notify(as_client(http), "🟢 up"))

    assert http.posted == ["🟢 up"]


def test_notify_silent_when_disabled(config, monkeypatch):
    monkeypatch.setattr(relay, "NOTIFY_LIFECYCLE", False)
    http = FakeHTTP()

    asyncio.run(relay.notify(as_client(http), "🟢 up"))

    assert http.posted == []


def test_notify_swallows_failures(config, monkeypatch):
    async def explode(http, text):
        raise RuntimeError("telegram is down")

    monkeypatch.setattr(relay, "send_via_bot", explode)

    asyncio.run(relay.notify(as_client(FakeHTTP()), "🔴 down"))  # must not raise


# --------------------------------------------------------------------------- #
#  _client                                                                     #
# --------------------------------------------------------------------------- #
def test_client_creates_the_session_directory(monkeypatch, tmp_path):
    """sqlite3 will not make the directory, and session/ is not in the repo."""
    store = tmp_path / "session" / "lexx_relay"
    monkeypatch.setattr(relay, "SESSION", str(store))
    monkeypatch.setattr(relay, "TelegramClient", FakeClient)

    relay._client(1, "hash")

    assert store.parent.is_dir()


def test_client_accepts_a_session_beside_the_script(monkeypatch, tmp_path):
    """A bare name has "." for a parent, which already exists."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(relay, "SESSION", "lexx_relay")
    monkeypatch.setattr(relay, "TelegramClient", FakeClient)

    assert isinstance(relay._client(1, "hash"), FakeClient)


# --------------------------------------------------------------------------- #
#  check                                                                       #
# --------------------------------------------------------------------------- #
@pytest.fixture
def patched(config, monkeypatch):
    """Wire the fakes in and hand back the HTTP double the code will use."""
    http = FakeHTTP()
    monkeypatch.setattr(relay, "TelegramClient", FakeClient)
    monkeypatch.setattr(relay.httpx, "AsyncClient", lambda *a, **kw: http)
    return http


def test_check_passes(patched):
    asyncio.run(relay.check())

    assert patched.posted
    assert patched.posted[0].endswith("(relay test)")
    assert FakeClient.instances[0].disconnected is True


def test_check_exits_when_source_unresolvable(patched, monkeypatch):
    def make_client(session, api_id, api_hash):
        client = FakeClient(session, api_id, api_hash)
        client.entity_error = ValueError("no such user")
        return client

    monkeypatch.setattr(relay, "TelegramClient", make_client)

    checking = relay.check()  # built here so only asyncio.run can raise below

    with pytest.raises(SystemExit) as exit_info:
        asyncio.run(checking)

    assert exit_info.value.code == 1
    assert FakeClient.instances[0].disconnected is True


def test_check_exits_when_bot_token_rejected(config, monkeypatch):
    http = FakeHTTP(get_response=FakeResponse(payload={"ok": False}))
    monkeypatch.setattr(relay, "TelegramClient", FakeClient)
    monkeypatch.setattr(relay.httpx, "AsyncClient", lambda *a, **kw: http)

    checking = relay.check()  # built here so only asyncio.run can raise below

    with pytest.raises(SystemExit) as exit_info:
        asyncio.run(checking)

    assert exit_info.value.code == 1


def test_check_exits_when_test_line_not_delivered(config, monkeypatch):
    http = FakeHTTP(post_response=FakeResponse(status_code=403, text="blocked"))
    monkeypatch.setattr(relay, "TelegramClient", FakeClient)
    monkeypatch.setattr(relay.httpx, "AsyncClient", lambda *a, **kw: http)

    checking = relay.check()  # built here so only asyncio.run can raise below

    with pytest.raises(SystemExit) as exit_info:
        asyncio.run(checking)

    assert exit_info.value.code == 1


# --------------------------------------------------------------------------- #
#  run                                                                         #
# --------------------------------------------------------------------------- #
def _run_relay(monkeypatch, on_run, http=None):
    """Drive run() with a fake client, returning (http double, signal handlers)."""
    http = http or FakeHTTP()
    handlers: dict = {}

    def make_client(session, api_id, api_hash):
        client = FakeClient(session, api_id, api_hash)
        client.on_run = on_run
        return client

    real_get_loop = asyncio.get_running_loop

    def recording_loop():
        return RecordingLoop(real_get_loop(), handlers)

    monkeypatch.setattr(relay, "TelegramClient", make_client)
    monkeypatch.setattr(relay.httpx, "AsyncClient", lambda *a, **kw: http)
    monkeypatch.setattr(relay.asyncio, "get_running_loop", recording_loop)

    asyncio.run(relay.run())
    return http, handlers


def test_run_reports_up_and_down(config, monkeypatch):
    async def disconnect_immediately(client):
        return None

    http, handlers = _run_relay(monkeypatch, disconnect_immediately)

    assert signal.SIGTERM in handlers
    assert signal.SIGINT in handlers
    assert str(http.posted[0]).startswith("🟢")
    # Nothing signalled the relay, so the reason is the dropped connection.
    assert "connection lost" in str(http.posted[-1])
    assert FakeClient.instances[0].disconnected is True


def test_run_up_notice_carries_the_journal_and_webhook_links(config, monkeypatch):
    monkeypatch.setattr(relay.tv_alerts, "JOURNAL_URL", "https://example.test/sheet")
    monkeypatch.setattr(relay.tv_alerts, "TV_PUBLIC_URL", "https://tv.example.test")
    monkeypatch.setattr(relay.tv_alerts, "TV_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setattr(relay.tv_alerts, "TV_PORT", 0)  # ephemeral: no clash

    async def disconnect_immediately(client):
        return None

    http, _ = _run_relay(monkeypatch, disconnect_immediately)

    up = str(http.posted[0])
    assert "📒 https://example.test/sheet" in up
    assert "📡 https://tv.example.test/tv/s3cret" in up


def test_run_announces_speaking_hours_with_a_speaker(config, monkeypatch):
    class FakeAudio:
        def shutdown(self):
            pass

    monkeypatch.setattr(relay.speaker, "enabled", lambda: True)
    monkeypatch.setattr(relay.speaker, "serve_forever", FakeAudio)
    monkeypatch.setattr(relay.speaker, "hours_text", lambda: "23:00–08:00")

    async def no_lifecycle(text):
        return False

    monkeypatch.setattr(relay.speaker, "lifecycle", no_lifecycle)

    async def disconnect_immediately(client):
        return None

    http, _ = _run_relay(monkeypatch, disconnect_immediately)

    assert "🔊 23:00–08:00" in str(http.posted[0])


def test_run_speaks_its_own_lifecycle(config, monkeypatch):
    class FakeAudio:
        def shutdown(self):
            pass

    said: list[str] = []

    async def fake_lifecycle(text):
        said.append(text)
        return True

    monkeypatch.setattr(relay.speaker, "enabled", lambda: True)
    monkeypatch.setattr(relay.speaker, "serve_forever", FakeAudio)
    monkeypatch.setattr(relay.speaker, "lifecycle", fake_lifecycle)

    async def disconnect_immediately(client):
        return None

    _run_relay(monkeypatch, disconnect_immediately)

    assert said == ["Relay up", "Relay down"]


def test_run_starts_the_bybit_watcher_when_keyed(config, monkeypatch):
    started = []

    async def fake_poll(http, send, speak, send_photo):
        started.append((send, speak, send_photo))
        await asyncio.sleep(3600)  # runs until the relay cancels it

    money_started = []

    async def fake_money_poll(http, send):
        money_started.append(send)
        await asyncio.sleep(3600)

    monkeypatch.setattr(relay.bybit_watch, "enabled", lambda: True)
    monkeypatch.setattr(relay.bybit_watch, "poll", fake_poll)
    monkeypatch.setattr(relay.bybit_watch, "money_poll", fake_money_poll)

    async def disconnect_immediately(client):
        return None

    _run_relay(monkeypatch, disconnect_immediately)

    assert len(started) == 1
    assert started[0][1] is relay.speaker.trade
    assert len(money_started) == 1


def test_run_starts_the_sizer_when_opted_in(config, monkeypatch):
    started = []

    async def fake_poll(http, send):
        started.append(send)
        await asyncio.sleep(3600)  # runs until the relay cancels it

    monkeypatch.setattr(relay.sizer, "enabled", lambda: True)
    monkeypatch.setattr(relay.sizer, "poll", fake_poll)

    async def disconnect_immediately(client):
        return None

    _run_relay(monkeypatch, disconnect_immediately)

    assert len(started) == 1


def test_run_starts_the_ip_watcher_when_enabled(config, monkeypatch):
    started = []

    async def fake_poll(http, send, speak):
        started.append(speak)
        await asyncio.sleep(3600)  # runs until the relay cancels it

    monkeypatch.setattr(relay.ip_watch, "enabled", lambda: True)
    monkeypatch.setattr(relay.ip_watch, "poll", fake_poll)

    async def disconnect_immediately(client):
        return None

    _run_relay(monkeypatch, disconnect_immediately)

    assert started == [relay.speaker.trade]


def test_run_starts_the_weekly_journal_when_sheets_are_wired(config, monkeypatch):
    started = []

    async def fake_weekly(http):
        started.append(http)
        await asyncio.sleep(3600)  # runs until the relay cancels it

    monkeypatch.setattr(relay.sheets, "enabled", lambda: True)
    monkeypatch.setattr(relay.sheets, "weekly", fake_weekly)

    async def disconnect_immediately(client):
        return None

    _run_relay(monkeypatch, disconnect_immediately)

    assert len(started) == 1


def test_run_starts_the_tv_webhook_when_a_secret_is_set(config, monkeypatch):
    stopped = []

    class FakeWebhook:
        def shutdown(self):
            stopped.append(True)

    started = []

    def fake_serve(loop, queue):
        started.append(queue)
        return FakeWebhook()

    async def fake_pump(queue, send, speak, price_of=None):
        await asyncio.sleep(3600)  # runs until the relay cancels it

    monkeypatch.setattr(relay.tv_alerts, "enabled", lambda: True)
    monkeypatch.setattr(relay.tv_alerts, "serve", fake_serve)
    monkeypatch.setattr(relay.tv_alerts, "pump", fake_pump)

    async def disconnect_immediately(client):
        return None

    _run_relay(monkeypatch, disconnect_immediately)

    assert len(started) == 1
    assert stopped == [True]  # the webhook came down with the relay


def test_run_does_not_speak_lifecycle_without_a_speaker(config, monkeypatch):
    said: list[str] = []

    async def fake_lifecycle(text):
        said.append(text)
        return True

    monkeypatch.setattr(relay.speaker, "enabled", lambda: False)
    monkeypatch.setattr(relay.speaker, "lifecycle", fake_lifecycle)

    async def disconnect_immediately(client):
        return None

    _run_relay(monkeypatch, disconnect_immediately)

    assert said == []


def test_run_up_notice_stays_plain_without_a_speaker(config, monkeypatch):
    monkeypatch.setattr(relay.speaker, "enabled", lambda: False)

    async def disconnect_immediately(client):
        return None

    http, _ = _run_relay(monkeypatch, disconnect_immediately)

    assert "🔊" not in str(http.posted[0])


def test_run_shuts_down_on_sigterm(config, monkeypatch):
    async def signal_then_hang(client):
        callback, args = _handlers[signal.SIGTERM]
        callback(*args)
        callback(*args)  # second delivery must not overwrite the reason
        await asyncio.sleep(3600)

    _handlers: dict = {}

    def make_client(session, api_id, api_hash):
        client = FakeClient(session, api_id, api_hash)
        client.on_run = signal_then_hang
        return client

    http = FakeHTTP()
    real_get_loop = asyncio.get_running_loop
    monkeypatch.setattr(relay, "TelegramClient", make_client)
    monkeypatch.setattr(relay.httpx, "AsyncClient", lambda *a, **kw: http)
    monkeypatch.setattr(
        relay.asyncio,
        "get_running_loop",
        lambda: RecordingLoop(real_get_loop(), _handlers),
    )

    asyncio.run(relay.run())

    last = str(http.posted[-1])
    assert "SIGTERM" in last
    assert last.startswith("🔴")


def test_run_relays_alerts_and_skips_noise(config, monkeypatch):
    async def feed_messages(client):
        await client.handler(SimpleNamespace(raw_text="Бот запущен"))
        await client.handler(
            SimpleNamespace(
                raw_text=(
                    "🔔 #OPUSDT OPUSDT, Пересечение 0.10282\n"
                    "-  exchange:  #BybitFutures\n"
                    "-  trend: 📈\n"
                    "-  price: 0.10277"
                )
            )
        )

    http, _ = _run_relay(monkeypatch, feed_messages)

    # startup notice, the one relayed alert, shutdown notice — the noise line
    # never reaches Telegram.
    assert http.posted[1] == "OP 📈 0.10277"
    assert len(http.posted) == 3


class FakeWatchedEvent:
    """A message from a watched user, as the watched-user handler sees it."""

    def __init__(self, raw_text, sender=None, chat=None):
        self.raw_text = raw_text
        self._sender = sender if sender is not None else SimpleNamespace(username="some_trader")
        self._chat = chat if chat is not None else SimpleNamespace(title="Trading Club")

    async def get_sender(self):
        return self._sender

    async def get_chat(self):
        return self._chat


def test_run_relays_watched_user_messages(config, monkeypatch):
    async def feed(client):
        await client.handlers[1](FakeWatchedEvent("сетку ставим на OP"))

    http, _ = _run_relay(monkeypatch, feed)

    assert http.posted[1] == "👤 @some_trader · Trading Club:\nсетку ставим на OP"


def test_watched_handler_skips_messages_without_text(config, monkeypatch):
    async def feed(client):
        await client.handlers[1](FakeWatchedEvent(""))

    http, _ = _run_relay(monkeypatch, feed)

    # Only the lifecycle notices — a sticker or photo has nothing to relay.
    assert len(http.posted) == 2


def test_watched_handler_falls_back_when_names_are_missing(config, monkeypatch):
    async def feed(client):
        await client.handlers[1](
            FakeWatchedEvent(
                "no names here",
                sender=SimpleNamespace(username=None, first_name="Alexey"),
                chat=SimpleNamespace(username=None),
            )
        )
        await client.handlers[1](
            FakeWatchedEvent(
                "nobody at all",
                sender=SimpleNamespace(username=None, first_name=None),
                chat=SimpleNamespace(username="lexx_club"),
            )
        )

    http, _ = _run_relay(monkeypatch, feed)

    assert http.posted[1] == "👤 @Alexey · ?:\nno names here"
    assert http.posted[2] == "👤 @? · lexx_club:\nnobody at all"


def test_watched_handler_truncates_to_telegram_limit(config, monkeypatch):
    async def feed(client):
        await client.handlers[1](FakeWatchedEvent("x" * 5000))

    http, _ = _run_relay(monkeypatch, feed)

    assert len(http.posted[1]) == 4000


def test_run_without_watch_users_registers_only_the_alert_handler(config, monkeypatch):
    monkeypatch.setattr(relay, "WATCH_USERS", [])

    async def nothing(client):
        return None

    _run_relay(monkeypatch, nothing)

    assert len(FakeClient.instances[0].handlers) == 1


def test_run_stops_the_audio_server_it_started(config, monkeypatch):
    """With a speaker configured, the file server must come down with the relay."""
    stopped = []

    class FakeAudio:
        def shutdown(self):
            stopped.append(True)

    spoken: list[str] = []

    async def fake_announce(compact, counter):
        spoken.append(compact)
        return True

    async def no_lifecycle(text):
        return False

    monkeypatch.setattr(relay.speaker, "enabled", lambda: True)
    monkeypatch.setattr(relay.speaker, "serve_forever", FakeAudio)
    monkeypatch.setattr(relay.speaker, "announce", fake_announce)
    monkeypatch.setattr(relay.speaker, "lifecycle", no_lifecycle)

    async def one_alert(client):
        await client.handler(
            SimpleNamespace(
                raw_text=(
                    "🔔 #OPUSDT OPUSDT, Пересечение 0.10282\n"
                    "-  exchange:  #BybitFutures\n"
                    "-  trend: 📈\n"
                    "-  price: 0.10277"
                )
            )
        )

    _run_relay(monkeypatch, one_alert)

    assert stopped == [True]
    assert spoken == ["OP 📈 0.10277"]


# --------------------------------------------------------------------------- #
#  main                                                                        #
# --------------------------------------------------------------------------- #
def test_main_runs_the_relay(monkeypatch):
    called = []

    async def fake_run():
        called.append("run")

    monkeypatch.setattr(relay.sys, "argv", ["relay.py"])
    monkeypatch.setattr(relay, "run", fake_run)

    relay.main()

    assert called == ["run"]


def test_main_check_flag_runs_the_check(monkeypatch):
    called = []

    async def fake_check():
        called.append("check")

    monkeypatch.setattr(relay.sys, "argv", ["relay.py", "--check"])
    monkeypatch.setattr(relay, "check", fake_check)

    relay.main()

    assert called == ["check"]


def test_run_up_notice_carries_the_external_ip(config, monkeypatch):
    async def fake_current(http):
        return "203.0.113.7"

    monkeypatch.setattr(relay.ip_watch, "current", fake_current)

    async def disconnect_immediately(client):
        return None

    http, _ = _run_relay(monkeypatch, disconnect_immediately)

    assert "🌐 203.0.113.7" in str(http.posted[0])


def test_run_up_notice_skips_an_unanswerable_ip(config, monkeypatch):
    async def broken_current(http):
        raise OSError("no network")

    monkeypatch.setattr(relay.ip_watch, "current", broken_current)

    async def disconnect_immediately(client):
        return None

    http, _ = _run_relay(monkeypatch, disconnect_immediately)

    assert "🌐" not in str(http.posted[0])
