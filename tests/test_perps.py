"""
tests/test_perps.py — the Perps division (engine/perps.py), against hand-computed examples.

Every expected number is worked out by hand from the rules in engine/rules.py:
fee 0.05%, slippage 5 bps on BTC-PERP and ETH-PERP, 3× gross leverage at most,
liquidation at 5% maintenance, hourly funding. No network, standard library only.
Battery protocol: one PASS or FAIL line per check, non-zero exit on failure.
Run it with:  python3 tests/test_perps.py
"""

from __future__ import annotations

import csv
import io
import json
import random
import sys
import tempfile
import traceback
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import rules  # noqa: E402
from engine.candles import Candle, CandleBook  # noqa: E402
from engine.fills import Signal  # noqa: E402
from engine.metrics import time_in_market  # noqa: E402
from engine.perps import FundingBook, PerpsEngine, capped, main as perps_main, replay_perps  # noqa: E402

PASS_TOKEN = "  " + "PASS" + "  "
FAIL_TOKEN = "  " + "FAIL" + "  "
T0 = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)   # on the hour
M = timedelta(minutes=1)
H = timedelta(hours=1)

CHECKS = []


def check(function):
    CHECKS.append(function)
    return function


def candles(symbol, prices, start=T0 - 5 * M, lows=None, highs=None):
    """One candle per minute from `start`: open = close = the price, with an
    optional low or high for chosen minutes."""
    lows, highs = lows or {}, highs or {}
    out = []
    for i, price in enumerate(prices):
        t = start + i * M
        p = D(str(price))
        out.append(Candle(symbol, t, p, D(str(highs.get(t, price))), D(str(lows.get(t, price))), p))
    return out


def flat(symbol, price, minutes=200, **kw):
    return candles(symbol, [price] * minutes, **kw)


def no_funding(*symbols, hours=48):
    return FundingBook((s, T0 - 5 * H + i * H, D(0)) for s in symbols for i in range(hours))


def sig(seconds, targets, signal_id="s"):
    return Signal(signal_id, T0 + timedelta(seconds=seconds), {k: D(str(v)) for k, v in targets.items()})


@check
def a_short_sells_at_the_open_with_slippage_and_fee():
    book = CandleBook(flat("BTC-PERP", 100))
    engine = PerpsEngine(book, no_funding("BTC-PERP"))
    [fill] = engine.apply(sig(10, {"BTC-PERP": -1}))
    # 10,000 × −1 at 100: sell 100 at 100 × (1 − 0.0005) = 99.95.
    assert (fill.side, fill.time, fill.quantity, fill.price) == ("sell", T0 + M, D(100), D("99.95"))
    assert fill.value == D("9995") and fill.fee == D("4.9975")
    # Cash 10,000 + 9,995 − 4.9975; the position is −100.
    assert engine.cash == D("19990.0025") and engine.positions == {"BTC-PERP": D(-100)}
    # The price falls to 90: the short earns 1,000.
    assert engine.equity_at(T0, {"BTC-PERP": D(90)}) == D("10990.0025")


@check
def gross_exposure_is_capped_at_3x():
    assert capped({"BTC-PERP": D(2), "ETH-PERP": D(-2)}) == {"BTC-PERP": D("1.5"), "ETH-PERP": D("-1.5")}
    assert capped({"BTC-PERP": D("-2.5")}) == {"BTC-PERP": D("-2.5")}
    book = CandleBook(flat("BTC-PERP", 100) + flat("ETH-PERP", 10))
    engine = PerpsEngine(book, no_funding("BTC-PERP", "ETH-PERP"))
    fills = engine.apply(sig(10, {"BTC-PERP": 2, "ETH-PERP": -2}))
    # Sells first: ETH short of 1.5 × 10,000 ÷ 10 = 1,500; then BTC long of 150.
    assert [(f.symbol, f.side, f.quantity) for f in fills] == [("ETH-PERP", "sell", D(1500)), ("BTC-PERP", "buy", D(150))]
    # Fees and slippage cost 30: 30,000 of exposure on 9,970 of equity, a little over 3×.
    assert engine.cash == D(9970) and engine.max_leverage == D(30000) / D(9970)


