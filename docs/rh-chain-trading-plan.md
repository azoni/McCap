# Trading Robinhood Chain tokens from Discord

Status: **built 2026-09-07, as a Python port inside McCap** (`mccapbot/rhc/`,
`/rh` commands). Decisions taken: operator-decryptable custody (scrypt +
SecretBox under `RHC_WALLET_SECRET`), KyberSwap-only routing with the router
pinned and every swap simulated before signing, per-user caps that reserve then
refund, no paper mode (the flag is the gate). The research below is what those
decisions rested on; the README describes what shipped.

## The ask

Let McCap trade Robinhood *Chain* tokens (PONS, RSTR, new pairs) for several
people in the Discord. McCap generates a wallet per person, secures it to that
person, and executes buys and sells with fast, cheap, well-filled swaps.

## What already exists

`Meme/mccap-web` (the personal terminal) has a working, hardened Robinhood Chain
execution engine in TypeScript, verified against the live chain on 2026-08-30:

| Piece | File | Why it matters |
|---|---|---|
| Verified addresses | `src/server/evm/chain.ts` | SwapRouter02 `0xcaf681a6…9e5cb2`, V3 factory, WETH, USDG, Permit2, Multicall3. Each probed by RPC, not copied from a list. |
| Drainer guard | `evm/guard.ts` | Chain 4663 has live drainers squatting Uniswap's canonical mainnet addresses. They accept bare ETH and return success. The guard denylists them, positively identifies the router by `factory()`/`WETH9()`, and requires the real router's bare-ETH revert. |
| Quotes by simulation | `evm/quote.ts` | No QuoterV2 on this chain. Quotes are `eth_call` state-override simulations of the real swap, so price impact and tick crossings are included. Sell quotes discover storage slots at runtime. |
| Swap | `evm/swap.ts` | Chain-id check, gas balance check, approve-then-swap, `amountOutMinimum` always from the quote. |
| Gas model | `evm/gas.ts` | Median swap = 157k gas ≈ $0.26. Fixed cost, so a $10 round trip pays ~5%. |
| Screener | `evm/screener.ts` | GeckoTerminal `robinhood` feed with junk filters; only `uniswap-v3-robinhood` pools are simulatable. |
| Vault | `crypto.ts`, `credentials.ts` | AES-256-GCM envelope encryption, scrypt-derived key from `CREDENTIALS_SECRET`, fresh salt and IV per record. |
| Tests | `src/server/__tests__` | 88+ vitest tests including a live-path suite. |

It is single-user and runs on Charlton's PC. The Discord bot is Python on Railway.

One warning from that code base carries over unchanged: the Uniswap V4
UniversalRouter on chain 4663 is a **Robinhood fork**. A stock V4 SDK encodes
calldata that succeeds on ETH pairs and reverts on token/token pools. Do not
integrate V4 directly with a stock SDK.

## Facts gathered today

**Chain.** Chain ID 4663, Arbitrum Orbit (Nitro) L2, gas in ETH, roughly 100 ms
soft confirmations, a single sequencer run by Robinhood. Public RPC
`https://rpc.mainnet.chain.robinhood.com` is rate limited and documented as not
for production; `https://robinhood-rpc.publicnode.com` is an independent fallback.
Explorer: `explorer.mainnet.chain.robinhood.com` (Blockscout).

**Node providers named in Robinhood's docs for production:** QuickNode, Alchemy,
Blockdaemon, dRPC, Validation Cloud. Also offering the chain: Chainstack,
Tenderly, Goldsky, thirdweb, OrbitFlare. No pricing was compared; that is a
purchase decision.

**Where the volume is (DefiLlama, 24h, 2026-09-07).** DEXes: Uniswap V4 $749M,
GMGN $331M, Uniswap V3 $156M, Pons V2 $147M, Uniswap V2 $96M, Ramses $63M, Up V3
$50M, plus a tail. Aggregators routing on the chain: 0x $231M, KyberSwap $144M,
fly.trade $28M, LI.FI $9M, 1inch $3.6M.

Two consequences. The V3-only path in mccap-web sees about a tenth of the chain's
liquidity. And today's new pairs (Nasduck, SNOWBALL) launched on Uniswap **V2**,
so "trade new pairs" needs V2 as well. An aggregator solves both without
touching the V4 fork problem, because it routes through its own settlement
contract.

**Funding.** The Robinhood app withdraws ETH straight onto Robinhood Chain, no
bridge. Exiting back over the canonical bridge takes about seven days.

**MEV.** A single first-come-first-served sequencer means no public mempool, so
classic mempool sandwiching is not the threat here. The threats are bad slippage
settings, squatted contracts, and honeypot tokens.

## Recommended architecture

**A. Reuse the engine.** Deploy mccap-web's server as a second Railway service
(the "engine") next to the bot, reachable only over Railway private networking
with a shared secret. McCap stays the Discord front end and calls the engine for
wallet, quote, and swap. Extend the engine's vault from one user to many.

Why: the dangerous parts (address verification, drainer guard, simulation quotes,
gas accounting) are already written, tested, and have been run against the live
chain. Porting them to Python duplicates a hardened engine and re-introduces the
bugs it already fixed.

**B. Port to Python** (web3.py + eth-account inside McCap). One codebase, one
deploy. Rejected for v1 for the reason above; revisit if running two services is
a problem.

