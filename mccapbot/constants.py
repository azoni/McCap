DEX_ALIASES = {
    "meteora": "meteora",
    "meteora-dlmm": "meteora",
    "raydium": "raydium",
    "raydium-clmm": "raydium",
    "pump": "pumpswap",
    "pumpswap": "pumpswap",
    "pump.fun": "pumpswap",
}
LP_VENUES = ["meteora", "raydium", "pumpswap"]

# Quote assets whose pools give a trustworthy price signal. A pool quoted in
# some obscure token reports a meaningless price change no matter how deep it
# is — BONK's largest pool is quoted in "TrumpBucks" and reads +542,339%.
MAJOR_QUOTES = {
    "SOL", "WSOL", "USDC", "USDT", "USDC.E", "DAI",
    "ETH", "WETH", "BTC", "WBTC", "BNB", "WBNB",
}
