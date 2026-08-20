# McCap

Discord bot for Solana market-cap alerts, launchpad graduation reports, and Solana Pay invoices.

## Commands

| Command | What it does |
|---|---|
| `/mc <ca> <target> [note]` | Set a market-cap alert. Direction (≥ / ≤) is inferred from the current MC. |
| `/mc_list [user] [public]` | List this server's active alerts with cached current MC. |
| `/mc_remove <alerts>` | Remove alerts. Autocompletes; accepts ids (`a1b2c3`) or `/mc_list` positions. |
| `/mc_recent [count] [user]` | Show recently fired alerts. |
| `/mc_status` | Polling tiers and the current outgoing request rate. |
| `/mc_lp <ca>` | Suggest the best LP venue across Meteora / Raydium / Pumpswap. |
| `/graduated_report [hours]` | Tokens that graduated in the last N hours. |
| `/pay <amount> [asset] [note]` | Create a Solana Pay invoice (SOL or USDC) with a QR code. |
| `/pay_status [invoice_id]` | Check an invoice. |
| `/pay_list [scope] [status]` | List invoices; `server` scope needs Manage Server. |

A background watcher posts an alert embed in the channel where it was created and
mentions the person who set it. Fired alerts are removed and recorded in history.

## Local development

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements-dev.txt   # source .venv/bin/activate on macOS/Linux
cp .env.example .env                                # then fill in MCCAP_TOKEN
.venv/Scripts/python main.py
```

Run the tests:

```bash
pytest -q
```

## Deploying on Railway

`railway.json` selects the `Dockerfile` builder and sets `restartPolicyType: ALWAYS`.

```bash
railway init --name mccap                       # create the project
railway add --service mccap --repo azoni/McCap --branch main
railway volume add -m /data -s mccap            # REQUIRED, see below
```

Then set the variables. Pipe secrets through stdin so they never land in your
shell history:

```bash
printf %s "$TOKEN" | railway variable set MCCAP_TOKEN --stdin -s mccap --skip-deploys
railway variable set DATA_DIR=/data LOG_LEVEL=INFO -s mccap
```

`.env.example` lists everything that's read. Only `MCCAP_TOKEN` is strictly
required (`DISCORD_TOKEN` works as an alias).

**The volume is not optional.** Railway's filesystem is ephemeral, so without one
every deploy starts from an empty alert list. `DATA_DIR` already defaults to
`/data` in the Dockerfile.

**Keep one replica.** Two instances means every alert posts twice.

To carry existing alerts over, upload the JSON files into the volume before first
boot:

```bash
railway volume files upload -v <volume> reminders.json alerts.json payments.json
```

Records written by older versions are missing the stable alert `id`; the loader
backfills those on first read and rewrites the file, so no manual migration is needed.

## How alert polling works

Every tracked token costs one DexScreener request per refresh, and that endpoint
allows about 300 requests/minute. The bot grades each alert by how close the token
is to its target and polls accordingly:

| Tier | Condition | Default interval |
|---|---|---|
| 🔥 hot | within 15% of target | 10s |
| 🌤 warm | within 2× of target | 60s |
| 🧊 cold | further away | 300s |
| ❔ unknown | no MC reported yet | 120s |

Several alerts on the same token share one request, at the shortest interval any of
them asks for. A client-side token bucket (`DEX_MAX_REQUESTS_PER_MIN`) is the final
backstop. `/mc_status` shows the live tier breakdown and estimated request rate.

Tokens are fetched one address per request on purpose: DexScreener's multi-address
form caps the response at 30 *pairs total* across the whole batch, so a token with
many pools starves the others and they come back with no market cap at all.

## Graduation streams

The BONK / BAGS / HEAVEN feeds are Bitquery websocket subscriptions and are the
only source for the Bags and Heaven sections of the report. They are off by
default to keep streaming quota opt-in — set `GRAD_STREAM_ENABLE=1` (and
`GRAD_STREAM_POST=1` to also post each graduation live as it happens).
