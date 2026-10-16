"""Replay past days with the paper engine. Kept for compatibility: it is `orb-bot replay`.

    python scripts/phase7_replay_paper.py 2026-10-14 2026-10-15 --quiet
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orb_intraday_bot.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(["replay", *sys.argv[1:]]))
