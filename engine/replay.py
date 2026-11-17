"""
engine/replay.py — replay a bot's signals over public candles, offline.

This is how anyone checks a MarginBots bot page: download the bot's signals
and the candles from its page, run the replay, and compare. The same inputs
give the same fills and the same daily equity, to the last digit.

    python3 -m engine replay signals.csv candles.csv
    python3 -m engine replay signals.csv BTC-USDT_1m.csv ETH-USDT_1m.csv --out my-check

Inputs:
  signals.csv  columns signal_id, received_at, targets. received_at is the
               server time (UTC); targets is the bot's full target after the
               signal, as JSON: {"BTC/USDT": 0.5, "ETH/USDT": 0.2}.
  candles      one or more CSV files of 1-minute candles (see candles.py).

Outputs, in --out (data/output/replay/ by default, which git ignores):
  fills.csv     every fill, in order;
  equity.csv    the bot's equity at each 00:00 UTC, next to buy-and-hold BTC
                started at the bot's first fill, with the same slippage and fee;
  metrics.json  the bot page's numbers (metrics.py), for the bot and for BTC.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from . import rules
from .candles import CandleBook, load_candles_csv, parse_time
from .fills import Engine, Fill, Mark, NotReady, Signal, fill_minute, midnights_after
from .metrics import Metrics, compute

DEFAULT_OUT = Path(__file__).resolve().parent.parent / "data" / "output" / "replay"
BENCHMARK_SYMBOL = "BTC/USDT"


@dataclass
class Result:
    fills: list = field(default_factory=list)        # Fill
    marks: list = field(default_factory=list)        # Mark, the bot
    benchmark: list = field(default_factory=list)    # Mark, buy-and-hold BTC
    pending: list = field(default_factory=list)      # Signal not filled: candles missing
    filled_signals: int = 0                          # the bot's own signals that were filled
    final: Mark | None = None
    benchmark_fills: list = field(default_factory=list)
    metrics: Metrics | None = None                   # None until the bot's first fill
    benchmark_metrics: Metrics | None = None


def load_signals_csv(path: str | Path) -> list[Signal]:
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    signals = []
    for row in rows:
        targets = json.loads(row["targets"], parse_float=Decimal, parse_int=Decimal)
        if not isinstance(targets, dict):
            raise ValueError(f"signal {row['signal_id']}: targets must be a JSON object")
        signals.append(Signal(row["signal_id"], parse_time(row["received_at"]), targets))
    return signals


def replay(signals: list[Signal], candles: CandleBook, until: datetime | None = None,
           retired_at: datetime | None = None) -> Result:
    """Fill every signal in order of received_at, and mark the bot (and
    buy-and-hold BTC) at each 00:00 UTC up to `until` (by default, the
    moment the last candle closes). A signal whose candles are missing stops the replay there: the
    ones after it depend on it, so they are returned as pending."""
    result = Result()
    ordered = sorted(signals, key=lambda s: s.received_at)
    if retired_at is not None:
        # Retiring closes every position at the next candle open; later
        # signals don't count.
        ordered = [s for s in ordered if s.received_at < retired_at]
        ordered.append(Signal("retired", retired_at, {}))
    last = candles.last_time()
    end = until or (last + rules.ONE_MINUTE if last else None)
    if not ordered or end is None:
        result.pending = ordered
        return result

    bot = Engine(candles)
    start = fill_minute(ordered[0].received_at)
    # Buy-and-hold BTC starts with the bot's first fill, at the same minute,
    # when the candles include BTC then; otherwise there is no benchmark.
    benchmark: Engine | None = None
    midnights = midnights_after(start, end)

    def mark_until(limit: datetime) -> None:
        while midnights and midnights[0] <= limit:
            midnight = midnights.pop(0)
            result.marks.append(bot.mark(midnight))
            if benchmark is not None:
                result.benchmark.append(benchmark.mark(midnight))

    for i, signal in enumerate(ordered):
        minute = fill_minute(signal.received_at)
        if minute > end:
            result.pending = ordered[i:]
            break
        mark_until(minute)
        try:
            fills = bot.apply(signal)
        except NotReady:
            result.pending = ordered[i:]
            break
        if fills and not result.fills and candles.has_data_at(BENCHMARK_SYMBOL, minute):
            benchmark = Engine(candles)
            result.benchmark_fills = benchmark.apply(
                Signal("benchmark", signal.received_at, {BENCHMARK_SYMBOL: Decimal(1)}))
        result.fills.extend(fills)
        if signal.signal_id != "retired" or retired_at is None:
            result.filled_signals += 1
    if not result.pending:
        mark_until(end)
    result.final = bot.mark(end)
    if result.fills:
        retired = retired_at is not None and not result.pending
        result.metrics = compute(result.fills[0].time, result.marks, result.fills, retired=retired)
    if result.benchmark_fills:
        result.benchmark_metrics = compute(result.benchmark_fills[0].time, result.benchmark,
                                           result.benchmark_fills)
    return result


def fmt(value) -> str:
    if isinstance(value, Decimal):
        text = format(value.normalize(), "f")
        return "0" if text in ("-0", "") else text
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value)


def write_fills(path: Path, fills: list[Fill]) -> None:
    columns = ["signal_id", "time", "symbol", "side", "quantity", "reference_price", "price",
               "value", "fee", "slippage_cost"]
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        for fill in fills:
            writer.writerow([fmt(getattr(fill, c)) for c in columns])


def write_equity(path: Path, marks: list[Mark], benchmark: list[Mark]) -> None:
    btc_by_time = {m.time: m.equity for m in benchmark}
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["time", "equity", "cash", "positions", "btc_buy_and_hold"])
        for mark in marks:
            positions = json.dumps({s: fmt(q) for s, q in mark.positions.items()})
            btc = btc_by_time.get(mark.time)
            writer.writerow([fmt(mark.time), fmt(mark.equity), fmt(mark.cash), positions,
                             fmt(btc) if btc is not None else ""])


def metrics_dict(metrics: Metrics | None) -> dict | None:
    if metrics is None:
        return None
    return {name: (fmt(value) if value is not None else None) for name, value in vars(metrics).items()}


def write_metrics(path: Path, result: Result) -> None:
    with open(path, "w") as f:
        json.dump({"bot": metrics_dict(result.metrics),
                   "btc_buy_and_hold": metrics_dict(result.benchmark_metrics)}, f, indent=2)
        f.write("\n")


def pct(value: Decimal | None) -> str:
    return "n/a" if value is None else f"{value * 100:+.2f}%"


def ratio(value: Decimal | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m engine replay",
                                     description="Replay a bot's signals over public 1-minute candles.")
    parser.add_argument("signals", help="signals CSV: signal_id, received_at, targets")
    parser.add_argument("candles", nargs="+", help="one or more candle CSV files")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="folder for fills.csv and equity.csv")
    parser.add_argument("--until", help="mark the bot up to this UTC time (default: when the last candle closes)")
    parser.add_argument("--retired-at", help="the bot was retired at this UTC time")
    args = parser.parse_args(argv)

    candles = CandleBook(c for path in args.candles for c in load_candles_csv(path))
    signals = load_signals_csv(args.signals)
    result = replay(signals, candles,
                    until=parse_time(args.until) if args.until else None,
                    retired_at=parse_time(args.retired_at) if args.retired_at else None)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    write_fills(out / "fills.csv", result.fills)
    write_equity(out / "equity.csv", result.marks, result.benchmark)
    write_metrics(out / "metrics.json", result)

    print(f"{result.filled_signals} of {len(signals)} signals filled, {len(result.fills)} fills.")
    if result.final is not None:
        change = (result.final.equity / rules.START_CASH - 1) * 100
        print(f"Equity at {fmt(result.final.time)}: {result.final.equity:.2f} USDT ({change:+.2f}%).")
    if result.benchmark:
        print(f"{len(result.marks)} daily marks; buy-and-hold BTC at the last one: "
              f"{result.benchmark[-1].equity:.2f} USDT.")
    elif result.marks:
        print(f"{len(result.marks)} daily marks; no BTC/USDT candles, so no buy-and-hold BTC column.")
    m, b = result.metrics, result.benchmark_metrics
    if m is not None:
        print(f"{m.status.capitalize()}, {m.days_live} days, {m.fills} fills, as of {fmt(m.as_of)}.")
        rows = [("Return", pct, "total_return"), ("Max drawdown", pct, "max_drawdown"),
                ("Annualized return", pct, "annualized_return"), ("Volatility", pct, "volatility"),
                ("Sharpe", ratio, "sharpe"), ("Sortino", ratio, "sortino"), ("Calmar", ratio, "calmar")]
        for label, show, name in rows:
            btc = show(getattr(b, name)) if b is not None else "n/a"
            print(f"  {label:<18} {show(getattr(m, name)):>9}   BTC {btc:>9}")
        if m.annualized_return is None:
            print("  Yearly figures appear after 90 days and 20 fills.")
    if result.pending:
        first = result.pending[0]
        print(f"Stopped at signal {first.signal_id}: its candles are missing. Add them and replay again.")
    print(f"Written: {out / 'fills.csv'}, {out / 'equity.csv'} and {out / 'metrics.json'}")
    return 1 if result.pending else 0


if __name__ == "__main__":
    sys.exit(main())
