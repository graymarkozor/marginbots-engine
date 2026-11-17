"""
engine/metrics.py — the numbers on a bot page, from its daily equity.

Every formula here is the one the MarginBots methodology page shows (spec,
section 5.4). The equity curve is the starting capital at the bot's first
fill, then the bot's value at each 00:00 UTC (fills.Engine.mark). Daily
returns are the changes between consecutive points of that curve.

  return        equity_today ÷ 10,000 − 1
  max drawdown  min over t of (equity_t ÷ peak_t − 1), peak_t = highest equity up to t
  volatility    stdev(daily returns) × √365      (sample stdev, n − 1)
  Sharpe        mean(daily returns) ÷ stdev(daily returns) × √365
  Sortino       mean(daily returns) ÷ downside_dev × √365,
                downside_dev = √(mean of min(r, 0)²)
  Calmar        annualized ÷ |max drawdown|,
                annualized = (1 + return) ^ (365 ÷ days) − 1

Annualized return, volatility, Sharpe, Sortino and Calmar are given only
once a bot has 90 days and 20 fills: annualizing a few weeks of data is
misleading. A ratio whose divisor is 0 (no volatility, no losing day, no
drawdown) is left empty rather than shown as infinite. The risk-free rate
is 0.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext

from . import rules
from .fills import Fill, Mark

ANNUALIZATION_DAYS = 365
RANK_MIN_DAYS = 90
RANK_MIN_FILLS = 20
ZERO = Decimal(0)
ONE = Decimal(1)


@dataclass(frozen=True)
class Metrics:
    status: str                       # "provisional", "ranked" or "retired"
    start: datetime                   # the first fill
    as_of: datetime                   # the last daily mark
    days_live: int                    # whole days from start to as_of
    fills: int
    costs_paid: Decimal               # fees + slippage, in USDT
    time_in_market: Decimal           # share of the time holding anything, 0 to 1
    total_return: Decimal
    max_drawdown: Decimal             # 0 or negative
    current_drawdown: Decimal         # 0 or negative
    # After 90 days and 20 fills only; None before, or when a divisor is 0.
    annualized_return: Decimal | None = None
    volatility: Decimal | None = None
    sharpe: Decimal | None = None
    sortino: Decimal | None = None
    calmar: Decimal | None = None


def daily_returns(values: list[Decimal]) -> list[Decimal]:
    """A day after equity reached 0 (a Perps bot wiped out, engine/perps.py)
    changes nothing: its return is 0."""
    return [values[i] / values[i - 1] - 1 if values[i - 1] else ZERO for i in range(1, len(values))]


def drawdowns(values: list[Decimal]) -> list[Decimal]:
    """equity_t ÷ peak_t − 1 for every point: 0 at a new high, negative below it."""
    out, peak = [], None
    for value in values:
        peak = value if peak is None or value > peak else peak
        out.append(value / peak - 1)
    return out


def mean(xs: list[Decimal]) -> Decimal:
    return sum(xs, ZERO) / len(xs)


def sample_stdev(xs: list[Decimal]) -> Decimal | None:
    if len(xs) < 2:
        return None
    m = mean(xs)
    return (sum(((x - m) ** 2 for x in xs), ZERO) / (len(xs) - 1)).sqrt()


def downside_deviation(xs: list[Decimal]) -> Decimal | None:
    if not xs:
        return None
    return mean([min(x, ZERO) ** 2 for x in xs]).sqrt()


def elapsed_days(start: datetime, end: datetime) -> Decimal:
    delta = end - start
    seconds = Decimal(delta.days * 86400 + delta.seconds) + Decimal(delta.microseconds) / 1_000_000
    return seconds / 86400


def time_in_market(fills: list[Fill], start: datetime, end: datetime) -> Decimal:
    """The share of [start, end] during which the bot held at least one pair,
    long or short (engine/perps.py), rebuilt from its fills alone."""
    total = elapsed_days(start, end)
    if total <= 0:
        return ZERO
    held: dict[str, Decimal] = {}
    invested = ZERO
    since = None
    for fill in sorted(fills, key=lambda f: f.time):
        if fill.time > end:
            break
        before = any(q != 0 for q in held.values())
        held[fill.symbol] = held.get(fill.symbol, ZERO) + (fill.quantity if fill.side == "buy" else -fill.quantity)
        after = any(q != 0 for q in held.values())
        if not before and after:
            since = max(fill.time, start)
        elif before and not after and since is not None:
            invested += elapsed_days(since, fill.time)
            since = None
    if since is not None:
        invested += elapsed_days(since, end)
    return invested / total


def compute(start: datetime, marks: list[Mark], fills: list[Fill], retired: bool = False,
            start_cash: Decimal = rules.START_CASH) -> Metrics:
    """The metrics of one equity curve: the bot's, or buy-and-hold BTC's."""
    with localcontext(rules.DECIMAL_CONTEXT):
        points = [(start, start_cash)] + [(m.time, m.equity) for m in marks if m.time > start]
        as_of = points[-1][0]
        values = [value for _, value in points]
        returns = daily_returns(values)
        dd = drawdowns(values)
        days_live = (as_of - start).days
        counted = [f for f in fills if f.time <= as_of]
        total_return = values[-1] / start_cash - 1
        status = "retired" if retired else (
            "ranked" if days_live >= RANK_MIN_DAYS and len(counted) >= RANK_MIN_FILLS else "provisional")

        metrics = dict(
            status=status, start=start, as_of=as_of, days_live=days_live, fills=len(counted),
            costs_paid=sum((f.fee + f.slippage_cost for f in counted), ZERO),
            time_in_market=time_in_market(counted, start, as_of),
            total_return=total_return, max_drawdown=min(dd), current_drawdown=dd[-1],
        )
        if days_live >= RANK_MIN_DAYS and len(counted) >= RANK_MIN_FILLS:
            year = Decimal(ANNUALIZATION_DAYS).sqrt()
            annualized = (1 + total_return) ** (Decimal(ANNUALIZATION_DAYS) / elapsed_days(start, as_of)) - 1
            stdev = sample_stdev(returns)
            downside = downside_deviation(returns)
            average = mean(returns) if returns else None
            metrics.update(
                annualized_return=annualized,
                volatility=stdev * year if stdev is not None else None,
                sharpe=average / stdev * year if stdev else None,
                sortino=average / downside * year if downside else None,
                calmar=annualized / abs(metrics["max_drawdown"]) if metrics["max_drawdown"] < 0 else None,
            )
        return Metrics(**metrics)
