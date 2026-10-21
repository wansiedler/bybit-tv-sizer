# Bybit-TV-sizer

[![CI](https://github.com/wansiedler/bybit-tv-sizer/actions/workflows/ci.yml/badge.svg)](https://github.com/wansiedler/bybit-tv-sizer/actions/workflows/ci.yml)
[![CodeQL](https://github.com/wansiedler/bybit-tv-sizer/actions/workflows/codeql.yml/badge.svg)](https://github.com/wansiedler/bybit-tv-sizer/actions/workflows/codeql.yml)
[![Quality Gate Status](https://sonarcloud.io/api/project_badges/measure?project=wansiedler_bybit-tv-sizer&metric=alert_status)](https://sonarcloud.io/summary/new_code?id=wansiedler_bybit-tv-sizer)
[![Maintainability Rating](https://sonarcloud.io/api/project_badges/measure?project=wansiedler_bybit-tv-sizer&metric=sqale_rating)](https://sonarcloud.io/summary/new_code?id=wansiedler_bybit-tv-sizer)
[![Security Rating](https://sonarcloud.io/api/project_badges/measure?project=wansiedler_bybit-tv-sizer&metric=security_rating)](https://sonarcloud.io/summary/new_code?id=wansiedler_bybit-tv-sizer)
[![Reliability Rating](https://sonarcloud.io/api/project_badges/measure?project=wansiedler_bybit-tv-sizer&metric=reliability_rating)](https://sonarcloud.io/summary/new_code?id=wansiedler_bybit-tv-sizer)
[![Bugs](https://sonarcloud.io/api/project_badges/measure?project=wansiedler_bybit-tv-sizer&metric=bugs)](https://sonarcloud.io/summary/new_code?id=wansiedler_bybit-tv-sizer)
[![Vulnerabilities](https://sonarcloud.io/api/project_badges/measure?project=wansiedler_bybit-tv-sizer&metric=vulnerabilities)](https://sonarcloud.io/summary/new_code?id=wansiedler_bybit-tv-sizer)
[![Code Smells](https://sonarcloud.io/api/project_badges/measure?project=wansiedler_bybit-tv-sizer&metric=code_smells)](https://sonarcloud.io/project/issues?id=wansiedler_bybit-tv-sizer&issueStatuses=OPEN)
[![Coverage](https://sonarcloud.io/api/project_badges/measure?project=wansiedler_bybit-tv-sizer&metric=coverage)](https://sonarcloud.io/component_measures?id=wansiedler_bybit-tv-sizer&metric=coverage)
[![Duplicated Lines (%)](https://sonarcloud.io/api/project_badges/measure?project=wansiedler_bybit-tv-sizer&metric=duplicated_lines_density)](https://sonarcloud.io/component_measures?id=wansiedler_bybit-tv-sizer&metric=duplicated_lines_density)
[![Technical Debt](https://sonarcloud.io/api/project_badges/measure?project=wansiedler_bybit-tv-sizer&metric=sqale_index)](https://sonarcloud.io/component_measures?id=wansiedler_bybit-tv-sizer&metric=sqale_index)
[![Lines of Code](https://sonarcloud.io/api/project_badges/measure?project=wansiedler_bybit-tv-sizer&metric=ncloc)](https://sonarcloud.io/component_measures?id=wansiedler_bybit-tv-sizer&metric=ncloc)
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

Four streams can be given a chat of their own, so `TARGET_CHAT_ID` is left
to the account — entries, exits, the risk manager, the answers to commands:

| variable | what moves |
| --- | --- |
| `ALERTS_CHAT_ID` | the alerts from `SOURCE_CHAT` and the TV webhook |
| `ERRORS_CHAT_ID` | everything logged at ERROR |
| `WATCHED_CHAT_ID` | the `WATCH_USERS` relay |
| `DAILY_CHAT_ID` | the end-of-day summary |

Empty leaves that stream where it has always been. The bot must be a member
of the chat, and an admin to post in a channel; an id it cannot post to
costs nothing, since the message falls back to `TARGET_CHAT_ID` rather than
disappearing.

And it fills the Google Sheets trade journal automatically.

With `GUARD_GRACE_SEC` above zero the guard warns first and gives the
breach that many seconds to be fixed; at zero it acts immediately. A
forced close is labelled everywhere — in Telegram, and in the journal as
"force-stopped" with the broken rule.

A missing stop is the one breach the guard can repair rather than punish.
When the grace runs out on a stopless position it places a stop worth
`GUARD_STOP_PCT` of the wallet (1% by default) instead of closing the
trade — an entry thrown in at market is a trade that wanted to happen, and
a stop is what it was missing:

```text
🛑 BTC: no stop — setting a stop in 45s
🛡 BTC: no stop — set at 80288.8 (-799.91, 1% of the deposit)
```

That percent is the whole cost of stopping out, both fees inside, exactly
as `RISK_PCT` means it in the sizer. Your own stop always wins: the grace
runs first, and a stop you set yourself is never moved, however wide.
`GUARD_STOP_PCT=0` goes back to closing.

When no such stop exists — the position is large enough that the fees
alone spend the budget, or the market has already run past where the stop
would sit — the budget is gone, and the guard closes as it always did.

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

✂️ BTC: risk 2.11% depo vs target 1.00% — cutting 1→0.445
```

Stopping out afterwards costs 55.00 (entry fee on the full size) + 30.53
(the cut's market close) + 890.00 (price distance on the keep) + 23.99
(the keep's stop) = 999.51 $ — inside the 1% all in. Funding and stop
slippage are not budgeted; at 0.01%/8h funding is noise next to 1%.

## A ceiling on the deposit the risk is taken from (`RISK_EQUITY_MAX`)

`RISK_PCT` is a share of the wallet, so the position grows with the account.
`RISK_EQUITY_MAX` caps the base it is taken from:

```text
risk base = min(wallet balance, RISK_EQUITY_MAX)      # 0 = no ceiling
```

With `RISK_EQUITY_MAX=100000` and `RISK_PCT=1`, a stop-out costs 1 000 $
whether the wallet holds 100 000 or 300 000 — above the ceiling the size
stops scaling, below it nothing changes (a 40 000 $ wallet still risks 400).

The ceiling reaches every place the percent is used: the sizer's quantity
for limit orders, the trimmer's cut, and the "risk X% depo" warning on a
fresh entry. Reports, charts and journal rows keep showing the real balance
and the real shares of it — only the risk arithmetic is capped.

## Two entries, one risk budget

`RISK_PCT` is a cap on the **symbol**, not on each order. Averaging down means
two limits into one idea, and sized one at a time they risk twice:

```text
ONDO Buy @ 0.4608   131,406   stop 0.4544   risk 885.95$ = 1.00%
ONDO Buy @ 0.4668    45,108   stop 0.4544   risk 606.38$ = 0.68%
                                            ------------------
          if both fill:                   1,492.33$ = 1.68%
```

Every managed order on a symbol is sized as before, their combined stop-out
is measured, and if it overshoots the cap **all of them are scaled by one
factor**. The proportions drawn survive exactly — 45,108 against 131,406
stays 45,108 against 131,406 — because which entry should be the larger is
the trader's call and not the bot's.

One order is never scaled: sized to the cap on its own, it already fits it.

### Who gives way (`ENTRY_SPLIT`)

Scaling the drawn sizes keeps their proportions, which is fair and sometimes
useless: the live ONDO pair had the deeper order at a full 1.00% on its own,
so proportional scaling left the pre-entry a sliver of a sliver.

`ENTRY_SPLIT=25,75` divides the budget by **position in the queue**, counted
from the price outwards — a quarter to the feeler, three quarters to the zone
— whatever sizes were drawn. The pre-entry is always alive and the add always
gets the bulk.

Which order is nearest needs no call to the exchange: a buy limit rests below
the price, so the nearest is the dearest; a sell limit rests above it, so the
nearest is the cheapest.

Fewer shares than orders repeats the last one. Empty keeps the proportional
scaling. A lone order takes the whole budget either way.

Give both entries the **same stop**. With different stops the question has no
answer: once both fill the position carries one stop, and the risk is
measured from the average entry to that one.

## The drawn size as a ceiling (`RISK_GROW=0`)

By default the sizer works both ways: it cuts an order that risks more than
`RISK_PCT` and enlarges one that risks less. `RISK_GROW=0` keeps only the
cut.

The reason is what a tight stop does to the arithmetic. Notional over
deposit is `risk% / stop%`, so a 0.27% stop needs **three deposits** of
notional to put 0.84% at risk:

```text
DOGEUSDT Buy limit @ 0.09345
stop 0.0932 (0.27%) | risk 0.84% (RR 2.52 of 3)
qty 116035 (10,843$, 12.0% depo) -> 937961 (87,652$, 97.0% depo)
(capped by margin - raise the instrument's leverage)
```

That is the sizer doing its job exactly, and it is not what the hand that
drew a 10,843$ order meant by it. With `RISK_GROW=0` the order stands.

`RISK_PCT` then reads as a **ceiling rather than a target**, and the warning
follows: a target is missed in both directions, a cap only upwards.

```text
RISK_GROW=1   risk 0.10% depo, target 0.5%     <- under, and told about it
RISK_GROW=0   (nothing)                        <- under a cap is the plan
RISK_GROW=0   risk 1.00% depo, cap 0.5%        <- over, and told about it
```

## A thin setup risks less (`RR_FULL_RISK`)

`RISK_PCT` is what a stop-out may cost when the trade is worth it. The
sizer reads the reward-to-risk off the limit order's take and stop and
scales the percent down in proportion when the RR sits below
`RR_FULL_RISK` (3 by default):

```text
risk share = RISK_PCT * min(RR / RR_FULL_RISK, 1)      # 0 = always RISK_PCT
```

With `RISK_PCT=1`: an RR 3 order risks the full 1 %, an RR 1.5 order 0.5 %,
an RR 0.6 order 0.2 %. A better RR than 3 never risks more than the full
percent. An order drawn without a take has no RR to read and gets the plain
`RISK_PCT`, same as before.

The scaling happens where the limit order is sized — when you draw the
long/short in TradingView, and again if you drag the entry, stop or take
while the order still rests. Once it fills nothing chases the RR: the
trimmer keeps cutting to `RISK_PCT` only, and the fresh-entry "risk X%
depo" warning simply expects the scaled figure instead of nagging about a
position that was sized down on purpose. The ⚖️ notice names the share it
used:

```text
⚖️ ETHUSDT Buy limit @ 3000
stop 2970 (1.00%) | risk 0.50% (RR 1.50 of 3)
qty 1 (3,000$, 3.0% depo) → 15.5 (46,500$, 46.5% depo)
```

An order already within **a thousandth of its target** — or one lot step,
whichever is wider — is left alone. The target moves with equity, and equity
breathes with every open position's unrealised PnL, enough to walk it across
a rounding boundary and back. One resting order was amended 370.28 → .29 →
.28 → .29 → .28 in eleven seconds, four notices and four amends over 86
cents of a 32,000$ order, the last of them refused by Bybit for margin.

The width is relative because the wobble is: a 286,831 DOGE order walked
five units on a lot step of one, straight through a deadband a single step
wide. At `RISK_PCT=1%` a thousandth of the quantity is a thousandth of a
percent of the deposit at stake, below the resolution of every figure the
bot reports — while the order that wobbled had a real resize of +169%.

## Exit buttons on the entry notice

Every entry notice arrives with a row of buttons under the chart:

```text
[ 5% ] [ 25% ] [ 50% ] [ ❌ 100% ]
```

A tap closes that share of the position with a reduce-only market order —
partial takes without typing. The quantity is floored to the exchange's lot
step; a share smaller than the minimum order size is refused instead of
silently rounding to nothing, and a remainder too small to close later is
taken with the tap. Only `OWNER_ID` is obeyed: anyone else who can see the
chat can tap, and gets nothing. A notice with no chart behind it — a fresh
listing with no candles, or a photo Telegram refused — falls back to plain
text and carries the same buttons.

A tap also claims the auto-trimmer's cooldown before its order goes out.
The trimmer polls twice a second and Bybit needs a moment to show the
smaller size; without the claim it would read the pre-tap size and cut a
second time off a position already trimmed by hand.

The buttons live under one notice, and a notice scrolls away. `/cut <ticker>
<%>` is the same partial exit typed out, reachable at any time:

```text
/cut CL 25
✂️ CL: closing 25% (2.5) — 💸 report follows
```

## The tithe button (`TITHE_PCT`)

A close that made money arrives with one button under it:

```text
[ 🙏 десятина 61.76$ ]
```

A tap writes a single row into the journal and does nothing else:

```text
05.10.26 | десятина | USDT | … | Деп 91,445.81 | Вывел/Завел -61.76
```

**No money moves.** The Bybit key stays without withdrawal rights, as the
rest of this README assumes — the button is a ledger, not a payment. What it
buys is that the month's tithes are a column you can filter instead of
something to remember.

The kind column says `десятина`, not `вывод`. The money has not left Bybit,
and a row claiming it had would put the journal out of step with the
exchange and bury the tithes among real withdrawals.

A loss carries no button, and neither does a win whose tenth rounds to
nothing. `TITHE_PCT=0` takes it away entirely. Only `OWNER_ID` is obeyed,
like every other button. Telegram redelivers an update it was not told about
and fingers slip, so the taps already acted on are remembered: the second
tap answers with the same figure and writes nothing.

## Maker takes (`TP_MAKER=1`)

Bybit's position take-profit fires a market order — a taker fee. With
`TP_MAKER=1` the bot swaps it for a reduce-only limit resting at the same
price, which fills as a maker for less than half the fee (0.02% vs 0.055%).
The exit follows the position: the trimmer's cut resizes it, a stop-out
cancels the orphan, and every report and chart shows the take as usual.

The trade-off is real: the price can touch the level and bounce without
filling a resting limit, where the market TP would have closed. The stop is
never converted — a limit stop can miss on a wick, and a missed stop has no
ceiling, while a missed take is only a smaller win.

### Scaling out (`TP_FREE`, `TP_LADDER`)

The one limit can be a ladder. The first rung stands `TP_FREE` in R from the
entry, and its **share is the one that pays for the whole trade**: fill it
and a stop-out afterwards costs nothing.

```text
entry 100,000 - stop 99,171 - take 110,000 - 1 BTC

100,829   0.533 BTC   53%   <- sell this and the rest is free
101,658   0.116       12%
102,487   0.116       12%
103,316   0.116       12%
110,000   0.119       12%   <- the take
```

The share follows from the price:

```text
f = (R + stop*taker + entry*maker) / (|rung - stop| - rung*maker + stop*taker)
```

**Price and share are one choice, not two.** Nearer the entry needs a bigger
slice, and at the entry it would be the whole position - which is why the
distance is the setting and the share is derived. Selling 1/(k+1) at kR is
the same statement without the fees.

`TP_LADDER` is where the rest sit, also in R, splitting what the first rung
left. The position's own take always ends the ladder and carries the
rounding, so four rungs and a remainder never strand two coins above it.

A rung within **half an R of the take** is dropped: a ZEC trade whose take
sat at 3.19R put the 4R rung half a percent short of it, two orders doing one
order's work. Its share goes to the take. A rung the exchange would refuse -
below `minOrderQty` or `minNotionalValue` - is dropped the same way.

**Without a stop there is no R**, and the exit stays the single limit it has
always been. `TP_FREE=0` drops the breakeven rung; an empty `TP_LADDER`
leaves that rung and the take.

Rung prices are rounded to the instrument's **tick**, away from the
position — a long's rungs sell, so up; a short's buy, so down. Bybit snaps a
submitted price to the tick and keeps the snapped one, so a ladder compared
against what it asked for never matches what it got: 89.7622 on a tick of
0.01 came back 89.77, and one HYPE position rebuilt itself every pass for as
long as it was open. Rounding here also means the breakeven share is derived
from the price the order will really stand at.

A button, or `/cut`, takes its share **off the far end of the ladder**: the
quantity it closed is subtracted from the rung furthest from the entry, and
downwards from there if that rung is smaller than the cut. The near rungs are
left exactly where they stand — rebuilding instead would shrink every rung in
proportion, including the breakeven one, which is the rung the ladder exists
for. Taking profit early should come out of the target, not out of the
insurance.

The ladder is rebuilt only when it stops **covering the position** — after a
trim, or once a rung fills. Where each rung stands is the bot's suggestion,
not its decision: drag one to a better price and it stays there. Comparing
prices as well meant a hand-moved rung was put back within half a second,
which made the ladder impossible to adjust at all.

Cancel the exits to get the computed prices back.

### How loud it speaks (`SPEAK_VOLUME`)

Empty, the bot plays at whatever the Cast device is set to — and that is not
a dial anyone is really holding. A Nest keeps its **media** volume apart from
the assistant volume the buttons reach when nothing is playing, so turning it
down by hand moves the wrong one, and on one account something kept putting
the media volume back to 40%.

Over one evening the level wandered through 0.4, 0.15, 0.4, 0.3, 0.05 and
0.7 with neither the bot nor its owner moving it.

`SPEAK_VOLUME=15` applies the level before each clip and **leaves it there**.
It is not restored afterwards: restoring hands the dial straight back to
whatever moves it next, and the point is that the speaker stays where it was
put. The cost is worth knowing — whatever plays after an announcement plays
at this level too, until something else sets it.

Note that `SPEAK_HOURS` silences **alerts** outside its window but
`SPEAK_LIFECYCLE` ignores it — "Relay up" and "Relay down" are spoken at any
hour. `SPEAK_LIFECYCLE=0` is what stops a restart talking at night.

## Alert relaying

Compacts LEXX Draco alerts and re-posts them through your own bot.

```text
🔔 #OPUSDT OPUSDT, Crossing 0.10282
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
placing a grid on OP
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
(`🟢 bybit-tv-sizer up …` / `🔴 bybit-tv-sizer down — SIGTERM, uptime 3h12m`). SIGTERM and
SIGINT are handled: the relay says goodbye, disconnects and exits 0. Silence the
notices with `NOTIFY_LIFECYCLE=0`, rename them with `RELAY_NAME`.

## Keep it running after reboot

```bash
cp com.bybit.tv-sizer.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.bybit.tv-sizer.plist
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
launchctl print gui/$(id -u)/com.bybit.tv-sizer | grep -E 'state|pid|last exit'
tail -f ~/Library/Logs/bybit-tv-sizer-launchd.log
docker logs -f bybit-tv-sizer
```

Stop the relay with `docker compose down` (the agent will bring it back next
login); retire the agent with `launchctl bootout gui/$(id -u)/com.bybit.tv-sizer`
followed by removing the plist from `~/Library/LaunchAgents`.

`reboot.sh` says "Rebooting" on the Nest (through the running container's
speech stack) and then restarts the machine. Alias it so `reboot` and
`sudo reboot` both go through it — the trailing space in the `sudo` alias
makes zsh alias-expand the word that follows:

```bash
alias sudo='sudo '
alias reboot='/Users/me/bybit-tv-sizer/reboot.sh'
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
forever and the only evidence is `docker logs bybit-tv-sizer` growing. Two
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

The script answers an appended trade with its row number and sheet id, and
the close notice turns the equity figure into a link straight at that row —
`JOURNAL_URL` is what it is built from. A sheet that refuses the write costs
the link and nothing else; the notice still goes out.

Rows are reset to the default format as they are appended. `appendRow`
inherits the format of the row above, and the ИТОГ НЕДЕЛИ banner is
white-on-green: without the reset, every trade booked after a weekly summary
came out white on white — the values present, invisible, with only column P
readable because the banner never painted that far.

The sheet is written in Russian. Its headers are Russian and it is read as
one document, so the cells match: `Лонг` / `Шорт`, `стоп` / `тейк` / `руками`
/ `риск-гард`, `ввод` / `вывод`, and the `ИТОГО` / `ИТОГ НЕДЕЛИ` banners the
Apps Script writes. This is the one place the repo is not English — Telegram
notices, logs and code stay as they are, and the translation happens in
`_ru()` at the edge, where a row is built for the sheet and nowhere else.

### One row per position, not per closing order

A position trimmed with `/cut` (or by `RISK_TRIM`) is closed in pieces, and
Bybit files one closed-pnl record per piece. The journal gets one row for the
position: at the close every piece is collected and merged, so PnL, fees and
volume are the trade's. Reporting the last piece alone once booked 30.81 of a
trade that made 116.73, and the six slices before it were never journalled at
all.

Bybit offers nothing to group the pieces by — a record's `createdTime` is its
closing order's, not the position's. What they share is the position's
average entry price to the last decimal, so the run of records carrying it is
the position. Closing a position and opening another at the very same average
entry would merge the two; at eight decimals that is a curiosity, and the
cost is one merged row.

Each trim still announces what it realised, hanging the figure off the resize
notice — `/cut` says "💸 report follows" and this is what follows. The
cumulative close notice says how many closes it covers, so the two cannot be
read as the same money twice.

### RR and R come from the entry, not the exit

The R multiple is PnL over the risk the **entry** stop carried, and the RR is
read off the entry stop and take. Both are remembered when the position
opens. Measuring them against the levels standing at the close made a stop
dragged to breakeven report a trade as 2.34R at RR 1:50.4 — honestly 0.31R at
1:2.2, on a stop worth 13$ instead of 378$.

A relay restarted mid-trade never saw the entry, so both cells stay empty.
Empty says the data is gone; a number computed from a trailed stop says
something false and looks like a result.

**Without a stop there is no R, and the cell stays empty** — it used to fall
back to net USDT, which put dollars in an R column and made the `ИТОГО` and
`ИТОГ НЕДЕЛИ` totals add R to money. The money is in the PnL column already.

The banner wording is load-bearing, not decoration: `weekSummary_` finds the
start of the current week by scanning column A for the previous banner, and
the `ИТОГО` formulas exclude those rows with `"<>ИТОГ*"`. Rename the banner
without renaming the scan and every weekly total silently sums the whole
journal from row 3.

The script itself lives in the spreadsheet, not in this repo; the reference
copy is in [docs/sheets-script.js](docs/sheets-script.js). It handles four
payloads: `{row: [...]}` appends a trade (with an optional base64 `png`
saved to the Drive folder `trade-screens` and chipped into column G, and an
optional `png_entry` chipped into column Q), `{week: true}` appends the
weekly total, `{headers: [...]}` rewrites row 1, `{totals: true}` builds the
frozen running-total row 2.

## Your own TradingView chart, everywhere

Every picture the bot sends is the chart **you** were reading — your layout,
your levels, your annotations — not a drawing of the same candles. That
covers the entry notice, the close notice, both journal columns (the pane as
the position opened in Q, as it closed in G), `/positions` and the BTC/ETH
snapshot on `/status`.

The bot still draws its own chart from Bybit klines, and always will: it
works in a slim container with no browser, and it is what goes out when the
layout cannot be reached. Backup, not first choice.

The equity curve on `/statistics` stays the bot's own drawing — there is no
pane of your account history to shoot.

**What it costs:** a warm shot is 8-12 seconds and a symbol the layout has
to be driven to about twenty, so a notice or a command waits that long for
its picture. `/positions` asks one symbol at a time — there is one browser —
and stops asking after the first refusal, because a sleeping Mac says no to
the fourth symbol exactly as slowly as to the first.

A container cannot take that picture: it needs a browser holding a real
TradingView session. So a small service runs on the Mac instead
([tv_capture.py](tv_capture.py)), owns a dedicated Chrome profile, and
answers exactly one question — "a PNG of symbol X".

### Setting it up

1. **A Chrome profile of its own**, never your everyday one and never your
   working window:

   ```bash
   mkdir -p ~/.bybit-tv-sizer/tv-profile && chmod 700 ~/.bybit-tv-sizer/tv-profile
   "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
       --user-data-dir=~/.bybit-tv-sizer/tv-profile \
       --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1 \
       https://www.tradingview.com/#signin
   ```

   Sign in, dismiss the cookie banner once, open the layout you trade from.
   That window can be closed afterwards — the session stays in the profile,
   and the service starts Chrome headless on it when nothing is up.

2. **The secret and the layout** in `bipboop`: `TV_SHOT_SECRET` (long and
   random, the same string on both sides) and `TV_SHOT_LAYOUT`, the URL of
   the saved layout.

3. **The service**, under launchd beside the relay:

   ```bash
   cp com.bybit.tv-capture.plist ~/Library/LaunchAgents/
   launchctl load ~/Library/LaunchAgents/com.bybit.tv-capture.plist
   ```

   `capture-start.sh` builds its own virtualenv from
   [requirements-capture.txt](requirements-capture.txt) on first run. There
   is no `playwright install`: it drives the Chrome already on the Mac,
   which is the whole point — a bundled browser would have no session in it.

4. **Rebuild the relay** so it ships the client module:
   `docker compose up -d --build`. `--force-recreate` alone reuses the old
   image and silently runs the old code.

### What it will not do

- **It never proxies the DevTools port.** Chrome's debugging port is the
  blunt door behind this one: whoever reaches it drives the whole browser
  and every session in the profile. It stays bound to `127.0.0.1`, the
  service refuses anything but its own secret path, and off that path it
  hangs up without a byte — the same posture as the alert webhook.
- **It never listens on the LAN.** The default bind is loopback. Docker
  Desktop forwards `host.docker.internal` to the host's own loopback, so the
  container reaches it without the port ever being offered to the network.
- **It never edits your layout.** A symbol your grid does not hold is loaded
  through a URL parameter, which overrides the active pane for that one tab;
  TradingView autosaves layout *edits*, and driving a pane by clicking and
  typing would quietly rewrite your own grid. The saved layout comes back
  untouched on the next plain load.
- **It never attaches the wrong symbol.** Panes are matched on the exact
  ticker, never a prefix, and the match is re-checked after the window is
  resized and the grid reflows.

### When there is no frame

Degradation is the feature, not an afterthought. A profile signed out, a Mac
asleep, a service not running, a symbol TradingView will not show, a shot
slower than `TV_SHOT_WAIT` — each ends the same way: **the picture is the
chart the bot drew, and the notice says which of those it was**, on a line
of its own:

```text
💸<b>+512.30</b>=<b>10,000.00$</b>(+5.12%)·📈FARTCOIN·manual
📷 the TradingView profile is signed out
```

The entry notice, `/positions` and `/status` carry the same line when the
layout would not answer them. Nothing is ever attached blank, nothing is
ever attached silently wrong, and nothing ever downgrades in silence.

### Things worth knowing

- **The frame is your view, zoom and all.** It shoots the pane as the layout
  sits — if that pane is a daily chart scrolled across three years, that is
  what the journal gets. Changing what you look at changes what is recorded.
- **A symbol outside the grid has no "your view" to inherit**, so it comes
  back on whatever timeframe TradingView last showed you for it — easily a
  weekly chart of a five-minute trade. `TV_SHOT_INTERVAL=15` pins those.
- **The viewport is derived, not fixed.** The service measures the pane grid
  and sizes the window so one pane lands on `TV_SHOT_WIDTH`x`TV_SHOT_HEIGHT`
  — a 4x4 grid and a 5x1 of tall columns need very different windows, and a
  window chosen for the wrong one yields a portrait strip with the candles
  crushed into it.
- **Entry frames wait on disk** in `TV_SHOT_DIR`, inside the session volume,
  because trades stay open across the rebuilds that a code change needs. A
  frame is consumed when its trade closes; a trade opened before this was
  switched on simply has none.

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
