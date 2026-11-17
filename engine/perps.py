"""
engine/perps.py — the Perps division: perpetual futures, with shorts and leverage.

The model is the spot one (fills.py) with three additions, all in rules.py:

1. A weight can be negative (a short) and the weights' absolute values can add
   up to 3 (MAX_LEVERAGE); a target above that shrinks, every weight alike.
   Quantities are signed: a short is a negative quantity. Selling a quantity
   adds its value to cash, buying takes it, and cash may go below 0 (that is
   the leverage). Equity is cash plus every quantity × its price, which is
   the profit and loss of a long and of a short alike.
2. Funding, every hour on the hour: each position pays quantity × price ×
   the published rate, so a long pays a positive rate and a short receives it.
3. Liquidation, checked over each stretch between two events (a fill, the
   hour), once the stretch is over, so at least once an hour: each position is priced at the worst
   price it reached in the stretch (the low for a long, the high for a
   short). If equity at those prices is 5% of the positions' value at those
   prices or less, everything closes at those prices, with slippage and
   fees, at the minute the last of them was reached. Equity below 0 ends at
   0: the bot is wiped out.

Signals fill as in fills.py: at the open of the first minute after they
arrive, with slippage against the bot and a fee (0.05%). Changes under 0.1%
of equity are skipped; a target of 0 always closes the position.

At a minute where several things happen: the 00:00 UTC mark (prices before
the minute), the hour's funding (at the minute's open), then the signal's
fills (at the open). The stretch that starts there includes that minute.

Funding rates come from a CSV of symbol, time, rate (one row per pair and
hour). A rate missing while later ones exist counts as 0; a rate the data
hasn't reached yet stops the replay there, like a missing candle.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Iterable

from . import rules
from .candles import CandleBook, parse_time
from .fills import Fill, Mark, NotReady, Signal, fill_minute, midnights_after
from .metrics import Metrics, compute

ZERO = Decimal(0)
ONE_HOUR = timedelta(hours=1)
BENCHMARK_SYMBOL = "BTC-PERP"


@dataclass(frozen=True)
class FundingPayment:
    time: datetime         # the hour it was paid
    symbol: str
    quantity: Decimal      # the position then, signed
    price: Decimal
    rate: Decimal
    amount: Decimal        # what the bot paid: negative when it received


@dataclass(frozen=True)
class Liquidation:
    time: datetime         # the minute the last of the worst prices was reached
    equity: Decimal        # at the stretch's worst prices, before closing
    maintenance: Decimal   # 5% of the positions' value at those prices


class FundingBook:
    """Hourly funding rates in memory, per pair."""

    def __init__(self, rows: Iterable[tuple[str, datetime, Decimal]]):
        self._rates: dict[str, dict[datetime, Decimal]] = {}
        for symbol, when, rate in rows:
            self._rates.setdefault(symbol, {})[when] = rate
        self._last = {s: max(r) for s, r in self._rates.items() if r}

    def rate(self, symbol: str, hour: datetime) -> Decimal:
        """The rate of that hour. Raises NotReady when the data hasn't reached
        it yet; a gap before the latest rate counts as 0."""
        rate = self._rates.get(symbol, {}).get(hour)
        if rate is not None:
            return rate
        last = self._last.get(symbol)
        if last is None or last < hour:
            raise NotReady(symbol, hour)
        return ZERO

    def last_time(self) -> datetime | None:
        return min(self._last.values()) if self._last else None


def load_funding_csv(path: str | Path) -> list[tuple[str, datetime, Decimal]]:
    """Rows of symbol, time, rate."""
    with open(path, newline="") as f:
        return [(row["symbol"].strip().upper(), parse_time(row["time"]), Decimal(row["rate"]))
                for row in csv.DictReader(f)]


def capped(targets: dict) -> dict:
    """The signal's weights as Decimals, shrunk alike if their absolute values
    add up to more than MAX_LEVERAGE."""
    weights = {s: Decimal(str(w)) for s, w in targets.items()}
    gross = sum((abs(w) for w in weights.values()), ZERO)
    if gross > rules.MAX_LEVERAGE:
        weights = {s: w * rules.MAX_LEVERAGE / gross for s, w in weights.items()}
    return weights


class PerpsEngine:
    """One Perps bot's account, moved forward stretch by stretch."""

    def __init__(self, candles: CandleBook, funding: FundingBook, start_cash: Decimal = rules.START_CASH):
        self.candles = candles
        self.funding = funding
        self.cash = start_cash
        self.positions: dict[str, Decimal] = {}   # symbol -> signed quantity, never 0
        self.fills: list[Fill] = []
        self.payments: list[FundingPayment] = []
        self.liquidations: list[Liquidation] = []
        self.max_leverage = ZERO                  # highest gross exposure ÷ equity right after a fill
        self.wiped_out = False

    # -- valuing --------------------------------------------------------------

    def _price(self, symbol: str, when: datetime) -> Decimal:
        price = self.candles.price_at(symbol, when)
        if price is None:
            raise NotReady(symbol, when)
        return price

    def equity_at(self, when: datetime, prices: dict | None = None) -> Decimal:
        with localcontext(rules.DECIMAL_CONTEXT):
            total = self.cash
            for symbol, quantity in sorted(self.positions.items()):
                price = prices[symbol] if prices and symbol in prices else self._price(symbol, when)
                total += quantity * price
            return total

    def mark(self, when: datetime) -> Mark:
        """The account at `when`: each position at the close of its last candle before it."""
        with localcontext(rules.DECIMAL_CONTEXT):
            prices = {}
            for symbol in self.positions:
                candle = self.candles.last_before(symbol, when)
                if candle is None:
                    raise NotReady(symbol, when)
                prices[symbol] = candle.close
            return Mark(when, self.equity_at(when, prices), self.cash, dict(sorted(self.positions.items())))

    # -- funding and liquidation ---------------------------------------------

    def pay_funding(self, hour: datetime) -> None:
        with localcontext(rules.DECIMAL_CONTEXT):
            for symbol, quantity in sorted(self.positions.items()):
                rate = self.funding.rate(symbol, hour)
                price = self._price(symbol, hour)
                amount = quantity * price * rate
                self.cash -= amount
                self.payments.append(FundingPayment(hour, symbol, quantity, price, rate, amount))

    def check_liquidation(self, start: datetime, end: datetime) -> list[Fill]:
        """Liquidate if the worst prices of [start, end) bring equity down to
        the maintenance margin."""
        if not self.positions or start >= end:
            return []
        with localcontext(rules.DECIMAL_CONTEXT):
            worst, when = {}, []
            for symbol, quantity in self.positions.items():
                found = self.candles.extremes(symbol, start, end)
                if found is None:  # no trade in the stretch: the last price
                    worst[symbol] = self._price(symbol, start)
                    continue
                (low, low_time), (high, high_time) = found
                worst[symbol], at = (low, low_time) if quantity > 0 else (high, high_time)
                when.append(at)
            equity = self.cash + sum((q * worst[s] for s, q in self.positions.items()), ZERO)
            maintenance = rules.MAINTENANCE_MARGIN * sum((abs(q) * worst[s] for s, q in self.positions.items()), ZERO)
            if equity > maintenance:
                return []
            at = max(when) if when else start
            self.liquidations.append(Liquidation(at, equity, maintenance))
            fills = [self._trade("liquidation", at, symbol, worst[symbol], -quantity)
                     for symbol, quantity in sorted(self.positions.items())]
            if self.cash <= 0:
                self.cash = ZERO
                self.wiped_out = True
        self.fills.extend(fills)
        return fills

    # -- filling --------------------------------------------------------------

    def _symbols(self, weights: dict) -> list[str]:
        return sorted({s for s, w in weights.items() if w != 0} | set(self.positions))

    def ready(self, signal: Signal) -> bool:
        minute = fill_minute(signal.received_at)
        return all(self.candles.has_data_at(s, minute) for s in self._symbols(capped(signal.targets)))

    def apply(self, signal: Signal) -> list[Fill]:
        """Fill one signal at its minute's open. Raises NotReady, and changes
        nothing, when a candle it needs hasn't been collected yet."""
        minute = fill_minute(signal.received_at)
        weights = capped(signal.targets)
        symbols = self._symbols(weights)
        for symbol in symbols:
            if not self.candles.has_data_at(symbol, minute):
                raise NotReady(symbol, minute)
        if self.wiped_out:
            return []
        with localcontext(rules.DECIMAL_CONTEXT):
            opens = {s: self.candles.first_at_or_after(s, minute) for s in symbols}
            prices = {s: self._price(s, minute) for s in self.positions}
            equity = self.equity_at(minute, prices)
            if equity <= 0:
                return []
            orders = []   # (symbol, candle, signed quantity to trade)
            for symbol in symbols:
                weight = weights.get(symbol, ZERO)
                candle = opens[symbol]
                held = self.positions.get(symbol, ZERO)
                if weight == 0:
                    if held != 0:
                        orders.append((symbol, candle, -held))
                    continue
                change = weight * equity - held * candle.open
                if abs(change) < rules.MIN_TRADE_SHARE * equity:
                    continue
                orders.append((symbol, candle, change / candle.open))
            # Sells first, then buys, each in the pairs' order, as for spot.
            orders.sort(key=lambda o: (o[2] > 0, o[0]))
            fills = [self._trade(signal.signal_id, candle.open_time, symbol, candle.open, quantity)
                     for symbol, candle, quantity in orders]
            after = self.equity_at(minute, {s: opens[s].open for s in self.positions})
            if after > 0:
                gross = sum((abs(q) * opens[s].open for s, q in self.positions.items()), ZERO)
                self.max_leverage = max(self.max_leverage, gross / after)
        self.fills.extend(fills)
        return fills

    def _trade(self, signal_id: str, time: datetime, symbol: str, reference: Decimal, quantity: Decimal) -> Fill:
        """Trade a signed quantity (positive buys) at `reference`, with slippage and fee."""
        slip = rules.perps_slippage(symbol)
        side = "buy" if quantity > 0 else "sell"
        size = abs(quantity)
        price = reference * (1 + slip) if quantity > 0 else reference * (1 - slip)
        value = size * price
        fee = value * rules.PERPS_FEE_RATE
        self.cash += (-value if quantity > 0 else value) - fee
        remaining = self.positions.get(symbol, ZERO) + quantity
        if remaining != 0:
            self.positions[symbol] = remaining
        else:
            self.positions.pop(symbol, None)
        return Fill(signal_id, time, symbol, side, size, reference, price, value, fee, size * abs(price - reference))


