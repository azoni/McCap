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

### Watchlists

| Command | What it does |
|---|---|
| `/watch add <ca> [list]` | Add a token to a named list (default: `default`). |
| `/watch remove <ca> [list]` | Remove a token. |
| `/watch view [list]` | One table: MC, 24h change, liquidity — sorted by biggest mover. |
| `/watch lists` | Every watchlist in the server. |

Watchlists are read-on-demand: tokens are fetched only when someone runs
`/watch view`, so a long list costs nothing in the background.

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
`alerts.json`. To carry data over:

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
