"""Tests for the Nest speaker path.

No Cast device and no Google TTS are contacted: `pychromecast` and `gtts` are
installed as stub modules for the duration of a test, and the HTTP server is
started on a real ephemeral port and fetched over loopback.
"""

import asyncio
import stat
import sys
import types
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

import speaker


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Both endpoints configured, audio written into a temp directory."""
    monkeypatch.setattr(speaker, "SPEAK_ALERTS", True)
    # The developer's own .env may carry SPEAK_HOURS; tests must not go mute
    # depending on the wall clock they run at.
    monkeypatch.setattr(speaker, "SPEAK_WINDOW", None)
    monkeypatch.setattr(speaker, "CAST_HOST", "192.0.2.10")
    monkeypatch.setattr(speaker, "TTS_HOST", "192.0.2.20")
    monkeypatch.setattr(speaker, "TTS_PORT", 8422)
    monkeypatch.setattr(speaker, "TTS_DIR", tmp_path / "tts")
    return tmp_path / "tts"


# --------------------------------------------------------------------------- #
#  proxy exemption                                                             #
# --------------------------------------------------------------------------- #
def test_exempt_from_proxy_appends_the_lan_hosts(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "api.telegram.org")
    monkeypatch.delenv("no_proxy", raising=False)

    value = speaker.exempt_from_proxy("192.0.2.10", "192.0.2.20")

    assert value == "api.telegram.org,192.0.2.10,192.0.2.20"
    assert speaker.os.environ["NO_PROXY"] == value
    assert speaker.os.environ["no_proxy"] == value  # urllib reads either spelling


def test_exempt_from_proxy_skips_blank_and_listed_hosts(monkeypatch):
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.setenv("no_proxy", " 192.0.2.10 ,")

    assert speaker.exempt_from_proxy("", "192.0.2.10") == "192.0.2.10"


# --------------------------------------------------------------------------- #
#  enabled / spoken                                                            #
# --------------------------------------------------------------------------- #
def test_enabled_when_fully_configured(wired):
    assert speaker.enabled() is True


@pytest.mark.parametrize(
    ("attr", "value"),
    [("SPEAK_ALERTS", False), ("CAST_HOST", ""), ("TTS_HOST", "")],
)
def test_enabled_false_when_anything_missing(wired, monkeypatch, attr, value):
    monkeypatch.setattr(speaker, attr, value)

    assert speaker.enabled() is False


# --------------------------------------------------------------------------- #
#  speaking hours                                                              #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("23-8", (23, 8)),
        ("9-17", (9, 17)),
        ("0-23", (0, 23)),
        ("", None),  # unset: around the clock
        ("   ", None),
    ],
)
def test_parse_window_reads_start_and_end(raw, expected):
    assert speaker.parse_window(raw) == expected


@pytest.mark.parametrize("raw", ["8", "8-", "-8", "24-8", "23-99", "8-8", "night", "8:30-9"])
def test_parse_window_refuses_nonsense_without_muting(raw, caplog):
    # A typo in SPEAK_HOURS must not silence every alert.
    with caplog.at_level("WARNING", logger="relay.speaker"):
        assert speaker.parse_window(raw) is None

    assert "SPEAK_HOURS" in caplog.text


@pytest.mark.parametrize(
    ("window", "expected"),
    [
        ((23, 8), "23:00–08:00"),
        ((9, 17), "09:00–17:00"),
        (None, "round the clock"),
    ],
)
def test_hours_text_reads_the_window_out(monkeypatch, window, expected):
    monkeypatch.setattr(speaker, "SPEAK_WINDOW", window)

    assert speaker.hours_text() == expected


@pytest.mark.parametrize(
    ("window", "hour", "expected"),
    [
        ((23, 8), 23, True),  # wraps midnight
        ((23, 8), 2, True),
        ((23, 8), 8, False),  # end is exclusive
        ((23, 8), 12, False),
        ((9, 17), 9, True),  # same-day window
        ((9, 17), 17, False),
        ((9, 17), 3, False),
        (None, 12, True),  # no window: always
    ],
)
def test_within_window(monkeypatch, window, hour, expected):
    monkeypatch.setattr(speaker, "SPEAK_WINDOW", window)

    assert speaker.within_window(datetime(2026, 8, 26, hour, 30)) is expected


@pytest.mark.parametrize(
    ("compact", "expected"),
    [
        ("LTC 📉 84.31", "Litecoin down, 84.3"),
        ("OP 📈 0.10277", "Optimism up, 0.103"),
        # Multiplier tickers speak the plain coin name.
        ("1000PEPE 📈 0.0102", "Pepe up, 0.0102"),
        # A ticker the table does not know is spoken as-is.
        ("XYZZY 📉 0.00409", "XYZZY down, 0.00409"),
    ],
)
def test_spoken_reads_the_trend_out(compact, expected):
    assert speaker.spoken(compact) == expected


@pytest.mark.parametrize(
    "compact",
    ["", "OP 📈", "OP 📈 0.1 extra", "OP 🔔 0.10277"],
)
def test_spoken_refuses_anything_else(compact):
    assert speaker.spoken(compact) is None


@pytest.mark.parametrize(
    ("price", "expected"),
    [
        ("2383.66", "2380"),  # big prices lose noise digits...
        ("12345", "12300"),
        ("84.31", "84.3"),
        ("0.10277", "0.103"),  # ...cheap coins keep their magnitude
        ("0.0040938", "0.00409"),
        ("7", "7"),  # already short: unchanged, no trailing zeros
        ("0", "0"),  # log10 has no answer here; special-cased
    ],
)
def test_rounded_keeps_three_significant_digits(price, expected):
    assert speaker.rounded(price) == expected


def test_ensure_dir_tightens_a_directory_that_already_exists(wired):
    """mkdir's mode is ignored for an existing directory; chmod is not."""
    wired.mkdir(parents=True)
    wired.chmod(0o755)

    speaker.ensure_dir()

    assert stat.S_IMODE(wired.stat().st_mode) == 0o700


