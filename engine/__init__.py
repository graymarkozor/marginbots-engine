"""
engine/ — the fill engine of the MarginBots leaderboard.

It turns a bot's signals into fills and daily equity, from public 1-minute
candles only, with the published fees and slippage (rules.py). The
leaderboard runs this code; anyone can run it too and get the same numbers:

    python3 -m engine replay signals.csv candles.csv

Standard library only, so it runs on any Python 3.9 or newer, without
installing anything.
"""
