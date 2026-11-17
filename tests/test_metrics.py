"""
tests/test_metrics.py — the bot page metrics (engine/metrics.py), against hand-computed examples.

The expected values are worked out here from the methodology page's formulas
with plain floats, independently of the engine's decimal code.
Battery protocol: one PASS or FAIL line per check, non-zero exit on failure.
Run it with:  python3 tests/test_metrics.py
"""

from __future__ import annotations

import io
import json
import math
import sys
import tempfile
import traceback
import csv
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.candles import Candle, CandleBook  # noqa: E402
from engine.fills import Fill, Mark, Signal  # noqa: E402
from engine.metrics import compute, daily_returns, drawdowns, time_in_market  # noqa: E402
from engine.replay import main as replay_main, replay  # noqa: E402

PASS_TOKEN = "  " + "PASS" + "  "
FAIL_TOKEN = "  " + "FAIL" + "  "
START = datetime(2026, 1, 1, tzinfo=timezone.utc)
DAY = timedelta(days=1)

CHECKS = []


def check(function):
    CHECKS.append(function)
    return function


def near(actual, expected, rel=1e-9):
    assert actual is not None, f"expected {expected}, got None"
    assert math.isclose(float(actual), expected, rel_tol=rel, abs_tol=1e-12), f"{actual} != {expected}"


def marks_from(values, start=START):
    """Daily marks at the midnights after `start`, with these equities."""
    return [Mark(start + (i + 1) * DAY, D(str(v)), D(0), {}) for i, v in enumerate(values)]


def fill(time, side="buy", quantity="1", symbol="BTC/USDT", fee="1", slippage="0.5"):
    return Fill("s", time, symbol, side, D(quantity), D(100), D(100), D(100), D(fee), D(slippage))


def some_fills(n, start=START):
    return [fill(start + timedelta(hours=i)) for i in range(n)]


@check
def returns_and_drawdowns_start_from_the_capital():
    values = [D(10000), D(11000), D(9900), D(10890)]
    assert daily_returns(values) == [D("0.1"), D("-0.1"), D("0.1")]
    assert drawdowns(values) == [D(0), D(0), D("-0.1"), D("-0.01")]
    m = compute(START, marks_from([11000, 9900, 10890]), some_fills(3))
    assert m.total_return == D("0.089")
    assert m.max_drawdown == D("-0.1") and m.current_drawdown == D("-0.01")
    # A first mark below the capital is a drawdown from the capital.
    assert compute(START, marks_from([9000]), some_fills(1)).max_drawdown == D("-0.1")


@check
def a_young_bot_shows_no_yearly_figures():
    m = compute(START, marks_from([10000 + i for i in range(30)]), some_fills(25))
    assert m.status == "provisional" and m.days_live == 30
    assert (m.annualized_return, m.volatility, m.sharpe, m.sortino, m.calmar) == (None,) * 5
    # 90 days but only 19 fills: still provisional.
    m = compute(START, marks_from([10000 + i for i in range(95)]), some_fills(19))
    assert m.status == "provisional" and m.sharpe is None


@check
def sharpe_sortino_volatility_and_calmar_follow_the_page():
    # 100 days alternating +2% and -1%.
    values, equity = [], 10000.0
    for i in range(100):
        equity *= 1.02 if i % 2 == 0 else 0.99
        values.append(equity)
    m = compute(START, marks_from([round(v, 10) for v in values]), some_fills(25))
    assert m.status == "ranked" and m.days_live == 100 and m.fills == 25

    returns = [0.02 if i % 2 == 0 else -0.01 for i in range(100)]
    avg = sum(returns) / 100
    stdev = math.sqrt(sum((r - avg) ** 2 for r in returns) / 99)
    downside = math.sqrt(sum(min(r, 0) ** 2 for r in returns) / 100)
    total = (1.02 * 0.99) ** 50 - 1
    annualized = (1 + total) ** (365 / 100) - 1
    near(m.total_return, total)
    near(m.volatility, stdev * math.sqrt(365), rel=1e-7)
    near(m.sharpe, avg / stdev * math.sqrt(365), rel=1e-7)
    near(m.sortino, avg / downside * math.sqrt(365), rel=1e-7)
    near(m.max_drawdown, -0.01, rel=1e-7)
    near(m.annualized_return, annualized, rel=1e-7)
    near(m.calmar, annualized / 0.01, rel=1e-6)


