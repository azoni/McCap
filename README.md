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

### Robinhood Chain wallets and trading (`/rhc`)

**Off by default. Custodial. Real money.** McCap generates one Robinhood Chain
(chain id 4663) wallet per Discord user, keeps the key encrypted on the volume,
and swaps on that user's behalf after they press a confirm button. Whoever runs
the host controls every wallet; the wallet-creation reply says so. Keep only
what you are actively trading here.

| Command | What it does |
|---|---|
| `/rhc wallet create` | Generate your wallet. Fund it by withdrawing ETH on Robinhood Chain from the Robinhood app. |
| `/rhc wallet show` | Address, ETH balance, today's remaining buy budget. |
| `/rhc wallet export` | Reveal your private key (ephemeral, confirm first, logged). |
| `/rhc wallet withdraw <to> <eth>` | Send ETH out. Confirm first. |
| `/rhc buy <token> <eth> [slippage_bps]` | Quote, honeypot check, confirm, swap, receipt. Counts against your daily cap. |
| `/rhc sell <token> <percent> [slippage_bps]` | Sell part of a holding for ETH. Exits are never capped. |
| `/rhc holdings` | ETH and every token you have traded here, with rough USD values. |
| `/rhc trending [window] [sort] [count] [include_majors]` | Busiest and fastest-moving tokens on the chain: volume, market cap at the start of the window → now, liquidity. Windows 5m to 24h; sort by volume, gainers, losers or newest. |
| `/rhc new [count] [min_liquidity]` | Brand-new pairs from GeckoTerminal's new-pools feed, newest first, with age, liquidity, 1h volume and change. |
| `/help` | Every command with what it does. |

The bot's status line and its About Me (click McCap) show the combined total
across all wallets: ETH, tokens traded through McCap, and a rough dollar value,
refreshed every `PRESENCE_REFRESH_SECONDS`. Per-person figures stay behind
`/rhc holdings`. `RHC_ABOUT_ME_ENABLE=0` leaves the profile text alone.

`<token>` is a contract address or a symbol from `/rhc trending`. Quotes, trade
results, wallet addresses, balances and holdings post to the channel so the
group can see them (`RHC_PUBLIC_REPLIES=0` makes everything private). Confirm
prompts, refusals and the private-key export are only ever visible to the user.

**Routing.** Swaps go through the KyberSwap aggregator, which sees every DEX on
the chain (Uniswap V2/V3/V4, Ramses, Pons, ...) and builds the calldata itself.
Uniswap V3 alone sees about a tenth of the chain's volume, and new pairs launch
on V2, so a single router was never going to give good fills. It also keeps us
off the Robinhood-forked Uniswap V4 router, which breaks stock SDK encodings.

**Why so many checks.** Robinhood Chain has live drainers squatting Uniswap's
canonical mainnet addresses: 2,109-byte stubs that accept ETH and return
success. So before anything is signed: the RPC must report chain 4663; the
router must be the pinned canonical KyberSwap address (an address in an API
response is never trusted), not on the denylist, and carry real code; the
wallet must hold gas; sells approve exactly the trade's amount, never
unlimited; and the exact calldata is simulated with `eth_call` and must return
an ABI-encoded amount at or above the slippage floor. A drainer returns empty
bytes there. Measured 2026-09-07: the real router simulated within 0.03% of the
quote. One transaction at a time per wallet; a receipt timeout is reported as
*pending* with the hash, never as a failure that invites a double spend.

**Setup**

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"      # a wallet secret; back it up offline
printf %s "$SECRET" | railway variable set RHC_WALLET_SECRET --stdin -s mccap --skip-deploys
railway variable set RHC_TRADER_IDS=<discord ids, comma separated> RHC_TRADING_ENABLE=1 -s mccap
```

New slash commands
are synced globally at startup and can take up to an hour to show in Discord
clients; DM the bot `!sync` (owner only) or restart the Discord client to hurry it.

Losing `RHC_WALLET_SECRET` or `rhc_wallets.json` loses every wallet. Back both
up before the first deposit (`railway volume files -v mccap-volume download
rhc_wallets.json ./rhc_wallets.backup.json`, then check it decrypts). To rotate
the secret without locking everyone out, run `scripts/rhc_rekey.py` locally
(`RHC_OLD_SECRET=... RHC_NEW_SECRET=... python scripts/rhc_rekey.py rhc_wallets.json rhc_wallets.rekeyed.json`):
it re-encrypts every record under the new secret and verifies each key still
derives its address before writing anything. Upload the result over
`rhc_wallets.json` and set the new secret in the same deploy.

Two more things the code does that are worth knowing. Every broadcast is
journaled before its receipt is awaited, and on startup the journal rebuilds the
list of unresolved transactions, so a redeploy mid-trade cannot forget that a
wallet has money in flight; that wallet refuses new trades until the transaction
is found or `RHC_PENDING_BLOCK_SECONDS` (900) passes, at which point a buy's
budget reservation is returned. And the dollar figure checked against the caps
is the LARGER of KyberSwap's `amountInUsd` and the ETH amount times DexScreener's
ETH price, so one wrong feed cannot shrink a trade under the cap. Optional: `RHC_GUILD_IDS` (server allowlist),
`RHC_RPC_URLS` (add a paid endpoint such as QuickNode or Alchemy first in the
list; the public RPC is rate limited), `RHC_MAX_TRADE_USD` (50) and
`RHC_MAX_DAILY_USD` (200) per user, `RHC_DEFAULT_SLIPPAGE_BPS` (200),
`RHC_MAX_SLIPPAGE_BPS` (1000).

Start with one allowlisted user and a few dollars, and compare realised fills
against quotes before opening it up. The design notes and the decisions behind
them are in `docs/rh-chain-trading-plan.md`.

### Chat (talk to McCap)

@mention McCap, or DM it, and it answers through the Claude API. It has two kinds
of memory, both on the data volume:

- a rolling conversation per channel (`CHAT_HISTORY_TURNS`, default 30), so a
  redeploy does not lose the thread; and
- long-term **notes** per server: tell it to remember something, describe a
  feature you want built, make a decision, and it saves a note it reads on every
  later request. Ask "what's on the list" and it answers from those notes.

It can read the live alert list and look tokens up (`what's RSTR at`), so those
answers carry real numbers. It does **not** create alerts; it points at `/mc`.