@check
def a_long_pays_funding_and_a_short_receives_it():
    book = CandleBook(flat("BTC-PERP", 100) + flat("ETH-PERP", 10))
    funding = FundingBook([("BTC-PERP", T0 + H, D("0.0001")), ("ETH-PERP", T0 + H, D("0.0001"))])
    long_, short = PerpsEngine(book, funding), PerpsEngine(book, funding)
    long_.apply(sig(10, {"BTC-PERP": 1}))
    short.apply(sig(10, {"ETH-PERP": -1}))
    # Long 100 BTC-PERP: bought at 100.05, value 10,005, fee 5.0025.
    assert long_.cash == D("-10.0025")
    long_.pay_funding(T0 + H)
    short.pay_funding(T0 + H)
    # 100 × 100 × 0.0001 = 1 paid; the short of −1,000 ETH-PERP at 10 receives 1.
    assert long_.cash == D("-11.0025") and long_.payments[0].amount == D(1)
    assert short.payments[0].amount == D(-1)


@check
def three_x_long_is_liquidated_when_the_low_crosses_the_line():
    # Bought 300 at 100.05: cash 10,000 − 30,015 − 15.0075 = −20,030.0075.
    # Liquidation when −20,030.0075 + 300 × low ≤ 5% × 300 × low, so low ≤ 70.28…
    hit = T0 + 30 * M
    for low, liquidated in ((71, False), (70, True)):
        book = CandleBook(flat("BTC-PERP", 100, lows={hit: low}))
        result = replay_perps([sig(10, {"BTC-PERP": 3})], book, no_funding("BTC-PERP"), until=T0 + 60 * M)
        assert bool(result.liquidations) == liquidated, (low, result.liquidations)  # 13:00 ends the stretch
    # At 70: sold 300 at 70 × 0.9995 = 69.965, value 20,989.5, fee 10.49475.
    [buy, sell] = result.fills
    assert (sell.signal_id, sell.time, sell.side, sell.price, sell.value) == ("liquidation", hit, "sell", D("69.965"), D("20989.5"))
    assert result.final.equity == D("948.99775") and not result.wiped_out


@check
def a_loss_past_the_collateral_wipes_the_bot_out():
    hit = T0 + 30 * M
    book = CandleBook(flat("BTC-PERP", 100, lows={hit: 60}))
    signals = [sig(10, {"BTC-PERP": 3}), sig(3600, {"BTC-PERP": 1}, "later")]
    result = replay_perps(signals, book, no_funding("BTC-PERP"), until=T0 + 120 * M)
    # Sold at 59.97: −20,030.0075 + 17,991 − 8.9955 = −2,048.003, which ends at 0.
    assert result.wiped_out and result.final.equity == 0
    assert [f.signal_id for f in result.fills] == ["s", "liquidation"]  # the later signal can't trade


@check
def a_wiped_out_bot_still_gets_its_metrics():
    # Wiped out on day one, then marked at 0 every midnight: the days after
    # 0 return 0, they don't divide by it.
    hit = T0 + 30 * M
    book = CandleBook(flat("BTC-PERP", 100, minutes=3 * 1440, lows={hit: 60}))
    result = replay_perps([sig(10, {"BTC-PERP": 3})], book, no_funding("BTC-PERP", hours=80), until=T0 + 60 * H)
    assert [m.equity for m in result.marks] == [0, 0, 0]
    assert result.metrics.total_return == -1 and result.metrics.max_drawdown == -1


@check
def a_short_is_liquidated_on_the_high():
    hit = T0 + 20 * M
    book = CandleBook(flat("BTC-PERP", 100, highs={hit: 140}))
    result = replay_perps([sig(10, {"BTC-PERP": -3})], book, no_funding("BTC-PERP"), until=T0 + 60 * M)
    # Short 300 at 99.95: cash 10,000 + 29,985 − 14.9925 = 39,970.0075. At 140:
    # 39,970.0075 − 42,000 < 5% × 42,000, so it closes, buying 300 at 140.07.
    [_, close] = result.fills
    assert (close.side, close.price, close.time) == ("buy", D("140.07"), hit)
    assert result.wiped_out and result.final.equity == 0


