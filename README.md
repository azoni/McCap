# McCap

Discord bot for Solana market-cap alerts and token watchlists.

## Commands

### Alerts

| Command | What it does |
|---|---|
| `/mc <ca> <target> [note]` | Alert when market cap hits a target. Accepts **`2x`**, **`+50%`**, **`-30%`** or an absolute (`250k`, `2.5m`). Direction (≥ / ≤) is inferred. |
| `/mc_move <ca> <percent> [window] [direction] [cooldown]` | Momentum alert — fires when a token **moves** X% within a window (`15m`, `1h`, `4h`, `1d`). Recurring. |
| `/mc_list [user] [public]` | All active alerts, level and momentum. |
| `/mc_remove <alerts>` | Remove alerts. Autocompletes; accepts ids (`a1b2c3`) or `/mc_list` positions. |
| `/mc_recent [count] [user]` | Recently fired alerts. |
| `/mc_status` | Polling tiers, request rate, and which momentum alerts are still filling their window. |
| `/mc_lp <ca>` | Best LP venue across Meteora / Raydium / Pumpswap. |
| `/mc_check <ca>` | Holder count, top-10 concentration, mint/freeze authority, dev mints. |

### Watchlists

| Command | What it does |
|---|---|
| `/watch add <ca> [list]` | Add a token to a named list (default: `default`). |
| `/watch remove <ca> [list]` | Remove a token. |
| `/watch view [list]` | One table: MC, 24h change, liquidity — sorted by biggest mover. |
| `/watch lists` | Every watchlist in the server. |

Watchlists are read-on-demand: tokens are fetched only when someone runs
`/watch view`, so a long list costs nothing in the background.

### Scan watching (Rick and other scanner bots)

Off by default. When enabled, McCap notices when a scanner bot posts a token
scan, records the market cap at that moment, and afterwards tracks what the
token actually did.

| Command | What it does |
|---|---|
| `/scans report [hours] [sort]` | Were the calls any good? Ranks scanned tokens by peak or current gain, with a median and a 2x hit count. |
| `/scans recent [count]` | Most recent detections. |
| `/scans status` | Whether detection is actually working, and why not if it isn't. |

**Enabling it — order matters.** Turn on *Message Content Intent* under
**Bot → Privileged Gateway Intents** in the Discord Developer Portal **first**,
then set `SCAN_WATCH_ENABLE=1`. No Discord approval is needed for this intent,
but requesting it before the portal grants it makes Discord refuse the
connection entirely (close code 4014). `main.py` catches that and starts without
the intent rather than staying offline, so a mis-set flag costs you scan
detection, not the whole bot.

Setting `SCANNER_BOT_IDS` to Rick's bot user id is strongly recommended. Left
blank, McCap treats *any* bot posting a resolvable mint as a scanner.

On detection it can add the token to a `scans` watchlist, reply with its own
consensus numbers, and auto-arm a momentum alert. Auto-armed alerts are capped
(`SCAN_AUTO_MOVE_MAX`) and expire (`SCAN_AUTO_MOVE_TTL`) because every armed
alert costs polling — without both, a busy scan channel would quietly eat the
whole request budget.

Two things worth knowing. Detection reads messages in the channels it watches,
which is what the privileged intent is for — `SCAN_CHANNEL_IDS` narrows it to
specific channels. And it depends on another bot's undocumented message format,
so the parser sweeps every surface of a message (content, all embed text, and
button URLs) rather than one field, and **logs a warning when a scanner posts
something it can't extract a mint from** — a format change shows up as a log
line instead of silence.

### Trading (Robinhood Crypto)

**Off by default.** These place real orders against a real brokerage account.

| Command | What it does |
|---|---|
| `/rh_balance` | Buying power, holdings, and today's spend against the cap. |
| `/rh_quote <symbol>` | Best bid/ask for a pair like `BTC-USD`. |
| `/rh_buy <symbol> <usd>` | Buy a dollar amount. Asks to confirm before executing. |
| `/rh_sell <symbol> <qty>` | Sell a quantity. Asks to confirm before executing. |
| `/rh_orders` | Recent orders and their state. |
| `/rh_trending [window] [count]` | Biggest movers among the coins Robinhood lists, by 1h / 24h / 7d. |

