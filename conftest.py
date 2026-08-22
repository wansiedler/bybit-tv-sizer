"""Test-wide isolation from the developer's own `.env`.

`commands` and `speaker` read their configuration at import time, and they
call `load_dotenv()` themselves because `relay` imports them before it loads
the file. That is right in production and wrong in a test run: a machine with
a speaker configured would make the suite bind the real TTS port and try to
reach the real Cast device. The endpoints are blanked here so every test starts
from an unconfigured speaker; the tests that want speaking on turn it back on
themselves, either through `test_speaker`'s `wired` fixture, which sets the
endpoints to documentation addresses, or by patching `speaker.enabled` outright
as `test_relay` does.
"""

import pytest

import speaker


@pytest.fixture(autouse=True)
def _speaker_unconfigured(monkeypatch):
    monkeypatch.setattr(speaker, "CAST_HOST", "")
    monkeypatch.setattr(speaker, "TTS_HOST", "")
