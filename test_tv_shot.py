"""Tests for the relay's side of the TradingView capture. HTTP is faked.

What matters here is that a shot which does not happen never costs anything
but the picture, and always says which way it failed.
"""

import asyncio

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


def test_capture_entry_keeps_the_frame_and_stays_quiet():
    said = []

    async def say(text):
        said.append(text)

    asyncio.run(tv_shot.capture_entry(FakeHTTP(), "SOLUSDT", say))
    assert tv_shot.take("SOLUSDT") == b"PNG"
    assert said == []


def test_capture_entry_says_why_there_is_no_frame():
    said = []

    async def say(text):
        said.append(text)

    http = FakeHTTP(FakeResponse(503, b"", "TradingView would not show SOLUSDT.P"))
    asyncio.run(tv_shot.capture_entry(http, "SOLUSDT", say))
    assert said == ["📷 SOLUSDT: TradingView would not show SOLUSDT.P"]
    assert tv_shot.take("SOLUSDT") is None