**C. Non-custodial.** Each person connects their own wallet and signs each trade.
Discord has no signing surface, so this becomes "McCap posts a link, you sign on
your phone". Safer, much worse UX, and no auto-trading. Worth offering as an
opt-out later, not as the base.

## Custody model (the decision that matters most)

- One wallet per Discord user, generated by the engine. Record:
  `{discord_id, address, envelope, created_ts}` on the volume.
- Key envelope-encrypted with the existing `crypto.ts` scheme under a server
  secret (`WALLET_MASTER_SECRET`). Only interactions whose `user.id` matches the
  record can trade, export, or withdraw. Export is ephemeral or DM only, behind a
  confirm button bound to that user, and logged.
- **Be plain about what this is:** custodial hot wallets. Whoever controls the
  Railway environment and the volume controls every wallet. Users are trusting
  Charlton. Keep balances small, make withdrawing easy, say so in `/rh wallet
  create`.
- Losing `WALLET_MASTER_SECRET` or the wallet file loses everyone's funds. Both
  need an offline backup before the first deposit.
- Option: a per-user passphrase mixed into the key derivation. Then the operator
  cannot decrypt a wallet without the user. Cost: a forgotten passphrase is an
  unrecoverable wallet, and McCap cannot trade on that user's behalf while they
  are away. Choose one; they cannot both be true.

## Routing and fills

1. Quote through an aggregator first: **0x Swap API** and **KyberSwap Aggregator
   API** both route on chain 4663 with real volume. Compare their quotes net of
   gas and take the better one. Fall back to the direct V3 `exactInputSingle`
   path when neither returns a route. (0x needs an API key; check the free tier.)
2. Apply the guard philosophy to the aggregator too: the allowance target and
   settlement contract must match the address the aggregator publishes for chain
   4663, verified once by code size and the bare-ETH probe, then pinned. Never
   trust an address that arrives in an API response without that check.
3. Honeypot check before any buy: simulate the sell (exists in mccap-web).
4. `amountOutMinimum` always from the quote and the user's slippage; default 2%,
   hard cap 10%.
5. Dedicated RPC for submit and reads (QuickNode or Alchemy), with the public
   endpoints as fallbacks behind the existing latency ranking.
6. GMGN runs a router on this chain with a third of the DEX volume, and GMGN has
   an official self-serve API (`openapi.gmgn.ai`). Worth checking whether it
   exposes swap routing for chain 4663 before choosing; it is where the memecoin
   flow is.

## Discord surface, v1

| Command | Behaviour |
|---|---|
| `/rh wallet create` | Generates a wallet bound to the caller. Replies ephemerally with the address and the custody warning. |
| `/rh wallet show` | Address, ETH balance, token holdings with USD values. Ephemeral. |
| `/rh wallet export` | Private key, DM only, confirm button, logged. |
| `/rh wallet withdraw <to> <eth>` | Send ETH out. Confirm button. |
| `/rh quote <token> <eth>` | Best route and expected output, gas, price impact. |
| `/rh buy <token> <eth> [slippage]` | Quote, honeypot sim, confirm, swap, receipt. |
| `/rh sell <token> <percent>` | Same path in reverse. Exits are never capped. |
| `/rh holdings` | What the caller holds across their wallet, with P&L from the engine's journal. |

Guards, all carried over from mccap-web and the existing `rh_*` commands:
feature flag off by default; guild allowlist; per-user per-trade and daily caps
set by the operator, checked before quoting and re-checked after the confirm
button; confirm button bound to the caller and expiring; spend recorded only after
the chain accepts the transaction; honest receipt status; paper mode first.

## Decisions needed

1. Custody: operator-decryptable (enables auto-trading) or per-user passphrase
   (operator locked out, no auto-trading).
2. Architecture A (engine service) or B (Python port).
3. Routing: aggregator-first (0x / KyberSwap) or V3-only to start.
4. Node provider and budget (QuickNode vs Alchemy vs stay on public + publicnode).
5. Who may trade: allowlisted users or roles, and the caps.
6. Whether the engine runs 24/7 on Railway (second always-on service) or only
   while the terminal is up on the PC (then Discord trading is intermittent).

## Phases

- **Phase 0, no money.** Engine deployed with paper mode forced. `/rh wallet
  create/show`, `/rh quote` through the aggregator, journal visible. Proves the
  plumbing and the quotes.
- **Phase 1, owner only.** Live buys and sells for `RH_OWNER_ID` with small caps.
  Compare realised fills against quotes for a week.
- **Phase 2, the group.** Allowlisted users, export and withdraw, per-user
  ledgers, custody warning in every wallet creation.
- **Phase 3, new pairs.** Alerts from `/rh_trending new` feeding a
  honeypot-simulated quick-buy flow, only after Phase 1 shows the fills are sane.

## Sources

- Robinhood Chain docs: connecting, RPC providers, full-node guide (docs.robinhood.com/chain)
- QuickNode, "Top 10 Robinhood Chain RPC providers"
- DefiLlama API, `overview/dexs/robinhood-chain` and `overview/aggregators/robinhood-chain`
- LI.FI changelog: Robinhood Chain live on the LI.FI API
- `Meme/mccap-web/src/server/evm/*.ts` headers, verified 2026-08-30 and 2026-09-01
