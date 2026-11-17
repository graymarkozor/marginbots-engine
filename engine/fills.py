"""
engine/fills.py — turn a bot's signals into fills, the way the leaderboard does.

A signal says what share of its equity a bot wants in each pair, for example
{"BTC/USDT": 0.5, "ETH/USDT": 0.2}: half in BTC, a fifth in ETH, the rest in
cash (USDT). Pairs it doesn't name have a target of 0. The leaderboard's
server merges each signal with the bot's previous target before storing it,
so the engine always receives the bot's full target.

For a signal received at server time t (spec, section 5.2):

1. The fill minute is the first minute that opens after t. Each pair fills at
   the open of its first candle at or after that minute (the same minute,
   unless the exchange has no candle for it). That price didn't exist yet when
   the signal arrived, so no bot can see the future.
2. Equity is cash plus every holding at its price at the fill minute. Each
   order is target × equity − current value, at the reference price. Changes
   smaller than 0.1% of equity are skipped; a target of 0 always closes the
   position fully.
3. Sells go first, then buys, each in alphabetical order of the pair. If the
   buys would cost more cash than the bot has (slippage and fees can push a
   100% target slightly over), every buy shrinks in the same proportion so
   cash ends at 0. No borrowing, no shorting.
4. Slippage goes against the bot (rules.py), then a 0.10% fee on the traded
   value.

The only inputs are the signals and public candles, so the same inputs
always give the same fills, to the last digit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext

from . import rules

ZERO = Decimal(0)


@dataclass(frozen=True)
class Signal:
    signal_id: str
    received_at: datetime         # server time, UTC
    targets: dict                 # the bot's full target: symbol -> weight (Decimal)


@dataclass(frozen=True)
class Fill:
    signal_id: str
    time: datetime                # open time of the candle that priced it
    symbol: str
    side: str                     # "buy" or "sell"
    quantity: Decimal
    reference_price: Decimal      # the candle's open
    price: Decimal                # after slippage
    value: Decimal                # quantity × price
    fee: Decimal
    slippage_cost: Decimal        # quantity × |price − reference_price|


@dataclass(frozen=True)
class Mark:
    """The bot's value at a moment, usually 00:00 UTC."""
    time: datetime
    equity: Decimal
    cash: Decimal
    positions: dict               # symbol -> quantity


class NotReady(Exception):
    """The candles needed to fill a signal haven't been collected yet."""

    def __init__(self, symbol: str, when: datetime):
        super().__init__(f"no candle for {symbol} at or after {when:%Y-%m-%d %H:%M} UTC yet")
        self.symbol = symbol
        self.when = when


def floor_minute(when: datetime) -> datetime:
    return when.replace(second=0, microsecond=0)


def fill_minute(received_at: datetime) -> datetime:
    """The first minute that opens strictly after the signal: 12:00:41 fills
    at 12:01, and 12:01:00.000 exactly fills at 12:02."""
    return floor_minute(received_at.astimezone(timezone.utc)) + rules.ONE_MINUTE


@dataclass
class Portfolio:
    cash: Decimal = rules.START_CASH
    positions: dict = field(default_factory=dict)   # symbol -> quantity > 0


