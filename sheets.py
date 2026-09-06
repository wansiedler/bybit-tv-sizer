"""Append every closed trade to a Google Sheet — a trade journal that fills
itself.

The sheet side is a tiny Apps Script web app (see README section) that takes
a POST and appends a row; this side just fires the request. No Google SDK,
no service accounts — one URL and a shared secret in the body.

    SHEETS_URL=https://script.google.com/macros/s/.../exec
    SHEETS_SECRET=...        # must match the constant inside the script

Both empty disables the journal. Logging is best-effort: a dead sheet must
never block a close notice.
"""

import json
import logging
import os

import httpx
from dotenv import load_dotenv

log = logging.getLogger("relay.sheets")

# relay.py imports this module before its own load_dotenv(), same as speaker.
load_dotenv()

SHEETS_URL = os.getenv("SHEETS_URL", "")
SHEETS_SECRET = os.getenv("SHEETS_SECRET", "")


def enabled() -> bool:
    return bool(SHEETS_URL and SHEETS_SECRET)


async def log_close(http: httpx.AsyncClient, entry: dict) -> bool:
    """Send one closed trade to the journal. Never raises."""
    if not enabled():
        return False
    try:
        # text/plain, not application/json: Apps Script's front door answers
        # 405 to a JSON content type but happily parses the same body.
        response = await http.post(
            SHEETS_URL,
            content=json.dumps({"secret": SHEETS_SECRET, **entry}),
            headers={"Content-Type": "text/plain"},
            timeout=20,
            follow_redirects=True,  # Apps Script answers через redirect
        )
        if response.status_code >= 400:
            log.error("sheets refused: %s %s", response.status_code, response.text[:200])
            return False
    # Deliberately broad: the journal is a bonus, the notice must go out.
    except Exception:  # noqa: BLE001
        log.exception("could not journal the trade")
        return False
    log.info("journaled: %s", entry.get("symbol"))
    return True
