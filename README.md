# lexx-relay

Compacts LEXX Draco alerts and re-posts them through your own bot.

```
🔔 #OPUSDT OPUSDT, Пересечение 0.10282
-  exchange:  #BybitFutures
-  trend: 📈
-  price: 0.10277
```
becomes
```
OP 📈 0.10277
```

## Why a user account is involved

A Telegram bot never receives messages sent by another bot — not in private
chats, not in groups. So `@your_bot` cannot read `@source_bot` directly.
This relay reads the source with your own account (Telethon) and only uses the
bot for delivery.

## Setup

1. Get `api_id` / `api_hash` at https://my.telegram.org → API development tools.
2. Fill the config yourself — no credentials belong in the repo:
   ```
   cp .env.example .env
   ```
   - `TG_API_ID`, `TG_API_HASH` — from step 1
   - `BOT_TOKEN` — @BotFather → `@your_bot`
   - `TARGET_CHAT_ID` — leave empty for now
3. Send `/start` to `@your_bot`, then read your chat id and put it in `.env`:
   ```
   .venv/bin/python chat_id.py
   ```
4. First run asks for your phone, the login code, and 2FA password if set. The
   login stays local in `lexx_relay.session`:
   ```
   .venv/bin/python relay.py --check
   ```
   `--check` resolves the source, validates the bot token, and sends one test
   line so you see the whole chain work before leaving it running.

## Run

```
.venv/bin/python relay.py
```

Non-matching messages (start-ups, errors, PnL) are skipped.

## Keep it running after reboot

```
cp com.lexx.relay.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.lexx.relay.plist
```

Logs land in `relay.log`. Stop with
`launchctl unload ~/Library/LaunchAgents/com.lexx.relay.plist`.

Run `relay.py --check` interactively at least once first — launchd cannot type
the login code for you.

## Tests

```
.venv/bin/python -c "import test_parser as t; [getattr(t,n)() for n in dir(t) if n.startswith('test_')]; print('ok')"
```

## Tuning

- `parser.py` → `QUOTES` — quote assets stripped from the pair (`OPUSDT` → `OP`).
- `parser.py` → `parse_alert` — a message is relayed only when symbol, trend and
  price are all present.
- `.env` → `SOURCE_CHAT` — defaults to `source_bot`.

## Docker

The image ships the relay only — credentials stay in your local `.env`, and the
Telethon session is kept in the `session/` volume so the login survives
restarts.

```
docker compose build
```

First run needs a terminal, because Telegram asks for the login code:

```
docker compose run --rm relay python relay.py --check
```

Then run it detached:

```
docker compose up -d
docker compose logs -f
```

Neither `.env` nor the session file is baked into the image (`.dockerignore`).