@check
def a_stretch_combines_each_positions_worst_price():
    # Long 150 BTC-PERP and short 1,500 ETH-PERP: cash 9,970 (see above).
    # BTC's low of 60 (minute 10) or ETH's high of 14 (minute 40) alone leaves
    # equity of 3,970, above the margin; within one stretch they combine:
    # 9,970 + 150 × 60 − 1,500 × 14 = −2,030. The liquidation is dated minute 40.
    low_at, high_at = T0 + 10 * M, T0 + 40 * M
    book = CandleBook(flat("BTC-PERP", 100, lows={low_at: 60}) + flat("ETH-PERP", 10, highs={high_at: 14}))
    assert book.extremes("BTC-PERP", T0, T0 + H) == ((D(60), low_at), (D(100), T0))
    result = replay_perps([sig(10, {"BTC-PERP": "1.5", "ETH-PERP": "-1.5"})], book,
                          no_funding("BTC-PERP", "ETH-PERP"), until=T0 + 60 * M)
    [liquidation] = result.liquidations
    assert (liquidation.time, liquidation.equity) == (high_at, D(-2030))
    assert result.wiped_out and {f.time for f in result.fills if f.signal_id == "liquidation"} == {high_at}


@check
def a_stretch_is_checked_once_it_is_over():
    # The low that liquidates comes at 12:30. Replayed at 12:45, the stretch
    # from 12:01 isn't over yet: no liquidation. At 13:05 it is, and the
    # earlier record is the start of the later one.
    hit = T0 + 30 * M
    book = CandleBook(flat("BTC-PERP", 100, minutes=90, lows={hit: 70}))
    early = replay_perps([sig(10, {"BTC-PERP": 3})], book, no_funding("BTC-PERP"), until=T0 + 45 * M)
    late = replay_perps([sig(10, {"BTC-PERP": 3})], book, no_funding("BTC-PERP"), until=T0 + 65 * M)
    assert early.liquidations == [] and [l.time for l in late.liquidations] == [hit]
    assert late.fills[:len(early.fills)] == early.fills
    assert early.final.time == T0 + 45 * M   # valued where the data ends all the same


@check
def the_benchmark_is_one_x_long_btc_perp_with_funding():
    book = CandleBook(flat("BTC-PERP", 100, minutes=26 * 60) + flat("ETH-PERP", 10, minutes=26 * 60))
    funding = FundingBook((s, T0 - 5 * H + i * H, D("0.0001")) for s in ("BTC-PERP", "ETH-PERP") for i in range(40))
    result = replay_perps([sig(10, {"ETH-PERP": -1})], book, funding, until=T0 + 13 * H)
    [bench] = result.benchmark_fills
    assert (bench.symbol, bench.side, bench.quantity) == ("BTC-PERP", "buy", D(100))
    # 00:00 UTC comes 12 hours after T0: one mark for each. Funding from 13:00 to 01:00 is 13 hours.
    assert [m.time for m in result.marks] == [T0 + 12 * H]
    assert len(result.payments) == 13 and result.funding_paid == D(-13)   # the short received 1 an hour
    assert result.benchmark[0].equity < result.marks[0].equity
    assert result.benchmark_final.time == result.final.time == T0 + 13 * H


@check
def missing_funding_stops_the_record_and_a_gap_counts_as_zero():
    book = CandleBook(flat("BTC-PERP", 100, minutes=6 * 60))
    # Rates for 13:00 and 15:00 only: 14:00 is a gap (0), 16:00 isn't collected yet.
    funding = FundingBook([("BTC-PERP", T0 + H, D("0.001")), ("BTC-PERP", T0 + 3 * H, D("0.001"))])
    result = replay_perps([sig(10, {"BTC-PERP": 1}), sig(5 * 3600, {"BTC-PERP": 0}, "close")], book, funding)
    assert [(p.time, p.rate) for p in result.payments] == [(T0 + H, D("0.001")), (T0 + 2 * H, D(0)), (T0 + 3 * H, D("0.001"))]
    assert [s.signal_id for s in result.pending] == ["close"]
    assert result.final.time == T0 + 4 * H