@check
def a_ratio_with_nothing_to_divide_by_stays_empty():
    # Flat equity: no volatility, no losing day, no drawdown.
    m = compute(START, marks_from([10000] * 100), some_fills(25))
    assert m.status == "ranked"
    assert m.volatility == 0 and m.annualized_return == 0
    assert (m.sharpe, m.sortino, m.calmar) == (None, None, None)


@check
def time_in_market_comes_from_the_fills():
    fills = [fill(START, "buy", "2"), fill(START + DAY, "sell", "2"),
             fill(START + 2 * DAY, "buy", "1"), fill(START + 3 * DAY, "buy", "1", symbol="ETH/USDT")]
    # Held from day 0 to 1, then from day 2 to the end (day 4): 3 of 4 days.
    assert time_in_market(fills, START, START + 4 * DAY) == D("0.75")
    assert time_in_market([], START, START + 4 * DAY) == 0


@check
def costs_add_fees_and_slippage_up_to_the_last_mark():
    fills = some_fills(4) + [fill(START + 5 * DAY)]  # the last one comes after the last mark
    m = compute(START, marks_from([10000, 10000, 10000]), fills)
    assert m.fills == 4 and m.costs_paid == D("6.0")


@check
def a_retired_bot_says_so():
    m = compute(START, marks_from([10100]), some_fills(2), retired=True)
    assert m.status == "retired"


@check
def the_replay_gives_metrics_for_the_bot_and_for_btc():
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    candles = []
    for i in range(3 * 1440):
        t = start + timedelta(minutes=i)
        btc, eth = D(60000) + D(i) / 2, D(3000) - D(i) / 20
        candles += [Candle("BTC/USDT", t, btc, btc, btc, btc), Candle("ETH/USDT", t, eth, eth, eth, eth)]
    sigs = [Signal("a", start + timedelta(hours=6, seconds=41), {"BTC/USDT": D("0.6"), "ETH/USDT": D("0.4")}),
            Signal("b", start + timedelta(days=1, hours=12, minutes=30), {"BTC/USDT": D(1)})]
    result = replay(sigs, CandleBook(candles))
    m, b = result.metrics, result.benchmark_metrics
    assert m.start == start + timedelta(hours=6, minutes=1) and b.start == m.start
    assert m.fills == 4 and b.fills == 1 and b.time_in_market == 1
    assert m.as_of == datetime(2026, 10, 4, tzinfo=timezone.utc) and m.days_live == 2
    assert m.total_return == result.marks[-1].equity / 10000 - 1
    assert b.total_return == result.benchmark[-1].equity / 10000 - 1

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        with open(tmp / "candles.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["symbol", "open_time", "open", "high", "low", "close"])
            for c in candles:
                w.writerow([c.symbol, c.open_time.isoformat(), c.open, c.high, c.low, c.close])
        with open(tmp / "signals.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["signal_id", "received_at", "targets"])
            for s in sigs:
                w.writerow([s.signal_id, s.received_at.isoformat(), json.dumps({k: str(v) for k, v in s.targets.items()})])
        out = io.StringIO()
        with redirect_stdout(out):
            code = replay_main([str(tmp / "signals.csv"), str(tmp / "candles.csv"), "--out", str(tmp / "out")])
        assert code == 0, out.getvalue()
        assert "Provisional, 2 days, 4 fills" in out.getvalue()
        saved = json.loads((tmp / "out" / "metrics.json").read_text())
        assert saved["bot"]["status"] == "provisional" and saved["bot"]["sharpe"] is None
        assert D(saved["bot"]["total_return"]) == m.total_return.normalize()
        assert saved["btc_buy_and_hold"]["fills"] == "1"


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