class Engine:
    """One bot's portfolio, moved forward signal by signal."""

    def __init__(self, candles, start_cash: Decimal = rules.START_CASH):
        self.candles = candles
        self.portfolio = Portfolio(cash=start_cash)
        self.fills: list[Fill] = []

    # -- valuing --------------------------------------------------------------

    def equity_at(self, when: datetime, prices: dict | None = None) -> Decimal:
        with localcontext(rules.DECIMAL_CONTEXT):
            total = self.portfolio.cash
            for symbol, quantity in sorted(self.portfolio.positions.items()):
                price = prices[symbol] if prices and symbol in prices else self.candles.price_at(symbol, when)
                if price is None:
                    raise NotReady(symbol, when)
                total += quantity * price
            return total

    def mark(self, when: datetime) -> Mark:
        """The bot's value at `when` (00:00 UTC for the daily marks): each
        holding at the close of its last candle before that moment."""
        with localcontext(rules.DECIMAL_CONTEXT):
            prices = {}
            for symbol in self.portfolio.positions:
                candle = self.candles.last_before(symbol, when)
                if candle is None:
                    raise NotReady(symbol, when)
                prices[symbol] = candle.close
            return Mark(when, self.equity_at(when, prices), self.portfolio.cash,
                        dict(sorted(self.portfolio.positions.items())))

    # -- filling --------------------------------------------------------------

    def ready(self, signal: Signal) -> bool:
        try:
            self._check_ready(signal)
        except NotReady:
            return False
        return True

    def _symbols(self, signal: Signal) -> list[str]:
        """The pairs a signal touches: those it wants and those the bot holds."""
        wanted = {s for s, w in signal.targets.items() if Decimal(str(w)) > 0}
        return sorted(wanted | set(self.portfolio.positions))

    def _check_ready(self, signal: Signal) -> None:
        minute = fill_minute(signal.received_at)
        for symbol in self._symbols(signal):
            if not self.candles.has_data_at(symbol, minute):
                raise NotReady(symbol, minute)

    def apply(self, signal: Signal) -> list[Fill]:
        """Fill one signal. Raises NotReady, and changes nothing, when a
        candle it needs hasn't been collected yet."""
        self._check_ready(signal)
        minute = fill_minute(signal.received_at)
        with localcontext(rules.DECIMAL_CONTEXT):
            held = self.portfolio.positions
            symbols = self._symbols(signal)
            reference = {s: self.candles.first_at_or_after(s, minute) for s in symbols}
            prices = {s: self.candles.price_at(s, minute) for s in held}
            equity = self.equity_at(minute, prices)

            sells, buys = [], []
            for symbol in symbols:
                weight = Decimal(str(signal.targets.get(symbol, 0)))
                candle = reference[symbol]
                quantity_held = held.get(symbol, ZERO)
                if weight == 0:
                    if quantity_held > 0:
                        sells.append((symbol, candle, quantity_held))
                    continue
                change = weight * equity - quantity_held * candle.open
                if abs(change) < rules.MIN_TRADE_SHARE * equity:
                    continue
                quantity = abs(change) / candle.open
                if change < 0:
                    sells.append((symbol, candle, min(quantity, quantity_held)))
                else:
                    buys.append((symbol, candle, quantity))

            fills = [self._sell(signal, symbol, candle, quantity) for symbol, candle, quantity in sells]

            # Buys: shrink them all alike if they would cost more than the cash.
            unit_costs = [candle.open * (1 + rules.slippage(symbol)) * (1 + rules.FEE_RATE)
                          for symbol, candle, _ in buys]
            total_cost = sum((q * u for (_, _, q), u in zip(buys, unit_costs)), ZERO)
            scale = min(Decimal(1), self.portfolio.cash / total_cost) if total_cost > 0 else Decimal(1)
            for symbol, candle, quantity in buys:
                if quantity * scale > 0:
                    fills.append(self._buy(signal, symbol, candle, quantity * scale))
            if self.portfolio.cash < 0:
                # Rounding dust from the shrinking, in the 28th digit.
                self.portfolio.cash = ZERO

        self.fills.extend(fills)
        return fills

    def _sell(self, signal: Signal, symbol: str, candle, quantity: Decimal) -> Fill:
        price = candle.open * (1 - rules.slippage(symbol))
        value = quantity * price
        fee = value * rules.FEE_RATE
        self.portfolio.cash += value - fee
        remaining = self.portfolio.positions[symbol] - quantity
        if remaining > 0:
            self.portfolio.positions[symbol] = remaining
        else:
            del self.portfolio.positions[symbol]
        return Fill(signal.signal_id, candle.open_time, symbol, "sell", quantity, candle.open,
                    price, value, fee, quantity * (candle.open - price))

    def _buy(self, signal: Signal, symbol: str, candle, quantity: Decimal) -> Fill:
        price = candle.open * (1 + rules.slippage(symbol))
        value = quantity * price
        fee = value * rules.FEE_RATE
        self.portfolio.cash -= value + fee
        self.portfolio.positions[symbol] = self.portfolio.positions.get(symbol, ZERO) + quantity
        return Fill(signal.signal_id, candle.open_time, symbol, "buy", quantity, candle.open,
                    price, value, fee, quantity * (price - candle.open))


def midnights_after(start: datetime, end: datetime) -> list[datetime]:
    """Every 00:00 UTC strictly after `start`, up to `end` included."""
    day = start.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    out = []
    day += timedelta(days=1)
    while day <= end:
        out.append(day)
        day += timedelta(days=1)
    return out
