# Bybit-TV-sizer

[![CI](https://github.com/wansiedler/bybit-tv-sizer/actions/workflows/ci.yml/badge.svg)](https://github.com/wansiedler/bybit-tv-sizer/actions/workflows/ci.yml)
[![CodeQL](https://github.com/wansiedler/bybit-tv-sizer/actions/workflows/codeql.yml/badge.svg)](https://github.com/wansiedler/bybit-tv-sizer/actions/workflows/codeql.yml)
[![Quality Gate Status](https://sonarcloud.io/api/project_badges/measure?project=wansiedler_bybit-tv-sizer&metric=alert_status)](https://sonarcloud.io/summary/new_code?id=wansiedler_bybit-tv-sizer)
[![Maintainability Rating](https://sonarcloud.io/api/project_badges/measure?project=wansiedler_bybit-tv-sizer&metric=sqale_rating)](https://sonarcloud.io/summary/new_code?id=wansiedler_bybit-tv-sizer)
[![Security Rating](https://sonarcloud.io/api/project_badges/measure?project=wansiedler_bybit-tv-sizer&metric=security_rating)](https://sonarcloud.io/summary/new_code?id=wansiedler_bybit-tv-sizer)
[![Reliability Rating](https://sonarcloud.io/api/project_badges/measure?project=wansiedler_bybit-tv-sizer&metric=reliability_rating)](https://sonarcloud.io/summary/new_code?id=wansiedler_bybit-tv-sizer)
[![coverage: 100%](https://img.shields.io/badge/coverage-100%25_branches-brightgreen)](#tests)
[![python](https://img.shields.io/badge/python-3.12_%7C_3.13_%7C_3.14-blue)](https://github.com/wansiedler/bybit-tv-sizer/actions/workflows/ci.yml)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![mypy: checked](https://img.shields.io/badge/mypy-checked-blue)](https://mypy-lang.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**A risk-manager watchdog bot for the owner's Bybit account.**

The watchdog checks three rules every half second:

1. Spotted a position with no stop-loss? Instant reduce-only market close.
2. A position leveraged above the limit (`MAX_LEVERAGE=1` — configurable,
   but 1x is how this account trades for now) → instant market close, and
   the instrument's leverage is reset back to 1x.
3. An entry limit order with no stop → the order is cancelled. Reduce-only
   exits, conditional stop/take orders and limit orders that do carry a
   stop are left alone.

The bot works in tandem with TradingView's long/short position tool and
keeps every trade risking exactly 1% of the deposit: draw a long/short in
TV with an entry, stop and take sized at $100 but a 2% stop — and it
lowers the entry limit to $50 on its own.

It receives native TradingView alerts, announces them on the speaker and
talks in Telegram: every entry and exit goes to the owner's chat with a
chart screenshot and a fee-aware report.

And it fills the Google Sheets trade journal automatically.

With `GUARD_GRACE_SEC` above zero the guard warns first and gives the
breach that many seconds to be fixed; at zero it acts immediately. A
forced close is labelled everywhere — in Telegram, and in the journal as
«принудительно остановлено» with the broken rule.

## Auto-trim: a market entry is cut back to the target risk

The sizer shapes limit orders before they fill. But sometimes the entry is
thrown in at market instead — the quantity stays sized for the old price and
the stop suddenly risks more than `RISK_PCT`. The trimmer (`RISK_TRIM=1`,
the default) notices and shrinks the position with a reduce-only market
order until stopping out costs the target again. It only ever shrinks.

The budget pays for everything between the entry and the stop, not just the
price distance — every leg is a taker order:

```text
stop cost per unit = |entry − stop| + entry·fee + stop·fee
```

and when a cut is needed, the kept size also answers for the cut's own
market close (the entry fee is already sunk on the full size):

```text
target ≥ size·entry·fee  +  (size − keep)·entry·fee  +  keep·(|entry − stop| + stop·fee)
```

solved for `keep`, floored to the exchange's lot step.

Worked example — a 100 000 $ deposit, 1 BTC bought at market for 100 000
with the stop at 98 000, `RISK_PCT=1%`, taker fee 0.055%:

```text
stop cost per unit:   2 000 + 55.00 + 53.90 = 2 108.90 $/BTC
position risk:        2 108.90 $ = 2.11% of the deposit → trim
budget for the keep:  1 000 − 2 × 55.00 = 890 $
keep:                 890 / (2 000 + 53.90 − 55.00) → 0.445 BTC

✂️ BTC: риск 2.11% депо при цели 1.00% — режу 1→0.445
```

Stopping out afterwards costs 55.00 (entry fee on the full size) + 30.53
(the cut's market close) + 890.00 (price distance on the keep) + 23.99
(the keep's stop) = 999.51 $ — inside the 1% all in. Funding and stop
slippage are not budgeted; at 0.01%/8h funding is noise next to 1%.

## Alert relaying

Compacts LEXX Draco alerts and re-posts them through your own bot.

```text
🔔 #OPUSDT OPUSDT, Пересечение 0.10282
-  exchange:  #BybitFutures
-  trend: 📈
-  price: 0.10277
```

becomes

```text
OP 📈 0.10277
```

It also watches chosen people (`WATCH_USERS`, default `@some_trader`) and
re-posts everything they write — in any chat your account can see — through the
same bot, prefixed with who wrote it and where:

```text
👤 @some_trader · Trading Club:
сетку ставим на OP
```

## Why a user account is involved

A Telegram bot never receives messages sent by another bot — not in private
chats, not in groups. So `@your_bot` cannot read `@source_bot` directly.
This relay reads the source with your own account (Telethon) and only uses the
bot for delivery.

## Setup

1. Get `api_id` / `api_hash` at <https://my.telegram.org> → API development tools.
2. Fill the config yourself — no credentials belong in the repo:

   ```bash
   cp bipboop.example bipboop
   ```

   - `TG_API_ID`, `TG_API_HASH` — from step 1
   - `BOT_TOKEN` — @BotFather → `@your_bot`
   - `TARGET_CHAT_ID` — leave empty for now

3. Send `/start` to `@your_bot`, then read your chat id and put it in `bipboop`:

   ```bash
   .venv/bin/python chat_id.py
   ```

4. First run asks for your phone, the login code, and 2FA password if set. The
   login stays local in `session/lexx_relay.session`:

   ```bash
   .venv/bin/python relay.py --check
   ```

   `--check` resolves the source, validates the bot token, and sends one test
   line so you see the whole chain work before leaving it running.

## Run

```bash
.venv/bin/python relay.py
```

Non-matching messages (start-ups, errors, PnL) are skipped.

On start and on shutdown the relay posts a lifecycle line through the bot
(`🟢 lexx-relay up …` / `🔴 lexx-relay down — SIGTERM, uptime 3h12m`). SIGTERM and
SIGINT are handled: the relay says goodbye, disconnects and exits 0. Silence the
notices with `NOTIFY_LIFECYCLE=0`, rename them with `RELAY_NAME`.

## Keep it running after reboot

```bash
cp com.lexx.relay.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.lexx.relay.plist
```

The agent runs `launchd-start.sh`: it first clears colima's stale `vz.pid`
if an unclean shutdown left one behind (lima otherwise refuses to start with
"vz driver is running but host agent is not"), waits for the Docker daemon
(colima's own LaunchAgent brings that up), then runs `docker compose up -d`
and exits. Crash recovery is the container's job — `restart: unless-stopped`
in `docker-compose.yml` — so the agent has no `KeepAlive`; it only guarantees
the compose project comes up after login.

Never run `relay.py` directly while the container is up: the two instances
fight over the bot's `getUpdates` (a stream of 409s), and whichever grabs
port 8422 first leaves the other's speaker casts silent — the Nest plays its
start chime and then nothing, because the audio fetch hits a dead port.

Check it came up, then watch the logs:

```bash
launchctl print gui/$(id -u)/com.lexx.relay | grep -E 'state|pid|last exit'
tail -f ~/Library/Logs/lexx-relay-launchd.log
docker logs -f lexx-relay
```

Stop the relay with `docker compose down` (the agent will bring it back next
login); retire the agent with `launchctl bootout gui/$(id -u)/com.lexx.relay`
followed by removing the plist from `~/Library/LaunchAgents`.

`reboot.sh` says "Rebooting" on the Nest (through the running container's
speech stack) and then restarts the machine. Alias it so `reboot` and
`sudo reboot` both go through it — the trailing space in the `sudo` alias
makes zsh alias-expand the word that follows:

```bash
alias sudo='sudo '
alias reboot='/Users/me/lexx-relay/reboot.sh'
```

Complete the Telegram login interactively at least once first — launchd
cannot type the code for you:

```bash
docker compose run --rm relay python relay.py --check
```

This is a LaunchAgent, so it starts at **login**, not at boot: a Mac sitting at
the login window is a Mac with no relay. If it has to run headless from power-on,
the same plist belongs in `/Library/LaunchDaemons` with a `UserName` key and
absolute paths, installed as root.

### When it does not come up

`restart: unless-stopped` means a relay that cannot start is restarted
forever and the only evidence is `docker logs lexx-relay` growing. Two
failures look identical from the outside and are worth checking first:

- `EOFError: EOF when reading a line` from `client.start()` — Telethon is asking
  for a phone number because the session it opened is not an authorized one.
  `SESSION_NAME` and the working directory decide which file that is; the login
  lives in whichever `.session` file you actually completed `--check` against.
  A session file exists as soon as anything connects, so its presence proves
  nothing — `sqlite3 <file> 'select count(*) from entities;'` returning 0 is a
  good sign it never logged in.
- `commands.Refused: {'ok': False, 'error_code': 404, ...}` on `getUpdates`
  while alerts still relay fine — the bot token reached `relay.py` but not
  `commands.py`. Both that module and `speaker` call `load_dotenv()` themselves
  for exactly this reason, because `relay.py` imports them before it loads the
  file; a 404 here means that call went missing.

The desktop and Docker runs share one login by pointing at the same file —
`SESSION_NAME=session/lexx_relay` on the host, which is the `session/` volume
Docker mounts at `/app/session`. Two copies of one session drift apart and one
of them ends up unauthorized.

## Speaking alerts on a Google Nest

Relayed lines can be read out loud: `OP 📈 0.10277` becomes *"OP up, 0.10277"*.

A Cast device is never sent audio — it is handed a URL and fetches the file
itself. So the relay serves the generated speech over HTTP on the LAN and tells
the speaker where to look. That is why `TTS_HOST` must be this machine's LAN
address and never `localhost`: the URL is resolved by the speaker.

Find the speaker, then put both addresses in `bipboop`:

```bash
dns-sd -B _googlecast._tcp local
```

```bash
ipconfig getifaddr en0
```

- `CAST_HOST` — the speaker's address
- `TTS_HOST` — this machine, as the speaker sees it
- `TTS_PORT` — published to the LAN in `docker-compose.yml` (default 8422)
- `SPEAK_ALERTS=0` — mute it without removing the config
- `CAST_UUID` — optional; without it a stable id is derived from the address

Speaking is best-effort: a speaker that is asleep, busy or unreachable is
logged and skipped, and the Telegram side is unaffected. If casting starts
failing with a refused connection while the rest of the LAN is reachable, the
speaker itself is wedged — power-cycle it.

## Tests

```bash
.venv/bin/pytest
```

With coverage, the way CI runs it:

```bash
.venv/bin/pytest --cov --cov-report=term-missing
```

Every shipped file is at 100%, statements and branches both, and
`fail_under = 100` in `pyproject.toml` keeps it there — a new line without a
test reds the build. Telegram and the network are never touched: `TelegramClient`
and `httpx.AsyncClient` are replaced with fakes, and the shutdown path is driven
by calling the relay's own signal handlers through a recording event loop rather
than by signalling the test process.

## Development

The CI gates run locally through the same hook suite:

```bash
uv venv .venv --python 3.13
uv pip install --python .venv/bin/python -r requirements.lock -r .github/requirements-ci.lock
.venv/bin/pre-commit install --install-hooks
.venv/bin/pre-commit run --all-files
```

Hooks: ruff (lint + format), mypy, bandit, detect-secrets, codespell, yamllint,
markdownlint, workflow/dependabot schema checks, commitizen on commit messages.
CI adds pytest on 3.12/3.13, actionlint, a Trivy filesystem and image scan, and
an image build with a smoke test.

The workflow itself can be run locally with [act](https://nektosact.com);
the settings that make it work on a colima host live in `.actrc`. Point act at
the daemon and run a job:

```bash
export DOCKER_HOST="unix://$HOME/.colima/default/docker.sock"
act -j tests
```

The `docker` job is the one to leave to real CI — it needs a daemon inside the
job container, which is exactly the mount `.actrc` disables.

## VPS tunnel

The bot egresses through a fixed-IP VPS so the Bybit key can be
IP-whitelisted — setup, configs and debugging in
[docs/vps-tunnel.md](docs/vps-tunnel.md).

## Trade journal in Google Sheets

Every closed trade lands as a row in a Google Sheet — pair, side, win/stop,
RR, the R multiple, full money figures, a screenshot chip on your Drive —
plus a weekly total row every Sunday at 23:55 and automatic deposit and
withdrawal rows. One-time setup:

1. **The spreadsheet.** Create one (or copy your diary template) and note its
   link. The relay writes to the FIRST sheet of it.
2. **The Apps Script — where the function lives.** The receiving function is
   not part of this repo's runtime: it lives *inside the spreadsheet*. Open
   the sheet → **Extensions → Apps Script** — an editor opens with a single
   `Code.gs` file bound to that spreadsheet. Replace its whole content with
   the `doPost` script from [docs/sheets-script.js](docs/sheets-script.js),
   set `SECRET` in its first line to a long random string of your own (the
   same one goes into `SHEETS_SECRET` later) and save with Cmd/Ctrl+S. Any
   later change to the function is made in this same editor — followed by
   Deploy → Manage deployments → edit → **New version**, never a fresh
   deployment (see below).
3. **A Google Cloud project** — needed because Google hard-blocks the Drive
   scope for anonymous scripts:
   - console.cloud.google.com → New project.
   - APIs & Services → OAuth consent screen → External, keep it in
     **Testing**, add your own gmail under **Test users**.
   - Enable **Google Drive API** and **Google Sheets API** in that project
     (APIs & Services → Library).
   - In the Apps Script editor: Project Settings (⚙️) → GCP project →
     Change project → paste the project number.
   - In the editor's Services panel (+) add **Google Sheets API** — the
     screenshot chips go through it.
4. **Authorize.** Run the `authTest` function once (Run ▶) and allow the
   permissions — the "unverified" warning is fine, you are the test user.
5. **Deploy.** Deploy → New deployment → Web app, Execute as **Me**, access
   **Anyone**. Copy the `/exec` URL.
6. **Wire the relay.** In `bipboop`:

   ```ini
   SHEETS_URL=https://script.google.com/macros/s/…/exec
   SHEETS_SECRET=the same string as in the script
   JOURNAL_URL=https://docs.google.com/spreadsheets/d/…/edit
   ```

   Restart the container. `/links` in the bot answers with the journal.

Redeploys of the script must go through Deploy → **Manage deployments** →
edit → **New version** — a fresh "New deployment" changes the `/exec` URL and
breaks `SHEETS_URL`.

The script itself lives in the spreadsheet, not in this repo; the reference
copy is in [docs/sheets-script.js](docs/sheets-script.js). It handles four
payloads: `{row: [...]}` appends a trade (with an optional base64 `png`
saved to the Drive folder `trade-screens` and chipped into column G),
`{week: true}` appends the weekly total, `{headers: [...]}` rewrites row 1,
`{totals: true}` builds the frozen running-total row 2.

## Tuning

- `parser.py` → `QUOTES` — quote assets stripped from the pair (`OPUSDT` → `OP`).
- `parser.py` → `parse_alert` — a message is relayed only when symbol, trend and
  price are all present.
- `bipboop` → `SOURCE_CHAT` — defaults to `source_bot`.

## Docker

The image ships the relay only — credentials stay in your local `bipboop`, and the
Telethon session is kept in the `session/` volume so the login survives
restarts.

```bash
docker compose build
```

First run needs a terminal, because Telegram asks for the login code:

```bash
docker compose run --rm relay python relay.py --check
```

Then run it detached:

```bash
docker compose up -d
docker compose logs -f
```

Neither `bipboop` nor the session file is baked into the image (`.dockerignore`).
