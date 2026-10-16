"""Where the report text goes: the console and a log file (one file per day, in DATA_DIR/logs)."""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path


def make_output(data_dir: Path, day: date) -> Callable[[str], None]:
    """A function that prints a block of text and appends it to the day's log file."""
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"orb-bot-{day.isoformat()}.log"
    _configure_logging(path)

    def out(text: str) -> None:
        print(text, flush=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(text + "\n")

    return out


def _configure_logging(path: Path) -> None:
    root = logging.getLogger("orb")
    if any(isinstance(h, logging.FileHandler) for h in root.handlers):
        return
    root.setLevel(logging.INFO)
    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.WARNING)
    console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(file_handler)
    root.addHandler(console)
