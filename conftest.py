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

import commands
import speaker


@pytest.fixture(autouse=True)
def _default_config(monkeypatch, tmp_path):
    monkeypatch.setattr(speaker, "CAST_HOST", "")
    monkeypatch.setattr(speaker, "TTS_HOST", "")
    monkeypatch.setattr(commands, "SOURCE", "lexx_dra_bot")
    monkeypatch.setattr(commands, "WATCH_USERS", ["aLexjjcrypt"])
    monkeypatch.setattr(commands, "POLL_TIMEOUT", 25)
    # Nothing in the suite should write a session file into the checkout.
    monkeypatch.setattr("relay.SESSION", str(tmp_path / "session" / "lexx_relay"))
