"""Dates the operator removed from the holiday list with `/holiday remove`.

The exchange list stays in `config/holidays.yaml`. A removal is saved in
`data/HOLIDAYS_REMOVED.json`, so a restart or a deploy keeps it. The serve loop reads the file
on each wait, so a removal takes effect without a restart.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from orb_intraday_bot.runtime.calendar import load_holidays

OVERRIDE_FILE_NAME = "HOLIDAYS_REMOVED.json"


def removed_path(data_dir: Path) -> Path:
    return data_dir / OVERRIDE_FILE_NAME


def load_removed(data_dir: Path) -> set[date]:
    """Dates `/holiday remove` has taken out of the holiday list. Unknown lines are skipped."""
    try:
        raw = json.loads(removed_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return set()
    items = raw.get("removed") if isinstance(raw, dict) else None
    found: set[date] = set()
    for item in items or []:
        try:
            found.add(date.fromisoformat(str(item)))
        except ValueError:
            continue
    return found


def save_removed(data_dir: Path, days: set[date]) -> None:
    path = removed_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"removed": [day.isoformat() for day in sorted(days)]}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def active_holidays(data_dir: Path, base: frozenset[date] | None = None) -> frozenset[date]:
    """Weekday closures still in force: the holiday file (or `base`) minus Telegram removals."""
    closed = set(load_holidays() if base is None else base)
    closed -= load_removed(data_dir)
    return frozenset(closed)


def parse_holiday_date(text: str) -> date | None:
    try:
        return date.fromisoformat(text.strip())
    except ValueError:
        return None


def remove_holiday(data_dir: Path, day: date) -> str:
    """Record `day` as not a holiday. Returns weekend, absent, already, or removed."""
    if day.weekday() >= 5:
        return "weekend"
    configured = load_holidays()
    removed = load_removed(data_dir)
    if day not in configured:
        return "already" if day in removed else "absent"
    if day in removed:
        return "already"
    removed.add(day)
    save_removed(data_dir, removed)
    return "removed"
