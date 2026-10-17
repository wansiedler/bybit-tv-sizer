"""Tests for the relay's side of the TradingView capture. HTTP is faked.

What matters here is that a shot which does not happen never costs anything
but the picture, and always says which way it failed.
"""

import asyncio
from types import SimpleNamespace

import pytest

import tv_shot


@pytest.fixture(autouse=True)
def _configured(monkeypatch, tmp_path):
    monkeypatch.setattr(tv_shot, "TV_SHOT_SECRET", "s3cret")
    monkeypatch.setattr(tv_shot, "TV_SHOT_URL", "http://host.docker.internal:8424")
    monkeypatch.setattr(tv_shot, "TV_SHOT_DIR", str(tmp_path / "tv-entry"))
    monkeypatch.setattr(tv_shot, "TV_SHOT_SUFFIX", ".P")


class FakeResponse:
    def __init__(self, status_code=200, content=b"PNG", text=""):
        self.status_code, self.content, self.text = status_code, content, text


class FakeHTTP:
    def __init__(self, response=None, error=None):
        self.response = response or FakeResponse()
        self.error = error
        self.got: list[tuple[str, dict]] = []

    async def get(self, url, params=None, timeout=None):
        if self.error is not None:
            raise self.error
        self.got.append((url, params))
        return self.response


def test_enabled_needs_a_secret(monkeypatch):
    assert tv_shot.enabled() is True
    monkeypatch.setattr(tv_shot, "TV_SHOT_SECRET", "")
    assert tv_shot.enabled() is False


def test_tv_symbol_is_the_bybit_one_as_a_perpetual():
    assert tv_shot.tv_symbol("solusdt") == "SOLUSDT.P"


def test_shot_asks_the_host_on_the_secret_path():
    http = FakeHTTP()
    png, why = asyncio.run(tv_shot.shot(http, "SOLUSDT"))
    assert (png, why) == (b"PNG", "")
    url, params = http.got[0]
    assert url == "http://host.docker.internal:8424/shot/s3cret"
    assert params == {"symbol": "SOLUSDT.P"}


def test_shot_says_nothing_at_all_when_the_feature_is_off(monkeypatch):
    monkeypatch.setattr(tv_shot, "TV_SHOT_SECRET", "")
    http = FakeHTTP()
    # No reason either: nothing failed, the user never asked for this.
    assert asyncio.run(tv_shot.shot(http, "SOLUSDT")) == (None, "")
    assert http.got == []


def test_shot_turns_an_unreachable_mac_into_a_reason():
    http = FakeHTTP(error=OSError("no route to host"))
    png, why = asyncio.run(tv_shot.shot(http, "SOLUSDT"))
    assert png is None
    assert why == "the capture service did not answer"


def test_shot_passes_the_services_own_reason_through():
    http = FakeHTTP(FakeResponse(503, b"", "the TradingView profile is signed out"))
    assert asyncio.run(tv_shot.shot(http, "SOLUSDT")) == (
        None,
        "the TradingView profile is signed out",
    )


def test_shot_has_a_reason_even_when_the_service_gives_none():
    http = FakeHTTP(FakeResponse(502, b"", ""))
    png, why = asyncio.run(tv_shot.shot(http, "SOLUSDT"))
    assert png is None
    assert why == "the capture service said 502"


def test_an_entry_frame_waits_on_disk_for_its_close():
    assert tv_shot.keep("SOLUSDT", b"PNG") == ""
    assert tv_shot.take("SOLUSDT") == b"PNG"
    # Consumed: the next trade on the same symbol starts with nothing.
    assert tv_shot.take("SOLUSDT") is None


def test_taking_a_frame_nobody_kept_is_not_an_error():
    assert tv_shot.take("NEVERUSDT") is None


def test_a_symbol_can_never_point_the_frame_out_of_its_folder(tmp_path):
    folder = tmp_path / "tv-entry"
    tv_shot.keep("../../etc/SOLUSDT", b"PNG")
    kept = list(folder.iterdir())
    assert [path.name for path in kept] == ["ETCSOLUSDT.png"]
    assert kept[0].resolve().parent == folder.resolve()


