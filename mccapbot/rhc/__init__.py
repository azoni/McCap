"""Robinhood Chain (chain id 4663) wallets and DEX trading.

Custodial by design: McCap generates one EVM wallet per Discord user, keeps the
key encrypted on the data volume, and signs swaps on that user's behalf after
they confirm. Routing goes through the KyberSwap aggregator so a trade sees the
whole chain's liquidity (Uniswap V2/V3/V4, Ramses, Pons, ...) instead of one
router, and so the Robinhood-forked Uniswap V4 router is never encoded by us.

Modules:
    chain    RPC access with fallback across endpoints, ERC-20 reads, sending.
    wallets  The encrypted vault: create, decrypt at signing time, export.
    kyber    Route and build-transaction calls to the aggregator.
    guard    What must be true before anything is signed.
    ledger   Per-user daily spend caps and the trade journal.
    swap     The execution path: checks, approve, simulate, sign, send, receipt.
"""
