"""
tests/test_engine.py — the fill engine (engine/), against hand-computed examples.

Every expected number below is worked out by hand from the rules in
engine/rules.py (spec 5.2 of the leaderboard), so a change in the engine's
arithmetic shows up here. No network, no packages beyond the standard library.
Battery protocol: one PASS or FAIL line per check, non-zero exit on failure.
Run it with:  python3 tests/test_engine.py
"""

from __future__ import annotations

import ast
import csv
import importlib.util
import io
import json
import random
import sys
import sysconfig
import tempfile
import traceback
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import rules  # noqa: E402
from engine.candles import Candle, CandleBook, load_candles_csv, parse_time  # noqa: E402
from engine.fills import Engine, NotReady, Signal, fill_minute  # noqa: E402
from engine.replay import load_signals_csv, main as replay_main, replay  # noqa: E402

PASS_TOKEN = "  " + "PASS" + "  "
FAIL_TOKEN = "  " + "FAIL" + "  "
T0 = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
M = timedelta(minutes=1)

CHECKS = []


def check(function):
    CHECKS.append(function)
    return function


def flat(symbol, price, start=T0 - 5 * M, minutes=30, skip=()):
    """Candles at one constant price: open = close = `price`."""
    price = D(str(price))
    return [Candle(symbol, start + i * M, price, price, price, price)
            for i in range(minutes) if start + i * M not in skip]


def close(a, b, tolerance=D("1e-20")):
    """Equal but for the 28th significant digit: the same amount computed in
    another order of operations."""
    return abs(a - b) <= tolerance * max(abs(a), abs(b), D(1))


def book(*candle_lists):
    return CandleBook(c for candles in candle_lists for c in candles)


def signal(seconds_after_t0, targets, signal_id="s"):
    return Signal(signal_id, T0 + timedelta(seconds=seconds_after_t0),
                  {k: D(str(v)) for k, v in targets.items()})


@check
def a_signal_fills_at_the_open_of_the_next_minute():
    assert fill_minute(T0 + timedelta(seconds=41)) == T0 + M
    assert fill_minute(T0 + timedelta(seconds=59, microseconds=999999)) == T0 + M
    # Exactly on the minute: that candle doesn't open *after* t, so the next one.
    assert fill_minute(T0) == T0 + M


@check
def a_first_buy_pays_slippage_and_fee():
    # 50% of 10,000 in BTC at 100: 50 BTC at 100.05 (5 bps), fee 0.10%.
    btc = flat("BTC/USDT", 100)
    engine = Engine(book(btc))
    [fill] = engine.apply(signal(30, {"BTC/USDT": 0.5}))
    assert fill.time == T0 + M and fill.side == "buy"
    assert fill.quantity == D(50)
    assert fill.reference_price == D(100) and fill.price == D("100.05")
    assert fill.value == D("5002.50") and fill.fee == D("5.00250")
    assert fill.slippage_cost == D("2.50")
    assert engine.portfolio.cash == D("4992.4975")
    assert engine.portfolio.positions == {"BTC/USDT": D(50)}


@check
def other_pairs_pay_15_bps():
    engine = Engine(book(flat("SOL/USDT", 200)))
    [fill] = engine.apply(signal(30, {"SOL/USDT": 0.1}))
    assert fill.quantity == D(5) and fill.price == D("200.30")


@check
def a_small_change_is_skipped_but_zero_always_closes():
    engine = Engine(book(flat("BTC/USDT", 100, minutes=60)))
    engine.apply(signal(30, {"BTC/USDT": 0.5}))
    # 0.5004 of ~9,997.5 equity is ~4 USDT more: under 0.1% of equity, skipped.
    assert engine.apply(signal(90, {"BTC/USDT": 0.5004})) == []
    # Back to a tiny target 0: the whole position goes.
    [fill] = engine.apply(signal(150, {}))
    assert fill.side == "sell" and fill.quantity == D(50)
    assert engine.portfolio.positions == {}
    # 50 × 99.95 = 4997.5, minus the 0.10% fee 4.9975.
    assert engine.portfolio.cash == D("4992.4975") + D("4997.5") - D("4.9975")


@check
def sells_come_first_and_buys_never_borrow():
    # BTC at 100 from 11:55 to 11:59, then 110; ETH at 50 all along.
    btc = flat("BTC/USDT", 100, minutes=5) + flat("BTC/USDT", 110, start=T0, minutes=30)
    eth = flat("ETH/USDT", 50, minutes=40)
    engine = Engine(book(btc, eth))
    # All in BTC: 100 BTC would cost 100 × 100.05 × 1.001 = 10,015.005 > 10,000,
    # so the buy shrinks to exactly the cash.
    [buy] = engine.apply(signal(-170, {"BTC/USDT": 1}))  # received 11:57:10, fills 11:58 at 100
    assert buy.reference_price == D(100)
    assert close(buy.quantity, D(10000) / (D("100.05") * D("1.001")))
    assert engine.portfolio.cash == 0
    # Switch to ETH when BTC is at 110: sell all BTC first, then buy ETH with it.
    sell, buy_eth = engine.apply(signal(60, {"ETH/USDT": 1}))  # fills 12:02
    assert (sell.symbol, sell.side, buy_eth.symbol, buy_eth.side) == ("BTC/USDT", "sell", "ETH/USDT", "buy")
    assert sell.reference_price == D(110)
    cash_after_sell = sell.value - sell.fee
    assert close(buy_eth.quantity, cash_after_sell / (D("50.025") * D("1.001")))
    assert engine.portfolio.cash == 0
    assert engine.portfolio.positions == {"ETH/USDT": buy_eth.quantity}