@dataclass
class PerpsResult:
    fills: list = field(default_factory=list)
    marks: list = field(default_factory=list)
    benchmark: list = field(default_factory=list)
    pending: list = field(default_factory=list)
    filled_signals: int = 0
    final: Mark | None = None
    benchmark_final: Mark | None = None                # 1× long BTC-PERP at the same moment
    benchmark_fills: list = field(default_factory=list)
    metrics: Metrics | None = None
    benchmark_metrics: Metrics | None = None
    payments: list = field(default_factory=list)       # FundingPayment
    liquidations: list = field(default_factory=list)   # Liquidation
    funding_paid: Decimal = ZERO                       # net: negative when the bot received more
    max_leverage: Decimal = ZERO
    wiped_out: bool = False


def replay_perps(signals: list[Signal], candles: CandleBook, funding: FundingBook,
                 until: datetime | None = None, retired_at: datetime | None = None) -> PerpsResult:
    """Fill every signal in order: marks at 00:00 UTC, funding on the hour,
    liquidation checks over every stretch between fills and hours, up to `until` (by
    default, when the last candle closes). The benchmark is 1× long BTC-PERP
    from the bot's first fill, with the same fees and funding. A signal whose
    candles or funding are missing stops the replay there."""
    result = PerpsResult()
    ordered = sorted(signals, key=lambda s: s.received_at)
    if retired_at is not None:
        ordered = [s for s in ordered if s.received_at < retired_at]
        ordered.append(Signal("retired", retired_at, {}))
    last = candles.last_time()
    end = until or (last + rules.ONE_MINUTE if last else None)
    if not ordered or end is None:
        result.pending = ordered
        return result

    bot = PerpsEngine(candles, funding)
    engines = [bot]
    benchmark: PerpsEngine | None = None
    start = fill_minute(ordered[0].received_at)
    midnights = set(midnights_after(start, end))
    cursor = start   # where the current stretch began

    def at_minute(m: datetime) -> None:
        """The mark and the funding due at minute m, before any fill."""
        if m in midnights:
            result.marks.append(bot.mark(m))
            if benchmark is not None:
                result.benchmark.append(benchmark.mark(m))
        if m.minute == 0:
            for engine in engines:
                engine.pay_funding(m)

    def run_until(limit: datetime, data_end: bool = False) -> None:
        """Every stretch up to `limit`: liquidation checks, then the mark and
        funding of each hour on the way. When `limit` is only where the data
        ends (not a fill or an hour), the last stretch isn't over yet: it is
        checked once it is, so a record never changes as data arrives."""
        nonlocal cursor
        while cursor < limit:
            hour = cursor.replace(minute=0, second=0, microsecond=0) + ONE_HOUR
            stop = min(hour, limit)
            if data_end and stop < hour:
                return
            for engine in engines:
                engine.check_liquidation(cursor, stop)
            cursor = stop
            if stop == hour:
                at_minute(stop)

    following = 0   # the first signal not filled yet
    reached = end   # where the record stops: the data's end, or what is missing
    try:
        at_minute(start)
        for i, signal in enumerate(ordered):
            at = fill_minute(signal.received_at)
            if at >= end:
                result.pending = ordered[i:]
                break
            run_until(at)
            fills = bot.apply(signal)
            if fills and benchmark is None and candles.has_data_at(BENCHMARK_SYMBOL, at):
                benchmark = PerpsEngine(candles, funding)
                engines.append(benchmark)
                result.benchmark_fills = benchmark.apply(
                    Signal("benchmark", signal.received_at, {BENCHMARK_SYMBOL: Decimal(1)}))
            following = i + 1
            if signal.signal_id != "retired" or retired_at is None:
                result.filled_signals += 1
        if not result.pending:
            run_until(end, data_end=True)
    except NotReady:
        # Candles or funding not collected yet: the record stops here.
        result.pending = ordered[following:]
        reached = min(cursor, end)
    # Liquidation fills are the bot's own record too.
    result.fills = list(bot.fills)
    result.final = bot.mark(reached)
    if benchmark is not None:
        result.benchmark_final = benchmark.mark(reached)
    result.payments, result.liquidations = bot.payments, bot.liquidations
    with localcontext(rules.DECIMAL_CONTEXT):
        result.funding_paid = sum((p.amount for p in bot.payments), ZERO)
    result.max_leverage, result.wiped_out = bot.max_leverage, bot.wiped_out
    if result.fills:
        retired = retired_at is not None and not result.pending
        result.metrics = compute(result.fills[0].time, result.marks, result.fills, retired=retired)
    if benchmark is not None and result.benchmark_fills:
        result.benchmark_metrics = compute(result.benchmark_fills[0].time, result.benchmark, benchmark.fills)
    return result