def test_keep_says_why_when_the_volume_will_not_take_it(monkeypatch):
    def refuse(self, *args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(tv_shot.Path, "write_bytes", refuse)
    assert tv_shot.keep("SOLUSDT", b"PNG") == "the entry frame could not be saved"


# --- the layout first, the bot's own drawing behind it --------------------
#
# "почему я не вижу реальных графмков", 17.10.2026: every notice carried the
# bot's drawing of the chart while the user's own pane went only to the
# journal. best() is which picture a notice gets.


async def _drew(png=b"DRAWN"):
    return png


def test_the_layouts_pane_wins_when_there_is_one():
    png, why = asyncio.run(tv_shot.best(FakeHTTP(), "SOLUSDT", _drew()))

    assert (png, why) == (b"PNG", "")


def test_the_drawing_is_the_backup_and_the_reason_comes_with_it():
    http = FakeHTTP(FakeResponse(503, b"", "the TradingView profile is signed out"))

    png, why = asyncio.run(tv_shot.best(http, "SOLUSDT", _drew()))

    assert png == b"DRAWN"
    assert why == "the TradingView profile is signed out"


def test_a_shot_and_a_drawing_are_made_at_once():
    """Not one after the other: a notice pays for the slower, not for both."""
    order = []

    class SlowHTTP(FakeHTTP):
        async def get(self, url, params=None, timeout=None):
            order.append("shot started")
            await asyncio.sleep(0)
            order.append("shot done")
            return self.response

    async def drawing():
        order.append("drawing started")
        await asyncio.sleep(0)
        order.append("drawing done")
        return b"DRAWN"

    asyncio.run(tv_shot.best(SlowHTTP(), "SOLUSDT", drawing()))

    # Interleaved, not one after the other.
    assert order == ["drawing started", "shot started", "drawing done", "shot done"]


def test_nothing_is_shot_when_the_capture_is_off(monkeypatch):
    monkeypatch.setattr(tv_shot, "TV_SHOT_SECRET", "")
    http = FakeHTTP()

    assert asyncio.run(tv_shot.best(http, "SOLUSDT", _drew())) == (b"DRAWN", "")
    assert http.got == []


def test_a_drawing_that_throws_still_collects_its_shot():
    """An abandoned task would log "exception was never retrieved"."""

    async def drawing():
        raise ValueError("no candles")

    with pytest.raises(ValueError, match="no candles"):
        asyncio.run(tv_shot.best(FakeHTTP(), "SOLUSDT", drawing()))


# --- the service says what it is running -----------------------------------
#
# It lives on the Mac under launchd; `docker compose up` does not touch it.
# On 17.10.2026 it served two-day-old code for two days, and three deploys
# reported success without going near it.


def _answering(body: str, code: int = 200):
    class HTTP:
        async def get(self, url, timeout=None):
            return SimpleNamespace(status_code=code, text=body)

    return HTTP()


# Two source hashes: what the service is running, and what is on the disk
# beside it. Hex, so the secret scanner has an opinion about them.
RUNNING = "ab12cd34ef56"  # pragma: allowlist secret
ON_DISK = "ff99ff99ff99"  # pragma: allowlist secret


def test_a_fresh_service_reports_just_its_version():
    answered = _answering(f"{RUNNING} {RUNNING} fresh")

    assert asyncio.run(tv_shot.version(answered)) == RUNNING


def test_a_stale_service_says_so_and_names_the_newer_one():
    seen = asyncio.run(tv_shot.version(_answering(f"{RUNNING} {ON_DISK} stale")))

    assert seen == f"{RUNNING} ⚠️ на диске {ON_DISK}"


def test_a_service_that_will_not_answer_is_named_as_such():
    assert asyncio.run(tv_shot.version(_answering("", 503))) == "не отвечает"


def test_a_service_that_is_not_there_at_all(caplog):
    class Dead:
        async def get(self, url, timeout=None):
            raise OSError("connection refused")

    with caplog.at_level("ERROR", logger="relay.tvshot"):
        assert asyncio.run(tv_shot.version(Dead())) == "не отвечает"

    assert "no capture version" in caplog.text


def test_nothing_is_asked_when_the_capture_is_switched_off(monkeypatch):
    monkeypatch.setattr(tv_shot, "TV_SHOT_SECRET", "")

    assert asyncio.run(tv_shot.version(None)) == ""
