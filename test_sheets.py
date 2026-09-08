"""Tests for the Google Sheets trade journal. HTTP is faked."""

import asyncio

import pytest

import sheets


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(sheets, "SHEETS_URL", "https://script.google.com/macros/s/x/exec")
    monkeypatch.setattr(sheets, "SHEETS_SECRET", "s3cret")


class FakeResponse:
    def __init__(self, status_code=200, text="ok"):
        self.status_code = status_code
        self.text = text


class FakeHTTP:
    def __init__(self, response=None, error=None):
        self.response = response or FakeResponse()
        self.error = error
        self.posted: list[dict] = []

    async def post(self, url, content=None, headers=None, timeout=None, follow_redirects=False):
        if self.error is not None:
            raise self.error
        import json as _json

        assert headers == {"Content-Type": "text/plain"}
        self.posted.append(_json.loads(content))
        return self.response


def test_enabled_needs_url_and_secret(monkeypatch):
    assert sheets.enabled() is True
    monkeypatch.setattr(sheets, "SHEETS_SECRET", "")
    assert sheets.enabled() is False


def test_log_close_posts_the_row_with_the_secret():
    http = FakeHTTP()

    ok = asyncio.run(sheets.log_close(http, {"symbol": "CL", "pnl": -0.04}))

    assert ok is True
    assert http.posted == [
        {"secret": "s3cret", "symbol": "CL", "pnl": -0.04}  # pragma: allowlist secret
    ]


def test_log_close_disabled_without_config(monkeypatch):
    monkeypatch.setattr(sheets, "SHEETS_URL", "")
    http = FakeHTTP()

    assert asyncio.run(sheets.log_close(http, {"symbol": "CL"})) is False
    assert http.posted == []


def test_log_close_reports_a_refusal(caplog):
    http = FakeHTTP(response=FakeResponse(status_code=403, text="denied"))

    with caplog.at_level("ERROR", logger="relay.sheets"):
        assert asyncio.run(sheets.log_close(http, {"symbol": "CL"})) is False

    assert "sheets refused" in caplog.text


def test_log_close_survives_transport_errors(caplog):
    http = FakeHTTP(error=OSError("no route"))

    with caplog.at_level("ERROR", logger="relay.sheets"):
        assert asyncio.run(sheets.log_close(http, {"symbol": "CL"})) is False

    assert "could not journal" in caplog.text


def test_seconds_to_sunday_targets_the_coming_sunday_evening():
    from datetime import datetime, timedelta

    wednesday = datetime(2026, 9, 9, 12, 0)  # a Wednesday noon
    seconds = sheets.seconds_to_sunday(wednesday)

    assert wednesday + timedelta(seconds=seconds) == datetime(2026, 9, 13, 23, 55)


def test_seconds_to_sunday_rolls_over_right_after_the_deadline():
    from datetime import datetime, timedelta

    late_sunday = datetime(2026, 9, 13, 23, 56)
    seconds = sheets.seconds_to_sunday(late_sunday)

    assert late_sunday + timedelta(seconds=seconds) == datetime(2026, 9, 20, 23, 55)


def test_weekly_posts_the_summary_every_wakeup(monkeypatch):
    posted = []
    naps = []

    async def fake_summary(http):
        posted.append(True)
        return len(posted) == 1  # first posts fine, second is refused

    async def fake_sleep(seconds):
        naps.append(seconds)
        if len(naps) > 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(sheets, "week_summary", fake_summary)
    monkeypatch.setattr(sheets.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(sheets, "seconds_to_sunday", lambda now: 1.0)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(sheets.weekly(None))

    assert posted == [True, True]
    assert naps == [1.0, 1.0, 1.0]


def test_week_summary_posts_the_flag():
    import json

    sent = {}

    class FakeHTTP:
        async def post(self, url, content=None, headers=None, timeout=None, follow_redirects=None):
            sent.update(json.loads(content))

            class R:
                status_code = 200
                text = "ok"

            return R()

    ok = asyncio.run(sheets.week_summary(FakeHTTP()))

    assert ok is True
    assert sent["week"] is True and "secret" in sent
