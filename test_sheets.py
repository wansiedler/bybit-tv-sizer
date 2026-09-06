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

    async def post(self, url, json=None, timeout=None, follow_redirects=False):
        if self.error is not None:
            raise self.error
        self.posted.append(json)
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