def test_ensure_dir_creates_it_private(wired):
    speaker.ensure_dir()

    assert stat.S_IMODE(wired.stat().st_mode) == 0o700


# --------------------------------------------------------------------------- #
#  the file server the speaker fetches from                                    #
# --------------------------------------------------------------------------- #
def test_serve_forever_serves_the_audio_directory(wired, monkeypatch):
    monkeypatch.setattr(speaker, "TTS_PORT", 0)  # ephemeral: no port clash in CI
    httpd = speaker.serve_forever()
    try:
        (wired / "alert-0.mp3").write_bytes(b"ID3-not-really")
        port = httpd.server_address[1]

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/alert-0.mp3") as response:
            assert response.read() == b"ID3-not-really"
    finally:
        httpd.shutdown()


def test_request_logging_goes_to_the_logger(wired, caplog):
    # The stock handler writes a line to stderr per fetch; ours must not.
    handler = speaker._QuietHandler.__new__(speaker._QuietHandler)

    with caplog.at_level("DEBUG", logger="relay.speaker"):
        speaker._QuietHandler.log_message(handler, "%s served", "alert-0.mp3")

    assert "alert-0.mp3 served" in caplog.text


# --------------------------------------------------------------------------- #
#  TTS and casting, both stubbed at the import site                            #
# --------------------------------------------------------------------------- #
@pytest.fixture
def stub_gtts(monkeypatch):
    """Install a fake `gtts` module and record what it was asked to say."""
    said = []

    class FakeTTS:
        def __init__(self, text, lang):
            said.append((text, lang))

        def save(self, path):
            Path(path).write_bytes(b"mp3")

    module = types.ModuleType("gtts")
    module.gTTS = FakeTTS  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "gtts", module)
    return said


