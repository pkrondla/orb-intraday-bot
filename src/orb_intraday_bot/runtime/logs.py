"""Structured logging: one JSON object per line, every line tagged with the run id.

    {"ts": "2026-10-16T09:33:05+05:30", "level": "INFO", "logger": "orb.live",
     "run_id": "20261016-091004-3fa9", "event": "candle_processed", "candle": "09:30", ...}

The human report (the per-candle text) still goes to the console and `orb-bot-<day>.log`; the
JSON lines go to `orb-bot-<day>.jsonl` for tools (grep, jq, a log shipper in Phase 17). A run id
ties together every line of one process, so two runs on the same day can be told apart.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime
from typing import Any

from orb_intraday_bot.core.clock import IST

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


def new_run_id(now: datetime | None = None) -> str:
    stamp = (now or datetime.now(IST)).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(2)}"


class RunIdFilter(logging.Filter):
    def __init__(self, run_id: str) -> None:
        super().__init__()
        self.run_id = run_id

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = self.run_id
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, IST).isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "run_id": getattr(record, "run_id", ""),
        }
        if "event" not in record.__dict__:
            entry["event"] = "log"
        entry["message"] = record.getMessage()
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key != "run_id":
                entry[key] = value
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str, ensure_ascii=False)


def log_event(logger: logging.Logger, event: str, message: str = "", **fields: Any) -> None:
    """One structured line: a stable `event` name plus fields, never a sentence to parse."""
    logger.info(message or event, extra={"event": event, **fields})
