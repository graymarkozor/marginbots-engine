"""
engine/rules.py — the numbers of the fill model, in one place.

These are the rules the MarginBots leaderboard publishes on its methodology
page. Anyone who replays a bot's signals with this engine uses exactly the
same numbers, so they get exactly the same fills.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal

# Every bot starts with this much virtual cash, in USDT.
START_CASH = Decimal("10000")

# Fee on the traded value of every fill: 0.10%, a conservative retail taker fee.
FEE_RATE = Decimal("0.001")

# Slippage, always against the bot: it buys a little above the reference
# price and sells a little below. 5 bps (0.05%) for BTC and ETH, 15 bps for
# the other pairs, which trade less.
MAJORS = frozenset({"BTC/USDT", "ETH/USDT"})
SLIPPAGE_MAJOR = Decimal("0.0005")
SLIPPAGE_OTHER = Decimal("0.0015")

# Changes smaller than 0.1% of equity are skipped (a target of 0 always
# closes the position fully, however small).
MIN_TRADE_SHARE = Decimal("0.001")

# Candles are one minute long.
ONE_MINUTE = timedelta(minutes=1)

# All the arithmetic runs in this decimal context: 28 significant digits,
# banker's rounding. Fixing it here makes every result identical on every
# computer, which floats would not guarantee once printed and read back.
DECIMAL_CONTEXT = Context(prec=28, rounding=ROUND_HALF_EVEN)


def slippage(symbol: str) -> Decimal:
    return SLIPPAGE_MAJOR if symbol in MAJORS else SLIPPAGE_OTHER


# -- The Perps division: perpetual futures (engine/perps.py) ---------------------
# A Perps bot may go short and use leverage. It has its own leaderboard and
# never mixes with the spot one. Pairs are written "BTC-PERP"; the prices and
# funding rates are Hyperliquid's, public.

# Fee on the traded value of every fill: 0.05%, a little above Hyperliquid's
# base taker fee (0.045%).
PERPS_FEE_RATE = Decimal("0.0005")

# Slippage against the bot, as for spot: 5 bps for BTC and ETH, 15 for the rest.
PERPS_MAJORS = frozenset({"BTC-PERP", "ETH-PERP"})

# Gross exposure, the sum of |weight| over the pairs, is at most 3 times the
# bot's equity. A target above that shrinks, every weight alike, down to 3.
MAX_LEVERAGE = Decimal("3")

# Liquidation. Over each stretch of time between two events (a fill, the
# hour), at most an hour long and checked once it is over (so the record is
# the same whenever it is computed), every position is priced at the worst price it
# reached in that stretch: its low for a long, its high for a short. If the
# bot's equity at those prices is 5% of its positions' value at those prices
# or less, everything closes at those prices, with slippage and fees, at the
# minute the last of them was reached. Equity below 0 ends at 0: the bot is
# wiped out and can't trade again.
MAINTENANCE_MARGIN = Decimal("0.05")

# Funding: every hour on the hour, each open position pays
# quantity × price × rate (a long pays a positive rate, a short receives it),
# at the rate the exchange published for that hour and the open of that minute.


def perps_slippage(symbol: str) -> Decimal:
    return SLIPPAGE_MAJOR if symbol in PERPS_MAJORS else SLIPPAGE_OTHER
