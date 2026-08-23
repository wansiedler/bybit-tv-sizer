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
from pathlib import Path
from typing import Any

import pytest

import speaker


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Both endpoints configured, audio written into a temp directory."""
    monkeypatch.setattr(speaker, "SPEAK_ALERTS", True)
    monkeypatch.setattr(speaker, "CAST_HOST", "192.0.2.10")
    monkeypatch.setattr(speaker, "TTS_HOST", "192.0.2.20")
    monkeypatch.setattr(speaker, "TTS_PORT", 8422)
    monkeypatch.setattr(speaker, "TTS_DIR", tmp_path / "tts")
    return tmp_path / "tts"


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


@pytest.mark.parametrize(
    ("compact", "expected"),
    [
        ("LTC 📉 84.31", "Litecoin down, 84.31"),
        ("OP 📈 0.10277", "Optimism up, 0.10277"),
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
        "disconnected": 0,
        "fail": None,
        "app_id": None,
        "quit": 0,
    }

    class FakeController:
        def play_media(self, url, mime):
            calls["played"].append((url, mime))

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
#  announce                                                                    #
# --------------------------------------------------------------------------- #
def test_announce_speaks(wired, stub_gtts, stub_cast):
    assert asyncio.run(speaker.announce("OP 📈 0.10277", 1)) is True
    assert stub_gtts == [("Optimism up, 0.10277", "en")]
    assert stub_cast["played"][0][0] == "http://192.0.2.20:8422/alert-1.mp3"


def test_announce_recycles_filenames(wired, stub_gtts, stub_cast):
    # 20 slots, so a long-running relay does not fill the disk with mp3s.
    asyncio.run(speaker.announce("OP 📈 0.10277", 21))

    assert stub_cast["played"][0][0].endswith("/alert-1.mp3")


def test_announce_silent_when_disabled(wired, monkeypatch, stub_gtts, stub_cast):
    monkeypatch.setattr(speaker, "SPEAK_ALERTS", False)

    assert asyncio.run(speaker.announce("OP 📈 0.10277", 1)) is False
    assert stub_gtts == []


def test_announce_skips_unspeakable_lines(wired, stub_gtts, stub_cast):
    assert asyncio.run(speaker.announce("relay started", 1)) is False
    assert stub_gtts == []


def test_announce_survives_a_dead_speaker(wired, stub_gtts, stub_cast, caplog):
    stub_cast["fail"] = OSError("no route to host")

    with caplog.at_level("ERROR", logger="relay.speaker"):
        assert asyncio.run(speaker.announce("OP 📈 0.10277", 1)) is False

    assert "could not speak" in caplog.text