@check
def retiring_closes_shorts_too():
    book = CandleBook(flat("BTC-PERP", 100))
    result = replay_perps([sig(10, {"BTC-PERP": -1})], book, no_funding("BTC-PERP"),
                          until=T0 + 60 * M, retired_at=T0 + 30 * M + timedelta(seconds=5))
    assert [(f.signal_id, f.side) for f in result.fills] == [("s", "sell"), ("retired", "buy")]
    assert result.final.positions == {} and result.metrics.status == "retired"


@check
def time_in_market_counts_a_short():
    book = CandleBook(flat("BTC-PERP", 100))
    engine = PerpsEngine(book, no_funding("BTC-PERP"))
    engine.apply(sig(10, {"BTC-PERP": -1}))
    engine.apply(sig(30 * 60 + 10, {"BTC-PERP": 0}))
    share = time_in_market(engine.fills, T0 + M, T0 + 61 * M)
    assert abs(share - D(1) / 2) < D("1e-20")   # 30 of 60 minutes, short


@check
def the_same_inputs_give_the_same_numbers():
    rng = random.Random(7)
    prices = [100 + rng.randint(-20, 20) / 10 for _ in range(300)]
    data = candles("BTC-PERP", prices) + candles("ETH-PERP", [p / 10 for p in prices])
    rates = [(s, T0 - 5 * H + i * H, D(rng.randint(-5, 5)) / 100000) for s in ("BTC-PERP", "ETH-PERP") for i in range(12)]
    signals = [sig(10 + 600 * k, {"BTC-PERP": rng.choice([-2, -1, 0, 1, 2]), "ETH-PERP": rng.choice([-1, 0, 1])}, f"s{k}")
               for k in range(20)]
    runs = []
    for _ in range(2):
        shuffled = data[:]
        rng.shuffle(shuffled)
        result = replay_perps(signals, CandleBook(shuffled), FundingBook(rates), until=T0 + 290 * M)
        runs.append([(f.symbol, f.side, f.quantity, f.price) for f in result.fills] + [result.final.equity, result.funding_paid])
    assert runs[0] == runs[1]


@check
def the_replay_perps_command_reads_and_writes_csv():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        with open(root / "signals.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["signal_id", "received_at", "targets"])
            w.writerow(["s1", (T0 + timedelta(seconds=10)).isoformat(), json.dumps({"BTC-PERP": -1})])
        with open(root / "candles.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["symbol", "open_time", "open", "high", "low", "close"])
            for c in flat("BTC-PERP", 100, minutes=120):
                w.writerow([c.symbol, c.open_time.isoformat(), c.open, c.high, c.low, c.close])
        with open(root / "funding.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["symbol", "time", "rate"])
            w.writerow(["BTC-PERP", (T0 + H).isoformat(), "0.0001"])
            w.writerow(["BTC-PERP", (T0 + 2 * H).isoformat(), "0.0001"])
        out = io.StringIO()
        with redirect_stdout(out):
            code = perps_main([str(root / "signals.csv"), str(root / "candles.csv"), "--funding",
                               str(root / "funding.csv"), "--out", str(root / "out")])
        assert code == 0, out.getvalue()
        metrics = json.loads((root / "out" / "metrics.json").read_text())
        assert metrics["funding_paid"] == "-1" and metrics["liquidations"] == []
        rows = list(csv.DictReader(open(root / "out" / "funding.csv")))
        assert rows[0]["amount"] == "-1"


@check
def spot_rules_are_unchanged():
    assert (rules.FEE_RATE, rules.SLIPPAGE_MAJOR, rules.SLIPPAGE_OTHER, rules.START_CASH) == (
        D("0.001"), D("0.0005"), D("0.0015"), D(10000))
    assert rules.perps_slippage("BTC-PERP") == D("0.0005") and rules.perps_slippage("SOL-PERP") == D("0.0015")


def main() -> int:
    failed = 0
    for function in CHECKS:
        try:
            function()
            print(PASS_TOKEN + function.__name__)
        except Exception:
            failed += 1
            print(FAIL_TOKEN + function.__name__)
            traceback.print_exc(file=sys.stdout)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
