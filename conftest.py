"""Test-wide isolation from the developer's own `.env`.

`commands` and `speaker` read their configuration at import time, and they
call `load_dotenv()` themselves because `relay` imports them before it loads
the file. That is right in production and wrong in a test run: whatever the
developer happens to relay in real life would decide what the suite asserts
against, and a machine with a speaker configured would bind the real TTS port
and try to reach the real Cast device.

Every value the suite has an opinion about is pinned back to its documented
default here. Tests that want something else say so themselves — `test_speaker`
configures the endpoints through its `wired` fixture, and `test_relay` patches
`speaker.enabled` outright.
"""

import pytest

import bybit_watch
import commands
import ip_watch
import sheets
import speaker
import tv_alerts


@pytest.fixture(autouse=True)
def _default_config(monkeypatch, tmp_path):
    monkeypatch.setattr(speaker, "CAST_HOST", "")
    monkeypatch.setattr(speaker, "TTS_HOST", "")
    # Real Bybit keys in the developer's .env must not arm the watcher or the
    # sizer inside the suite; tests that want them keyed say so themselves.
    monkeypatch.setattr(bybit_watch, "API_KEY", "")
    monkeypatch.setattr(bybit_watch, "API_SECRET", "")
    monkeypatch.setattr(bybit_watch, "API_URL", "https://api.bybit.com")
    monkeypatch.setattr(bybit_watch, "RISK_TARGET", 0.005)
    monkeypatch.setattr(bybit_watch, "MIN_RR", 2.0)
    # The guard would market-close most fixture positions (no stop); tests
    # that exercise it flip it back on themselves.
    monkeypatch.setattr(bybit_watch, "RISK_GUARD", False)
    monkeypatch.setattr(bybit_watch, "GUARD_MAX_LEVERAGE", 1.0)
    monkeypatch.setattr(bybit_watch, "GUARD_GRACE", 45.0)
    bybit_watch._guard_seen.clear()
    bybit_watch._guard_closed.clear()
    monkeypatch.setattr(tv_alerts, "TV_WEBHOOK_SECRET", "")
    monkeypatch.setattr(tv_alerts, "JOURNAL_URL", "")
    monkeypatch.setattr(tv_alerts, "TV_PUBLIC_URL", "")
    monkeypatch.setattr(sheets, "SHEETS_URL", "")
    monkeypatch.setattr(sheets, "SHEETS_SECRET", "")
    monkeypatch.setattr(ip_watch, "ENABLED", False)
    # The developer's HTTPS_PROXY points at a real WireGuard peer; the
    # suite must never dial it.
    monkeypatch.setattr(ip_watch, "PROXY", "")
    monkeypatch.setattr(commands, "SOURCE", "source_bot")
    monkeypatch.setattr(commands, "WATCH_USERS", ["some_trader"])
    monkeypatch.setattr(commands, "POLL_TIMEOUT", 25)
    # Nothing in the suite should write a session file into the checkout.
    monkeypatch.setattr("relay.SESSION", str(tmp_path / "session" / "lexx_relay"))