@check
def a_missing_minute_fills_at_the_next_candle():
    btc = flat("BTC/USDT", 100, skip={T0 + M, T0 + 2 * M})
    later = Candle("BTC/USDT", T0 + 3 * M, D(101), D(101), D(101), D(101))
    candles = [c for c in btc if c.open_time != T0 + 3 * M] + [later]
    [fill] = Engine(book(candles)).apply(signal(30, {"BTC/USDT": 0.5}))
    assert fill.time == T0 + 3 * M and fill.reference_price == D(101)


@check
def a_signal_waits_for_its_candle():
    candles = flat("BTC/USDT", 100, minutes=6)  # last candle opens at 12:00
    engine = Engine(book(candles))
    try:
        engine.apply(signal(30, {"BTC/USDT": 0.5}))  # needs 12:01
    except NotReady as exc:
        assert exc.symbol == "BTC/USDT" and exc.when == T0 + M
    else:
        raise AssertionError("expected NotReady")
    assert engine.portfolio.cash == rules.START_CASH and engine.fills == []


@check
def daily_marks_use_the_last_close_before_midnight():
    midnight = datetime(2026, 10, 14, tzinfo=timezone.utc)
    start = midnight - 3 * M
    # 23:57 open 100; 23:58 and 23:59 close at 120; 00:00 opens at 130.
    candles = [Candle("BTC/USDT", start, D(100), D(100), D(100), D(100)),
               Candle("BTC/USDT", start + M, D(100), D(120), D(100), D(120)),
               Candle("BTC/USDT", start + 2 * M, D(120), D(120), D(120), D(120)),
               Candle("BTC/USDT", midnight, D(130), D(130), D(130), D(130))]
    sig = Signal("s", start - timedelta(seconds=30), {"BTC/USDT": D("0.5")})  # fills 23:57 at 100
    result = replay([sig], book(candles))
    [mark] = result.marks
    assert mark.time == midnight
    assert mark.equity == D("4992.4975") + 50 * D(120)
    # Buy-and-hold BTC from the same minute, same slippage and fee.
    btc_quantity = D(10000) / (D("100.05") * D("1.001"))
    assert result.benchmark[0].equity == btc_quantity * 120


@check
def a_fill_at_midnight_comes_after_that_days_mark():
    midnight = datetime(2026, 10, 14, tzinfo=timezone.utc)
    candles = flat("BTC/USDT", 100, start=midnight - 10 * M, minutes=20)
    early = Signal("a", midnight - 5 * M, {"BTC/USDT": D("0.5")})
    at_midnight = Signal("b", midnight - timedelta(seconds=20), {})  # fills 00:00
    result = replay([early, at_midnight], book(candles))
    assert result.marks[0].positions == {"BTC/USDT": D(50)}
    assert result.fills[-1].time == midnight and result.fills[-1].side == "sell"


@check
def retiring_closes_everything_at_the_next_candle():
    candles = flat("BTC/USDT", 100, minutes=60)
    sigs = [signal(30, {"BTC/USDT": 0.5}, "a"), signal(600, {"BTC/USDT": 0.9}, "late")]
    result = replay(sigs, book(candles), retired_at=T0 + timedelta(seconds=200))
    assert [f.signal_id for f in result.fills] == ["a", "retired"]
    assert result.fills[-1].time == T0 + 4 * M
    assert result.final.positions == {}


@check
def a_replay_without_btc_candles_has_no_benchmark_but_still_runs():
    candles = flat("ETH/USDT", 50, start=T0, minutes=2 * 24 * 60)
    sigs = [signal(30, {"ETH/USDT": 0.5}, "a"), signal(3600, {"ETH/USDT": 0.2}, "b")]
    result = replay(sigs, book(candles))
    assert result.pending == [] and result.filled_signals == 2 and len(result.fills) == 2
    assert len(result.marks) == 2 and result.benchmark == []


@check
def signals_after_retirement_are_not_counted_as_filled():
    candles = flat("BTC/USDT", 100, minutes=60)
    sigs = [signal(30, {"BTC/USDT": 0.5}, "a"), signal(600, {"BTC/USDT": 0.9}, "late")]
    result = replay(sigs, book(candles), retired_at=T0 + timedelta(seconds=200))
    assert result.filled_signals == 1


