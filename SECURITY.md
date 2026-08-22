# Security Policy

## Scope

This is a single-user relay: it reads one Telegram bot's alerts with the
owner's own account and re-posts them through the owner's bot. There are no
releases and no version matrix — the deployed thing is whatever `main` builds,
so **only `main` is supported**. Fixes land there and nowhere else.

## What is sensitive here

- `.env` — `TG_API_ID`, `TG_API_HASH`, `BOT_TOKEN`, `TARGET_CHAT_ID`. Both the
  file and the session below are gitignored and are never copied into the image
  (`.dockerignore`).
- `session/lexx_relay.session` — **a full Telegram login**. Anyone holding this
  file can act as the account. It is worth more than the bot token: a bot token
  can be revoked in @BotFather, a session cannot be revoked selectively — you
  have to terminate the session from a Telegram client.
- The bot's username is public, so anyone can message it. Commands are answered
  only for `TARGET_CHAT_ID`; anything else is logged and dropped.

## Reporting a vulnerability

Open a private security advisory:
<https://github.com/wansiedler/lexx-relay/security/advisories/new>

For anything involving the credentials above, revoke first and report after:

1. `/revoke` in @BotFather for the bot token.
2. Telegram → Settings → Devices → terminate the relay's session.
3. Rotate `TG_API_HASH` at <https://my.telegram.org> if it may have leaked.

Expect a reply within a week. This is a personal project, not a staffed one —
there is no bounty and no SLA beyond that.

## What the pipeline already enforces

Every push to `main` runs, and blocks on:

- `detect-secrets` and Trivy's secret scanner over the whole tree, so a pasted
  token fails the build;
- Trivy vulnerability and misconfiguration scans of both the filesystem and the
  built image;
- `bandit` at high severity;
- installs verified against hash-locked requirement files, with actions pinned
  to full commit SHAs.

The image runs as an unprivileged user and carries no `pip`.

## Known accepted risks

- The speech the relay sends to a Google Nest is fetched by the speaker over
  plain HTTP on the local network. HTTPS would need a certificate the speaker
  trusts; the exposure is a LAN listener serving generated audio clips.
- Base-image CVEs in `python:3.14-slim` are reported but not gated on: most
  carry no fixed version. They are visible in the run summary of every build.
