# lexx-relay

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