@check
def the_same_inputs_give_the_same_numbers():
    rng = random.Random(7)
    candles = []
    for symbol, base in (("BTC/USDT", 60000), ("ETH/USDT", 3000), ("SOL/USDT", 150)):
        price = D(base)
        for i in range(3 * 24 * 60):
            step = D(rng.randint(-30, 30)) / D(10000)
            new = (price * (1 + step)).quantize(D("0.01"))
            candles.append(Candle(symbol, T0 + i * M, price, max(price, new), min(price, new), new))
            price = new
    sigs = []
    for k in range(40):
        weights = [rng.randint(0, 40) for _ in range(3)]
        sigs.append(Signal(f"s{k}", T0 + timedelta(minutes=rng.randint(0, 4000), seconds=rng.randint(0, 59)),
                           {s: D(w) / 100 for s, w in zip(("BTC/USDT", "ETH/USDT", "SOL/USDT"), weights) if w}))
    first = replay(sigs, book(candles))
    shuffled = candles[:]
    rng.shuffle(shuffled)
    second = replay(list(reversed(sigs)), CandleBook(shuffled))
    assert first.fills == second.fills and first.marks == second.marks
    assert len(first.fills) > 40 and len(first.marks) == 3  # midnights of the 14th, 15th and 16th
    for mark in first.marks:
        assert mark.cash >= 0 and all(q > 0 for q in mark.positions.values())


@check
def the_replay_command_reads_and_writes_csv():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        # One file in the engine's format, one written the way fetch_candles.py does.
        with open(tmp / "candles.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["symbol", "open_time", "open", "high", "low", "close", "volume"])
            for c in flat("BTC/USDT", 100, minutes=2 * 24 * 60):
                w.writerow([c.symbol, c.open_time.isoformat(), c.open, c.high, c.low, c.close, 1])
        with open(tmp / "ETH-USDT_1m.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["time_utc", "open", "high", "low", "close", "volume"])
            for c in flat("ETH/USDT", 50, minutes=2 * 24 * 60):
                w.writerow([c.open_time.strftime("%Y-%m-%d %H:%M"), c.open, c.high, c.low, c.close, 1])
        with open(tmp / "signals.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["signal_id", "received_at", "targets"])
            w.writerow(["sig_1", "2026-10-13T12:00:30.123Z", json.dumps({"BTC/USDT": 0.5})])
            w.writerow(["sig_2", "2026-10-13T18:00:00Z", json.dumps({"BTC/USDT": 0.5, "ETH/USDT": 0.25})])
        out = io.StringIO()
        with redirect_stdout(out):
            code = replay_main([str(tmp / "signals.csv"), str(tmp / "candles.csv"),
                                str(tmp / "ETH-USDT_1m.csv"), "--out", str(tmp / "out")])
        assert code == 0, out.getvalue()
        assert "2 of 2 signals filled, 2 fills." in out.getvalue()
        with open(tmp / "out" / "fills.csv", newline="") as f:
            fills = list(csv.DictReader(f))
        assert [(r["signal_id"], r["symbol"], r["quantity"], r["price"]) for r in fills] == [
            ("sig_1", "BTC/USDT", "50", "100.05"), ("sig_2", "ETH/USDT", fills[1]["quantity"], "50.025")]
        with open(tmp / "out" / "equity.csv", newline="") as f:
            equity = list(csv.DictReader(f))
        assert [r["time"] for r in equity] == ["2026-10-14T00:00:00Z", "2026-10-15T00:00:00Z"]
        # The library gives the same thing as the command.
        result = replay(load_signals_csv(tmp / "signals.csv"),
                        CandleBook(load_candles_csv(tmp / "candles.csv") + load_candles_csv(tmp / "ETH-USDT_1m.csv")))
        assert D(fills[1]["quantity"]) == result.fills[1].quantity.normalize()


@check
def times_parse_in_every_written_form():
    for text in ("2026-10-13T12:00:41Z", "2026-10-13T12:00:41.123Z", "2026-10-13 12:00",
                 "2026-10-13T14:00:41+02:00"):
        assert parse_time(text).tzinfo is not None
    assert parse_time("2026-10-13T14:00:41+02:00") == T0 + timedelta(seconds=41)


@check
def the_engine_uses_only_the_standard_library():
    stdlib = {Path(sysconfig.get_paths()[k]).resolve() for k in ("stdlib", "platstdlib")}
    for path in sorted((ROOT / "engine").glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module.split(".")[0]]
            for module in names:
                if module == "__future__":
                    continue
                spec = importlib.util.find_spec(module)
                origin = (spec.origin or "") if spec else ""
                builtin = origin in ("built-in", "frozen")
                in_stdlib = any(folder in Path(origin).resolve().parents for folder in stdlib) \
                    and "site-packages" not in origin
                assert builtin or in_stdlib, f"{path.name} imports {module}, which is not in the standard library"


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