All except `/rh_trending` are ephemeral — balances and orders are never posted
to a channel. `/rh_trending` is public market data and needs no credentials or
owner check, so anyone can run it.

Robinhood's API has no trending or movers endpoint — it is execution-only. So
`/rh_trending` takes the tradeable pair list from Robinhood (authoritative when
credentials are set, a built-in approximation otherwise, and the footer says
which) and the price movement from CoinGecko, which is free and keyless.

**Setup.** Generate a keypair, register the public half, then set four variables:

```bash
python scripts/generate_rh_keypair.py     # run locally, not on Railway

printf %s "$RH_KEY" | railway variable set RH_API_KEY --stdin -s mccap --skip-deploys
printf %s "$RH_PRIV" | railway variable set RH_PRIVATE_KEY_B64 --stdin -s mccap --skip-deploys
railway variable set RH_OWNER_ID=<your discord user id> RH_TRADING_ENABLE=1 -s mccap
```

Pipe the secrets through stdin so neither the API key nor the signing key lands
in your shell history.

US-only, and it needs an active Robinhood Crypto account.

**The guards, and why each exists:**

- **`RH_OWNER_ID` gates every command.** McCap runs in shared servers. Without an
  owner check any member could spend the account holder's money — so an unset
  owner blocks *everyone*, and is never read as "anyone".
- **Confirmation button** on every buy and sell, bound to the owner's user id so
  nobody else can press it. It expires after `RH_CONFIRM_TIMEOUT`.
- **Per-trade and daily dollar caps**, checked before the order is built *and
  re-checked after confirmation* — a button can sit unclicked while other orders
  land.
- **The daily ledger is on the volume**, not in memory. McCap redeploys several
  times a day, and a cap that resets on restart is not a cap.
- **Order sizing rounds down.** Rounding up would breach the very cap the amount
  was just checked against.
- Spend is recorded **only after** Robinhood accepts the order.

Note Robinhood lists roughly 15–30 mainstream coins. None of the Solana
memecoins McCap alerts on are tradeable there — those exist only on DEXes.

### Where commands work

McCap is **user-installable** — install it to your account and the read-only
commands work anywhere, including DMs and servers the bot isn't in.

| Works anywhere | Server-only |
|---|---|
| `/mc_list`, `/mc_recent`, `/mc_status`, `/mc_lp`, all of `/watch` | `/mc`, `/mc_move`, `/mc_remove` |

Alert *creation* stays server-only for a structural reason: an alert fires
minutes or days later, and a bot can only post unprompted into a channel it is
actually in. A `/mc` alert set in a server McCap isn't a member of could never
be delivered, so the command isn't offered there.

Outside a server there is no guild, so scoping switches from "this server's
alerts" to "your alerts, across every server", and watchlists become personal
rather than shared. Both are keyed on the caller — guildless records are *not*
pooled under a shared id.

### Holder and risk context

Alerts carry a second line beyond the market cap:

```
🔎 756 holders · ⚠️ top 10 hold 65% · dev minted 25 · organic: low
```

That comes from **Jupiter's token API** — free, keyless, and batched: up to 100
mints resolve in one request, so the entire watchlist costs a single call every
`JUPITER_REFRESH_SECONDS`. `/mc_check <ca>` shows the same data on demand.

It doubles as a **market-cap fallback**. DexScreener stops returning pairs for
tokens whose pools thin out, which had left a third of this bot's alerts unable
to fire at all. Jupiter still reports a market cap for most of them — measured on
the live alert set, 9 tokens went from "no data" to fireable. DexScreener's
consensus market cap stays primary; Jupiter is only consulted when it comes back
empty, and `/mc_list` labels the source.

Everything here is best-effort: a Jupiter outage costs you the context line, never
the alert. Concentration and dev-mint counts are third-party measurements, not
verdicts — the embed says so.

## Alert types

**Level alerts** (`/mc`) fire once and are consumed. Relative targets are
resolved at creation, so `/mc <ca> 2x` on a $1.2M token becomes a fixed $2.4M
target — `/mc_list` still shows `2x` so the intent stays readable.