@pytest.fixture
def stub_cast(monkeypatch):
    """Install a fake `pychromecast` and record the media it was handed."""
    calls: dict[str, Any] = {
        "played": [],
        "resumed": [],
        "disconnected": 0,
        "fail": None,
        "status_fail": None,
        "resume_fail": None,
        "app_id": None,
        "quit": 0,
        # What the fake media session reports. `states` is consumed one
        # player_state read at a time, holding on the last entry, so a test
        # can walk the player through PLAYING -> IDLE without real waiting.
        "media": {"content_id": None, "content_type": None, "current_time": 0.0},
        "states": ["UNKNOWN"],
    }

    class FakeMediaStatus:
        @property
        def content_id(self):
            return calls["media"]["content_id"]

        @property
        def content_type(self):
            return calls["media"]["content_type"]

        @property
        def current_time(self):
            return calls["media"]["current_time"]

        @property
        def player_state(self):
            states = calls["states"]
            return states.pop(0) if len(states) > 1 else states[0]

    class FakeController:
        status = FakeMediaStatus()

        def play_media(self, url, mime, current_time=None, stream_type="LIVE"):
            if current_time is None:
                calls["played"].append((url, mime))
            elif calls["resume_fail"] is not None:
                raise calls["resume_fail"]
            else:
                calls["resumed"].append((url, mime, current_time, stream_type))

        def update_status(self, callback_function=None):
            if calls["status_fail"] is not None:
                raise calls["status_fail"]
            if callback_function is not None:
                callback_function(True, None)

        def block_until_active(self, timeout=None):
            pass

    class FakeStatus:
        display_name = "YouTube Music"

    class FakeChromecast:
        def __init__(self, host_tuple):
            calls["host"], calls["port"], calls["uuid"] = host_tuple[:3]
            self.media_controller = FakeController()
            self.status = FakeStatus()

        @property
        def app_id(self):
            return calls["app_id"]

        def quit_app(self):
            calls["quit"] += 1
            if not calls.get("sticky"):
                calls["app_id"] = None

        def wait(self, timeout=None):
            if calls["fail"] is not None:
                raise calls["fail"]

        def disconnect(self):
            calls["disconnected"] += 1

    module = types.ModuleType("pychromecast")
    module.get_chromecast_from_host = (  # type: ignore[attr-defined]
        lambda host_tuple, timeout=None: FakeChromecast(host_tuple)
    )
    monkeypatch.setitem(sys.modules, "pychromecast", module)
    return calls


def test_write_speech_renders_english(wired, stub_gtts):
    path = speaker.write_speech("OP up, 0.10277", "alert-1.mp3")

    assert path.read_bytes() == b"mp3"
    assert stub_gtts == [("OP up, 0.10277", "en")]


def test_cast_url_hands_the_speaker_a_url(wired, stub_cast):
    speaker.cast_url("http://192.0.2.20:8422/alert-1.mp3")

    assert stub_cast["host"] == "192.0.2.10"
    assert stub_cast["port"] == 8009
    assert str(stub_cast["uuid"])  # a device identity was supplied
    assert stub_cast["played"] == [("http://192.0.2.20:8422/alert-1.mp3", "audio/mpeg")]
    assert stub_cast["disconnected"] == 1


def test_cast_url_always_disconnects(wired, stub_cast):
    stub_cast["fail"] = TimeoutError("speaker asleep")

    with pytest.raises(TimeoutError):
        speaker.cast_url("http://192.0.2.20:8422/alert-1.mp3")

    assert stub_cast["disconnected"] == 1


def test_cast_url_interrupts_another_app(wired, stub_cast, caplog):
    # A speaker running YouTube Music hands our URL to that app, which drops it.
    stub_cast["app_id"] = "2DB7CC49"

    with caplog.at_level("INFO", logger="relay.speaker"):
        speaker.cast_url("http://192.0.2.20:8422/alert-1.mp3")

    assert stub_cast["quit"] == 1
    assert stub_cast["played"]
    assert "interrupting YouTube Music" in caplog.text


