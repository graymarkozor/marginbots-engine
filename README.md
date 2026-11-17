# MarginBots engine

The fill engine of [MarginBots](https://marginbots.com), the leaderboard of
paper-traded trading bots. It turns a bot's signals into fills, daily equity
and metrics, from public 1-minute candles only, with the fees and slippage
MarginBots publishes. The leaderboard runs this code; you can run it too and
get the same numbers.

It uses only Python's standard library (Python 3.9 or newer): nothing to
install.

## Check a bot's record yourself

Every bot page on MarginBots offers the files its record was computed from,
up to 24 hours ago (the public log's delay). Replace `SLUG` with the last
part of the bot's address, for example `btc-trend-20-50`.

```bash
git clone https://github.com/graymarkozor/marginbots-engine
```

```bash
cd marginbots-engine
```

```bash
curl -sO https://api.marginbots.com/v1/public/bots/SLUG/signals.csv
```

```bash
curl -sO https://api.marginbots.com/v1/public/bots/SLUG/candles.csv
```

```bash
python3 -m engine replay signals.csv candles.csv --out my-check
```

`my-check/` then holds:

- `fills.csv`: every fill, in order;
- `equity.csv`: the bot's equity at each 00:00 UTC, next to buy-and-hold BTC
  started at the bot's first fill with the same slippage and fee;
- `metrics.json`: the figures of the bot's page, for the bot and for BTC.

Compare them with the bot's page. The same signals and candles always give
the same fills and the same equity, to the last digit.

For a bot that was retired, add `--retired-at` with the time shown on its
page. For a bot of the Perps division, download `funding.csv` the same way
and run:

```bash
python3 -m engine replay-perps signals.csv candles.csv --funding funding.csv --out my-check
```

## The rules

All the numbers of the fill model are in [engine/rules.py](engine/rules.py);
the [methodology page](https://marginbots.com/methodology) explains them.

- A signal gives the bot's target weights. It is filled at the open of the
  next 1-minute candle after the server received it.
- Slippage is always against the bot: 5 bps for BTC and ETH, 15 bps for the
  other pairs. Every fill pays a 0.10% fee.
- Every bot starts with 10,000 USDT of virtual cash. Changes smaller than
  0.1% of equity are skipped.
- Equity is marked once a day at 00:00 UTC. Yearly figures appear after 90
  days and 20 fills.
- The Perps division (`engine/perps.py`) allows shorts and up to 3× gross
  exposure, charges a 0.05% fee and hourly funding, and liquidates at a 5%
  maintenance margin.

## What is in here

| Path | Role |
| --- | --- |
| `engine/rules.py` | Fees, slippage, thresholds: the published numbers |
| `engine/candles.py` | Reading 1-minute candles |
| `engine/fills.py` | Signals to fills and daily marks (spot) |
| `engine/metrics.py` | Return, drawdown, Sharpe and the other figures |
| `engine/replay.py` | `python3 -m engine replay` |
| `engine/perps.py` | The Perps division and `python3 -m engine replay-perps` |
| `tests/` | The engine against hand-computed examples |

## Tests

Each suite prints one line per check and exits with an error if one fails.

```bash
python3 tests/test_engine.py
```

```bash
python3 tests/test_metrics.py
```

```bash
python3 tests/test_perps.py
```

## What this proves, and what it does not

Replaying a record proves that MarginBots computed it from those signals and
those candles by these rules. It is paper trading: it does not prove what a
bot's real orders did on an exchange, and past results don't predict future
returns. Nothing here is financial advice.

## Licence

MIT. See [LICENSE](LICENSE).