**Momentum alerts** (`/mc_move`) don't know their trigger price in advance. They
compare the current market cap to a baseline from the past, and re-arm after
firing rather than being consumed (cooldown defaults to 30m, so a volatile token
can't ping on every tick).

They need history before they can trigger, which is why the bot **backfills from
GeckoTerminal** — one call per token, on startup and whenever an alert is
created. In practice that means an alert is armed immediately rather than blind
for half its window. `/mc_status` shows any that are still warming up (a token
with no GeckoTerminal pool falls back to collecting samples live).

Candles are prices, not market caps, so each close is scaled against the live
market cap from DexScreener's consensus — supply is effectively constant over an
alert window.

Live history is intentionally **not** persisted across restarts. A stale
pre-restart baseline would report the bot's downtime as a price "move" and fire
spuriously; refetching real candles is both correct and cheap.

## Local development

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements-dev.txt   # source .venv/bin/activate on macOS/Linux
cp .env.example .env                                # then fill in MCCAP_TOKEN
.venv/Scripts/python main.py
```

```bash
pytest -q
```

## Deploying on Railway

`railway.json` selects the `Dockerfile` builder and sets `restartPolicyType: ALWAYS`.

```bash
railway init --name mccap
railway add --service mccap --repo azoni/McCap --branch main
railway service mccap                     # link the dir to the service
railway volume add -m /data               # REQUIRED, see below
```

Then set the variables, piping secrets through stdin so they stay out of shell history:

```bash
printf %s "$TOKEN" | railway variable set MCCAP_TOKEN --stdin -s mccap --skip-deploys
railway variable set DATA_DIR=/data LOG_LEVEL=INFO -s mccap
```

**The volume is not optional.** Railway's filesystem is ephemeral, so without one
every deploy starts from an empty alert list. `DATA_DIR` defaults to `/data` in
the Dockerfile.

**Keep one replica.** Two instances means every alert posts twice.

State files on the volume: `reminders.json`, `moves.json`, `watchlists.json`,
`alerts.json`, `scans.json`. To carry data over:

```bash
railway volume files -v mccap-volume upload ./reminders.json reminders.json
```

Records written by older versions are missing newer fields (stable ids, relative
target specs); the loader backfills them on first read, so no manual migration
is needed.

Two CLI quirks worth knowing: `railway volume add` panics with an
`Option::unwrap()` error *after* successfully creating the volume, so check
`railway volume list` rather than trusting the exit code — and run it from
PowerShell, since Git Bash rewrites `/data` into a Windows path.

## How polling works

Every tracked token costs one DexScreener request per refresh, against a limit of
about 300 requests/minute. Level alerts are graded by how close they are to
firing; momentum alerts need a steady sample rate fine enough to measure their
window. Each token takes the shortest interval anything asks of it, so several
alerts on one token share a single request.

| Tier | Condition | Default |
|---|---|---|
| 🔥 hot | within 15% of target | 10s |
| 🌤 warm | within 2× of target | 60s |
| 🧊 cold | further away | 300s |
| ❔ unknown | no MC reported yet | 120s |
| 📊 momentum | window ÷ 12, floored at 30s | — |

A client-side token bucket (`DEX_MAX_REQUESTS_PER_MIN`, plus a smaller
`DEX_BURST`) is the final backstop; burst is deliberately below the sustained
rate so a cold start can't exceed the limit inside a rolling 60s window.

Tokens are fetched one address per request on purpose: DexScreener's
multi-address form caps the response at 30 *pairs total* across the whole batch,
so a token with many pools starves the others and they come back with no market
cap at all.

## Data sources

| Source | Used for | Key | Limit |
|---|---|---|---|
| DexScreener | live MC, liquidity, 24h change, pairs | none | ~300 req/min |
| GeckoTerminal | historical OHLCV to seed momentum alerts | none | ~30 req/min |

They cross-validate: on a spot check GeckoTerminal's 1h candles over 23h gave
BONK +8.79% against DexScreener's +9.53% h24. GeckoTerminal has its own rate
limiter, deliberately separate so backfill can never starve the alert watcher.

## Data quality notes

Market cap, liquidity and 24h change are each resolved differently, because no
single pool is trustworthy for all three:

- **Market cap** — median across pools, with log-space IQR outlier rejection.
- **Liquidity / volume** — summed across every pool where the token is the base asset.
- **24h change** — median across pools quoted in a *major* asset (SOL, USDC, …).
  Liquidity alone is not a safe filter: BONK's single deepest pool is quoted in an
  obscure token and reports +542,339%, while its SOL and USDC pools all agree on ~11.6%.