def test_cast_url_leaves_the_media_receiver_alone(wired, stub_cast):
    stub_cast["app_id"] = speaker.MEDIA_RECEIVER

    speaker.cast_url("http://192.0.2.20:8422/alert-1.mp3")

    assert stub_cast["quit"] == 0
    assert stub_cast["played"]


def test_cast_url_can_stay_quiet_instead(wired, monkeypatch, stub_cast, caplog):
    monkeypatch.setattr(speaker, "SPEAK_INTERRUPT", False)
    stub_cast["app_id"] = "2DB7CC49"

    with caplog.at_level("INFO", logger="relay.speaker"):
        speaker.cast_url("http://192.0.2.20:8422/alert-1.mp3")

    assert stub_cast["quit"] == 0
    assert stub_cast["played"] == []
    assert "staying quiet" in caplog.text
    assert stub_cast["disconnected"] == 1


def test_cast_url_gives_up_waiting_for_a_stuck_app(wired, monkeypatch, stub_cast):
    """quit_app accepted but the app lingers: play anyway rather than hang."""
    stub_cast["app_id"] = "2DB7CC49"
    stub_cast["sticky"] = True  # quit_app leaves app_id alone
    slept: list[float] = []
    clock = iter([0.0, 1.0, 2.0, 100.0])
    monkeypatch.setattr(speaker.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(speaker.time, "sleep", slept.append)

    speaker.cast_url("http://192.0.2.20:8422/alert-1.mp3")

    assert slept  # it waited
    assert stub_cast["played"]  # and cast regardless


# --------------------------------------------------------------------------- #
#  resuming what the alert interrupted                                         #
# --------------------------------------------------------------------------- #
MELODY = "https://storage.googleapis.com/relaxation-sounds/country_night_3600.mp3"


def playing(stub_cast, app_id, content_id=MELODY):
    """Put the fake speaker mid-melody: PLAYING now, IDLE once the alert ends."""
    stub_cast["app_id"] = app_id
    stub_cast["media"] = {
        "content_id": content_id,
        "content_type": "audio/mp3",
        "current_time": 14.2,
    }
    # One read in the snapshot, one in the wait-for-PLAYING, then the alert
    # clip has finished.
    stub_cast["states"] = ["PLAYING", "PLAYING", "IDLE"]


def test_cast_url_resumes_the_interrupted_media(wired, stub_cast, caplog):
    playing(stub_cast, app_id="9731D581")

    with caplog.at_level("INFO", logger="relay.speaker"):
        speaker.cast_url("http://192.0.2.20:8422/alert-1.mp3")

    assert stub_cast["quit"] == 1
    assert stub_cast["played"] == [("http://192.0.2.20:8422/alert-1.mp3", "audio/mpeg")]
    assert stub_cast["resumed"] == [(MELODY, "audio/mp3", 14.2, "BUFFERED")]
    assert "resuming interrupted media at 14s" in caplog.text


def test_cast_url_never_resumes_its_own_tts(wired, stub_cast):
    playing(
        stub_cast,
        app_id=speaker.MEDIA_RECEIVER,
        content_id="http://192.0.2.20:8422/alert-3.mp3",
    )

    speaker.cast_url("http://192.0.2.20:8422/alert-4.mp3")

    assert stub_cast["resumed"] == []


def test_cast_url_resume_can_be_disabled(wired, monkeypatch, stub_cast):
    monkeypatch.setattr(speaker, "SPEAK_RESUME", False)
    playing(stub_cast, app_id="9731D581")

    speaker.cast_url("http://192.0.2.20:8422/alert-1.mp3")

    assert stub_cast["played"]
    assert stub_cast["resumed"] == []


def test_cast_url_stays_quiet_when_the_receiver_plays_foreign_media(
    wired, monkeypatch, stub_cast, caplog
):
    # After one resume the melody lives in our own media receiver; with
    # interrupting off, that still counts as busy.
    monkeypatch.setattr(speaker, "SPEAK_INTERRUPT", False)
    playing(stub_cast, app_id=speaker.MEDIA_RECEIVER)

    with caplog.at_level("INFO", logger="relay.speaker"):
        speaker.cast_url("http://192.0.2.20:8422/alert-1.mp3")

    assert stub_cast["played"] == []
    assert "staying quiet" in caplog.text


def test_cast_url_snapshots_nothing_from_an_app_without_media(wired, stub_cast, caplog):
    # Some apps refuse the media-status request outright; the alert must
    # still be spoken, with nothing to resume afterwards.
    stub_cast["app_id"] = "2DB7CC49"
    stub_cast["status_fail"] = RuntimeError("namespace not available")

    with caplog.at_level("DEBUG", logger="relay.speaker"):
        speaker.cast_url("http://192.0.2.20:8422/alert-1.mp3")

    assert stub_cast["played"]
    assert stub_cast["resumed"] == []
    assert "no media status" in caplog.text


def test_resume_waits_for_the_alert_clip_to_start(wired, monkeypatch, stub_cast):
    # The receiver takes a moment before the clip reaches PLAYING.
    playing(stub_cast, app_id="9731D581")
    stub_cast["states"] = ["PLAYING", "BUFFERING", "PLAYING", "IDLE"]
    slept: list[float] = []
    monkeypatch.setattr(speaker.time, "sleep", slept.append)

    speaker.cast_url("http://192.0.2.20:8422/alert-1.mp3")

    assert slept  # it polled instead of resuming over the clip
    assert stub_cast["resumed"] == [(MELODY, "audio/mp3", 14.2, "BUFFERED")]


def test_cast_url_survives_a_failed_resume(wired, stub_cast, caplog):
    # The alert was spoken; a resume the receiver rejects is logged, not raised.
    playing(stub_cast, app_id="9731D581")
    stub_cast["resume_fail"] = RuntimeError("receiver rejected the media")

    with caplog.at_level("ERROR", logger="relay.speaker"):
        speaker.cast_url("http://192.0.2.20:8422/alert-1.mp3")

    assert stub_cast["played"]
    assert stub_cast["resumed"] == []
    assert "could not resume" in caplog.text
    assert stub_cast["disconnected"] == 1


def test_cast_url_does_not_resume_app_private_streams(wired, stub_cast):
    # YouTube Music reports no content_id a plain receiver could replay.
    playing(stub_cast, app_id="2DB7CC49", content_id=None)

    speaker.cast_url("http://192.0.2.20:8422/alert-1.mp3")

    assert stub_cast["quit"] == 1
    assert stub_cast["played"]
    assert stub_cast["resumed"] == []


# --------------------------------------------------------------------------- #
#  announce                                                                    #
# --------------------------------------------------------------------------- #
def test_announce_speaks(wired, stub_gtts, stub_cast):
    assert asyncio.run(speaker.announce("OP 📈 0.10277", 1)) is True
    assert stub_gtts == [("Optimism up, 0.103", "en")]
    assert stub_cast["played"][0][0] == "http://192.0.2.20:8422/alert-1.mp3"


def test_announce_recycles_filenames(wired, stub_gtts, stub_cast):
    # 20 slots, so a long-running relay does not fill the disk with mp3s.
    asyncio.run(speaker.announce("OP 📈 0.10277", 21))

    assert stub_cast["played"][0][0].endswith("/alert-1.mp3")


def test_announce_silent_when_disabled(wired, monkeypatch, stub_gtts, stub_cast):
    monkeypatch.setattr(speaker, "SPEAK_ALERTS", False)

    assert asyncio.run(speaker.announce("OP 📈 0.10277", 1)) is False
    assert stub_gtts == []


def test_announce_silent_outside_speaking_hours(wired, monkeypatch, stub_gtts, stub_cast):
    monkeypatch.setattr(speaker, "SPEAK_WINDOW", (23, 8))
    monkeypatch.setattr(speaker, "within_window", lambda now=None: False)

    assert asyncio.run(speaker.announce("OP 📈 0.10277", 1)) is False
    assert stub_gtts == []


def test_announce_speaks_inside_speaking_hours(wired, monkeypatch, stub_gtts, stub_cast):
    monkeypatch.setattr(speaker, "SPEAK_WINDOW", (23, 8))
    monkeypatch.setattr(speaker, "within_window", lambda now=None: True)

    assert asyncio.run(speaker.announce("OP 📈 0.10277", 1)) is True


def test_announce_skips_unspeakable_lines(wired, stub_gtts, stub_cast):
    assert asyncio.run(speaker.announce("relay started", 1)) is False
    assert stub_gtts == []


def test_trade_speaks_inside_the_window(wired, stub_gtts, stub_cast):
    assert asyncio.run(speaker.trade("Fartcoin long opened")) is True
    assert stub_gtts == [("Fartcoin long opened", "en")]
    assert stub_cast["played"][0][0] == "http://192.0.2.20:8422/trade.mp3"


def test_trade_silent_without_a_speaker(wired, monkeypatch, stub_gtts, stub_cast):
    monkeypatch.setattr(speaker, "CAST_HOST", "")

    assert asyncio.run(speaker.trade("Fartcoin long opened")) is False
    assert stub_gtts == []


def test_trade_respects_the_window(wired, monkeypatch, stub_gtts, stub_cast):
    monkeypatch.setattr(speaker, "SPEAK_WINDOW", (23, 8))
    monkeypatch.setattr(speaker, "within_window", lambda now=None: False)

    assert asyncio.run(speaker.trade("Fartcoin long opened")) is False
    assert stub_gtts == []


# --------------------------------------------------------------------------- #
#  lifecycle notices                                                           #
# --------------------------------------------------------------------------- #
def test_lifecycle_speaks_at_any_hour(wired, monkeypatch, stub_gtts, stub_cast):
    # 12:00 is outside a 23-8 window; a state change must be heard anyway.
    monkeypatch.setattr(speaker, "SPEAK_WINDOW", (23, 8))
    monkeypatch.setattr(speaker, "within_window", lambda now=None: False)

    assert asyncio.run(speaker.lifecycle("Relay up")) is True
    assert stub_gtts == [("Relay up", "en")]
    assert stub_cast["played"][0][0] == "http://192.0.2.20:8422/notice.mp3"


def test_lifecycle_can_be_muted(wired, monkeypatch, stub_gtts, stub_cast):
    monkeypatch.setattr(speaker, "SPEAK_LIFECYCLE", False)

    assert asyncio.run(speaker.lifecycle("Relay up")) is False
    assert stub_gtts == []


def test_lifecycle_silent_without_a_speaker(wired, monkeypatch, stub_gtts, stub_cast):
    monkeypatch.setattr(speaker, "CAST_HOST", "")

    assert asyncio.run(speaker.lifecycle("Relay up")) is False
    assert stub_gtts == []


def test_say_survives_a_dead_speaker(wired, stub_gtts, stub_cast, caplog):
    stub_cast["fail"] = OSError("no route to host")

    with caplog.at_level("ERROR", logger="relay.speaker"):
        assert asyncio.run(speaker.say("Relay up", "notice.mp3")) is False

    assert "could not speak" in caplog.text


def test_announce_survives_a_dead_speaker(wired, stub_gtts, stub_cast, caplog):
    stub_cast["fail"] = OSError("no route to host")

    with caplog.at_level("ERROR", logger="relay.speaker"):
        assert asyncio.run(speaker.announce("OP 📈 0.10277", 1)) is False

    assert "could not speak" in caplog.text
