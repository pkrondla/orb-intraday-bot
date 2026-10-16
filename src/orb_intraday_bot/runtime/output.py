"""Where the report text goes: the console and log files (one set per day, in DATA_DIR/logs).

    orb-bot-<day>.log    the readable report plus warnings, for people
    orb-bot-<day>.jsonl  structured events, one JSON object per line, for tools
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path

from orb_intraday_bot.runtime.logs import JsonFormatter, RunIdFilter, new_run_id

_HANDLER_NAME = "orb-bot"


def make_output(
    data_dir: Path, day: date, run_id: str | None = None
) -> Callable[[str], None]:
    """A function that prints a block of text and appends it to the day's log file."""
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"orb-bot-{day.isoformat()}.log"
    configure_logging(log_dir, day, run_id or new_run_id())

    def out(text: str) -> None:
        print(text, flush=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(text + "\n")

    return out


def configure_logging(log_dir: Path, day: date, run_id: str) -> None:
    """Attach the text, JSON and console handlers to the `orb` logger (once per process)."""
    root = logging.getLogger("orb")
    for handler in list(root.handlers):
        if handler.get_name() == _HANDLER_NAME:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(logging.INFO)
    run_filter = RunIdFilter(run_id)
    text = logging.FileHandler(log_dir / f"orb-bot-{day.isoformat()}.log", encoding="utf-8")
    text.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(run_id)s] %(name)s: %(message)s")
    )
    machine = logging.FileHandler(log_dir / f"orb-bot-{day.isoformat()}.jsonl", encoding="utf-8")
    machine.setFormatter(JsonFormatter())
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.WARNING)
    console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    for handler in (text, machine, console):
        handler.addFilter(run_filter)
        handler.set_name(_HANDLER_NAME)     # marks ours, so a second call replaces them
        root.addHandler(handler)
