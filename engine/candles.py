"""
engine/candles.py — the public 1-minute candles the engine prices fills with.

A candle summarises one minute of trading: open, high, low, close, volume
(see data/fetch_candles.py). The engine only ever needs three questions
answered about them, and CandleBook answers them from a list in memory:

  - the first candle of a pair that opens at or after a time (fills);
  - the price of a pair at a time (valuing what a bot holds);
  - whether the data has reached a time yet (a fill must wait for it);
  - the lowest and highest price of a pair over a stretch (perps.py's
    liquidation check).

The leaderboard's server answers the same three questions from its
database; any object with these methods works with the engine.
"""

from __future__ import annotations

import bisect
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class Candle:
    symbol: str          # "BTC/USDT"
    open_time: datetime  # UTC, on the minute
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


class CandleBook:
    """Candles in memory, sorted per pair, for fast lookups."""

    def __init__(self, candles: Iterable[Candle]):
        by_symbol: dict[str, dict[datetime, Candle]] = {}
        for candle in candles:
            by_symbol.setdefault(candle.symbol, {})[candle.open_time] = candle
        self._candles = {s: [c[t] for t in sorted(c)] for s, c in by_symbol.items()}
        self._times = {s: [c.open_time for c in cs] for s, cs in self._candles.items()}

    def symbols(self) -> list[str]:
        return sorted(self._candles)

    def first_at_or_after(self, symbol: str, when: datetime) -> Candle | None:
        times = self._times.get(symbol, [])
        i = bisect.bisect_left(times, when)
        return self._candles[symbol][i] if i < len(times) else None

    def last_before(self, symbol: str, when: datetime) -> Candle | None:
        times = self._times.get(symbol, [])
        i = bisect.bisect_left(times, when)
        return self._candles[symbol][i - 1] if i > 0 else None

    def price_at(self, symbol: str, when: datetime) -> Decimal | None:
        """The open of the candle that starts at `when`; if the exchange has
        no candle for that minute, the close of the last one before it."""
        candle = self.first_at_or_after(symbol, when)
        if candle is not None and candle.open_time == when:
            return candle.open
        before = self.last_before(symbol, when)
        return before.close if before is not None else None

    def extremes(self, symbol: str, start: datetime, end: datetime):
        """The lowest low and highest high of the candles in [start, end), each
        with the open time of the first candle that reached it:
        ((low, low_time), (high, high_time)), or None without a candle."""
        times = self._times.get(symbol, [])
        i, j = bisect.bisect_left(times, start), bisect.bisect_left(times, end)
        if i >= j:
            return None
        span = self._candles[symbol][i:j]
        low = min(span, key=lambda c: (c.low, c.open_time))
        high = min(span, key=lambda c: (-c.high, c.open_time))
        return (low.low, low.open_time), (high.high, high.open_time)

    def has_data_at(self, symbol: str, when: datetime) -> bool:
        """True once a candle at or after `when` exists: the data has reached
        that minute, so nothing about it can still change."""
        return self.first_at_or_after(symbol, when) is not None

    def last_time(self) -> datetime | None:
        ends = [times[-1] for times in self._times.values() if times]
        return max(ends) if ends else None


def parse_time(text: str) -> datetime:
    """UTC times written as ISO 8601 ("2026-10-13T12:01:00Z", with or without
    seconds and fractions) or the way fetch_candles.py writes them
    ("2026-10-13 12:01")."""
    text = text.strip().replace(" ", "T")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    value = datetime.fromisoformat(text)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def load_candles_csv(path: str | Path) -> list[Candle]:
    """Read candles from a CSV with the columns
    symbol, open_time, open, high, low, close (volume and others ignored).

    A file written by data/fetch_candles.py has no symbol column: the pair is
    then read from its name, so "BTC-USDT_1m.csv" holds BTC/USDT."""
    path = Path(path)
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    candles = []
    for row in rows:
        symbol = row.get("symbol") or path.name.split("_")[0].replace("-", "/")
        when = row.get("open_time") or row.get("time_utc")
        if not when:
            raise ValueError(f"{path}: each row needs an open_time (or time_utc) column")
        candles.append(Candle(
            symbol=symbol.strip().upper(),
            open_time=parse_time(when),
            open=Decimal(row["open"]),
            high=Decimal(row["high"]),
            low=Decimal(row["low"]),
            close=Decimal(row["close"]),
        ))
    return candles
