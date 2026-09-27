"""Append every closed trade to a Google Sheet — a trade journal that fills
itself.

The sheet side is a tiny Apps Script web app (see README section) that takes
a POST and appends a row; this side just fires the request. No Google SDK,
no service accounts — one URL and a shared secret in the body.

    SHEETS_URL=https://script.google.com/macros/s/.../exec
    SHEETS_SECRET=...        # must match the constant inside the script
    JOURNAL_URL=https://docs.google.com/spreadsheets/d/.../edit   # for links

Both empty disables the journal. Logging is best-effort: a dead sheet must
never block a close notice.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta

import httpx
from dotenv import load_dotenv

log = logging.getLogger("relay.sheets")

# relay.py imports this module before its own load_dotenv, same as speaker.
load_dotenv("bipboop")

SHEETS_URL = os.getenv("SHEETS_URL", "")
SHEETS_SECRET = os.getenv("SHEETS_SECRET", "")
# The sheet's own /edit address. Only used to build the deep link a close
# notice points at; the journal itself is written through SHEETS_URL.
JOURNAL_URL = os.getenv("JOURNAL_URL", "")


def enabled() -> bool:
    return bool(SHEETS_URL and SHEETS_SECRET)


async def log_close(http: httpx.AsyncClient, entry: dict) -> dict | None:
    """Send one closed trade to the journal. Never raises.

    Returns what the sheet answered: `{"row": n, "gid": g}` for an appended
    trade, so the close notice can link the figure it announces at the row it
    landed in. The other payloads (headers, totals, week) answer in plain
    text, which comes back as `{"ok": True}` — truthy, carrying no row.
    None means the write did not happen; the caller drops the link and says
    nothing, because a dead sheet must never hold up a close notice.
    """
    if not enabled():
        return None
    try:
        # text/plain, not application/json: Apps Script's front door answers
        # 405 to a JSON content type but happily parses the same body.
        response = await http.post(
            SHEETS_URL,
            content=json.dumps({"secret": SHEETS_SECRET, **entry}),
            headers={"Content-Type": "text/plain"},
            timeout=20,
            follow_redirects=True,  # Apps Script answers via a redirect
        )
        if response.status_code >= 400:
            log.error("sheets refused: %s %s", response.status_code, response.text[:200])
            return None
    # Deliberately broad: the journal is a bonus, the notice must go out.
    except Exception:  # noqa: BLE001
        log.exception("could not journal the trade")
        return None
    written = _written(response.text)
    log.info("journaled: row %s", written.get("row", "—"))
    return written


def _written(body: str) -> dict:
    """The sheet's answer as a dict. A plain-text "ok" carries no row."""
    try:
        answer = json.loads(body)
    except ValueError:
        return {"ok": True}
    return answer if isinstance(answer, dict) else {"ok": True}


def row_link(written: dict | None) -> str:
    """Deep link to the journal row, or "" when there is nothing to point at.

    JOURNAL_URL is the sheet's own /edit address; the fragment is what makes
    Sheets scroll to and select the row.
    """
    if not written or not JOURNAL_URL:
        return ""
    row = written.get("row")
    if not row:
        return ""
    return f"{JOURNAL_URL}#gid={written.get('gid', 0)}&range=A{row}"


async def week_summary(http: httpx.AsyncClient) -> dict | None:
    """Ask the sheet to append its weekly-total row. Never raises."""
    return await log_close(http, {"week": True})


def seconds_to_sunday(now: datetime) -> float:
    """Seconds until the next Sunday 23:55 (local); a week if that just passed."""
    days_ahead = (6 - now.weekday()) % 7
    target = (now + timedelta(days=days_ahead)).replace(hour=23, minute=55, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=7)
    return (target - now).total_seconds()


async def weekly(http: httpx.AsyncClient) -> None:
    """Post the week's total every Sunday evening, container-local time."""
    while True:
        await asyncio.sleep(seconds_to_sunday(datetime.now()))
        if await week_summary(http):
            log.info("weekly summary posted")