| Command | What it does |
|---|---|
| `/memory view` | The notes it keeps for this server (or your DM). |
| `/memory forget <id>` | Delete one note. |
| `/memory status` | Model in use and today's call count. |

Setup: `ANTHROPIC_API_KEY` turns it on (`CHAT_ENABLE=0` turns it off again).
`CHAT_MODEL` defaults to `claude-haiku-4-5`, the cheap tier. Two spend guards,
because every mention in a shared server is a paid call: `CHAT_DAILY_CAP`
(default 300 calls/day) and `CHAT_USER_COOLDOWN_SECONDS` (default 3). Both are
in memory and reset on redeploy. Notes are
scoped per server, and private per DM.

```bash
printf %s "$KEY" | railway variable set ANTHROPIC_API_KEY --stdin -s mccap
```

No privileged intent is needed: Discord delivers message content for DMs and
for messages that mention the bot.

### Where commands work

McCap is **user-installable** — install it to your account and the read-only
commands work anywhere, including DMs and servers the bot isn't in.

| Works anywhere | Server-only |
|---|---|
| `/help`, `/mc_list`, `/mc_recent`, `/mc_status`, `/mc_check`, `/mc_lp`, all of `/watch`, `/memory` | `/mc`, `/mc_move`, `/mc_remove`, `/scans`, all of `/rhc` (server-installed; also usable in a DM with the bot) |

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
`alerts.json`, `scans.json`, `chat_memory.json`, `chat_history.json`,
`rhc_wallets.json` (encrypted keys; back it up), `rhc_ledger.json`, `rhc_trades.json`.
The volume must be mounted at `DATA_DIR` itself: the image sets
`RHC_REQUIRE_MOUNTED_DATA_DIR=1`, and wallet creation refuses when `DATA_DIR` is
not a mount point (keys minted onto ephemeral disk would vanish on the next deploy). To carry data over:

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
| GeckoTerminal | historical OHLCV to seed momentum alerts; Robinhood-chain pools for `/rhc trending` and `/rhc new` | none | ~30 req/min |
| Claude API | chat replies (`ANTHROPIC_API_KEY`) | yes | per account |
| KyberSwap Aggregator API | routes and swap calldata on Robinhood Chain | none (`X-Client-Id`) | unpublished; kept under 30/min |
| Robinhood Chain RPC | balances, simulation, broadcasting | none (public) or a paid provider | public endpoint is rate limited |

They cross-validate: on a spot check GeckoTerminal's 1h candles over 23h gave
BONK +8.79% against DexScreener's +9.53% h24. GeckoTerminal has its own rate
limiter, deliberately separate so backfill can never starve the alert watcher.

## Data quality notes

Market cap, liquidity and 24h change are each resolved differently, because no
single pool is trustworthy for all three:

- **Market cap** — pools under 1% of the deepest pool's liquidity are ignored,
  log-space IQR outliers are rejected, then the *liquidity-weighted* median. A
  plain median fails as soon as most pools are dust: RSTR had 23 pools, two funded
  ones agreeing on ~$2.6M and twenty-one sub-$1K pools with stale prices, and the
  plain median reported $1.04M.
- **Liquidity / volume** — summed across every pool where the token is the base asset.
- **24h change** — median across pools quoted in a *major* asset (SOL, USDC, …).
  Liquidity alone is not a safe filter: BONK's single deepest pool is quoted in an
  obscure token and reports +542,339%, while its SOL and USDC pools all agree on ~11.6%.