def write_funding(path: Path, payments: list[FundingPayment]) -> None:
    from .replay import fmt
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["time", "symbol", "quantity", "price", "rate", "amount"])
        for p in payments:
            writer.writerow([fmt(p.time), p.symbol, fmt(p.quantity), fmt(p.price), fmt(p.rate), fmt(p.amount)])


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    from .candles import load_candles_csv
    from .replay import DEFAULT_OUT, fmt, load_signals_csv, metrics_dict, write_equity, write_fills

    parser = argparse.ArgumentParser(prog="python3 -m engine replay-perps",
                                     description="Replay a Perps bot's signals over public 1-minute candles and funding.")
    parser.add_argument("signals", help="signals CSV: signal_id, received_at, targets (weights may be negative)")
    parser.add_argument("candles", nargs="+", help="one or more candle CSV files (symbols like BTC-PERP)")
    parser.add_argument("--funding", required=True, help="funding CSV: symbol, time, rate (hourly)")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="folder for the output files")
    parser.add_argument("--until", help="replay up to this UTC time (default: when the last candle closes)")
    parser.add_argument("--retired-at", help="the bot was retired at this UTC time")
    args = parser.parse_args(argv)

    candles = CandleBook(c for path in args.candles for c in load_candles_csv(path))
    funding = FundingBook(load_funding_csv(args.funding))
    signals = load_signals_csv(args.signals)
    result = replay_perps(signals, candles, funding,
                          until=parse_time(args.until) if args.until else None,
                          retired_at=parse_time(args.retired_at) if args.retired_at else None)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    write_fills(out / "fills.csv", result.fills)
    write_equity(out / "equity.csv", result.marks, result.benchmark)
    write_funding(out / "funding.csv", result.payments)
    with open(out / "metrics.json", "w") as f:
        json.dump({"bot": metrics_dict(result.metrics), "btc_perp_1x": metrics_dict(result.benchmark_metrics),
                   "funding_paid": fmt(result.funding_paid), "max_leverage": fmt(result.max_leverage),
                   "liquidations": [fmt(x.time) for x in result.liquidations], "wiped_out": result.wiped_out},
                  f, indent=2)
        f.write("\n")

    print(f"{result.filled_signals} of {len(signals)} signals filled, {len(result.fills)} fills, "
          f"{len(result.liquidations)} liquidations.")
    if result.final is not None:
        change = (result.final.equity / rules.START_CASH - 1) * 100
        print(f"Equity at {fmt(result.final.time)}: {result.final.equity:.2f} USDC ({change:+.2f}%), "
              f"funding paid {result.funding_paid:.2f}, highest leverage {result.max_leverage:.2f}x.")
    if result.pending:
        print(f"Stopped at signal {result.pending[0].signal_id}: its candles or funding are missing.")
    print(f"Written: fills.csv, equity.csv, funding.csv and metrics.json in {out}")
    return 1 if result.pending else 0
