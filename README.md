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

It also watches chosen people (`WATCH_USERS`, default `@aLexjjcrypt`) and
re-posts everything they write — in any chat your account can see — through the
same bot, prefixed with who wrote it and where:

```text
👤 @aLexjjcrypt · LEXX Trading CLUB:
сетку ставим на OP
```

## Why a user account is involved

A Telegram bot never receives messages sent by another bot — not in private
chats, not in groups. So `@asada3289_bot` cannot read `@lexx_dra_bot` directly.
This relay reads the source with your own account (Telethon) and only uses the
bot for delivery.

## Setup

1. Get `api_id` / `api_hash` at <https://my.telegram.org> → API development tools.
2. Fill the config yourself — no credentials belong in the repo:

   ```bash
   cp .env.example .env
   ```

   - `TG_API_ID`, `TG_API_HASH` — from step 1
   - `BOT_TOKEN` — @BotFather → `@asada3289_bot`
   - `TARGET_CHAT_ID` — leave empty for now

3. Send `/start` to `@asada3289_bot`, then read your chat id and put it in `.env`:

   ```bash
   .venv/bin/python chat_id.py
   ```

4. First run asks for your phone, the login code, and 2FA password if set. The
   login stays local in `lexx_relay.session`:

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
launchctl load ~/Library/LaunchAgents/com.lexx.relay.plist
```

Logs land in `relay.log`. Stop with
`launchctl unload ~/Library/LaunchAgents/com.lexx.relay.plist`.

Run `relay.py --check` interactively at least once first — launchd cannot type
the login code for you.

## Speaking alerts on a Google Nest

Relayed lines can be read out loud: `OP 📈 0.10277` becomes *"OP up, 0.10277"*.

A Cast device is never sent audio — it is handed a URL and fetches the file
itself. So the relay serves the generated speech over HTTP on the LAN and tells
the speaker where to look. That is why `TTS_HOST` must be this machine's LAN
address and never `localhost`: the URL is resolved by the speaker.

Find the speaker, then put both addresses in `.env`:

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
- `.env` → `SOURCE_CHAT` — defaults to `lexx_dra_bot`.

## Docker

The image ships the relay only — credentials stay in your local `.env`, and the
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

Neither `.env` nor the session file is baked into the image (`.dockerignore`).
