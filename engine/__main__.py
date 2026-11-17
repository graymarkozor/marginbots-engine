"""Run the engine's commands: python3 -m engine replay ... or replay-perps ..."""

from __future__ import annotations

import sys

from . import perps, replay

COMMANDS = {"replay": replay.main, "replay-perps": perps.main}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in COMMANDS:
        print("Usage: python3 -m engine replay SIGNALS.csv CANDLES.csv [...] [--out FOLDER]\n"
              "       python3 -m engine replay-perps SIGNALS.csv CANDLES.csv [...] --funding FUNDING.csv [--out FOLDER]")
        return 2
    return COMMANDS[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main())
