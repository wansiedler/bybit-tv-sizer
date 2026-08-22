"""Test-wide isolation from the developer's own `.env`.

`commands` and `speaker` read their configuration at import time, and they
call `load_dotenv()` themselves because `relay` imports them before it loads
the file. That is right in production and wrong in a test run: a machine with
a speaker configured would make the suite bind the real TTS port and try to
reach the real Cast device. The endpoints are blanked here so every test sees
the same unconfigured speaker, and the one test that wants speaking on patches
`speaker.enabled` for itself.
"""

import pytest

import speaker


@pytest.fixture(autouse=True)
def _speaker_unconfigured(monkeypatch):
    monkeypatch.setattr(speaker, "CAST_HOST", "")
    monkeypatch.setattr(speaker, "TTS_HOST", "")
