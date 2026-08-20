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

## Alert types

**Level alerts** (`/mc`) fire once and are consumed. Relative targets are
resolved at creation, so `/mc <ca> 2x` on a $1.2M token becomes a fixed $2.4M
target — `/mc_list` still shows `2x` so the intent stays readable.

**Momentum alerts** (`/mc_move`) don't know their trigger price in advance. They
compare the current market cap to a baseline from the past, and re-arm after
firing rather than being consumed. Two consequences worth knowing:

- They need history before they can trigger. A `1h` alert stays quiet for
  roughly the first 30 minutes. `/mc_status` shows how many are still warming up.
- Each has a cooldown (default 30m) so a volatile token can't ping on every tick.

History is intentionally **not** persisted across restarts. A stale pre-restart
baseline would report the bot's downtime as a price "move" and fire spuriously,
so after a deploy momentum alerts simply refill their window.

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

## Data quality notes

Market cap, liquidity and 24h change are each resolved differently, because no
single pool is trustworthy for all three:

- **Market cap** — median across pools, with log-space IQR outlier rejection.
- **Liquidity / volume** — summed across every pool where the token is the base asset.
- **24h change** — median across pools quoted in a *major* asset (SOL, USDC, …).
  Liquidity alone is not a safe filter: BONK's single deepest pool is quoted in an
  obscure token and reports +542,339%, while its SOL and USDC pools all agree on ~11.6%.
