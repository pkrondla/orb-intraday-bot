"""Structured logging: JSON lines, a run id on every line, and the runner's key events."""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

import pytest

from orb_intraday_bot.runtime.logs import JsonFormatter, log_event, new_run_id
from orb_intraday_bot.runtime.output import make_output
from tests.runtime_support import Harness, at

DAY = date(2026, 10, 15)


@pytest.fixture(autouse=True)
def clean_orb_logger() -> object:
    yield
    root = logging.getLogger("orb")
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()


def test_run_ids_are_unique_and_sortable() -> None:
    a, b = new_run_id(), new_run_id()
    assert a != b and len(a) == len("20261016-091004-3fa9")


def test_make_output_writes_text_and_json_lines_with_the_run_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = make_output(tmp_path, DAY, "RUN-1")
    out("hello report")
    log_event(logging.getLogger("orb.test"), "thing_happened", count=3, symbol="NIFTY")
    logging.getLogger("orb.test").warning("careful %s", "now")

    assert "hello report" in capsys.readouterr().out
    logs = tmp_path / "logs"
    assert "hello report" in (logs / f"orb-bot-{DAY}.log").read_text(encoding="utf-8")
    lines = [
        json.loads(x)
        for x in (logs / f"orb-bot-{DAY}.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [x["run_id"] for x in lines] == ["RUN-1", "RUN-1"]
    event = lines[0]
    assert event["event"] == "thing_happened" and event["count"] == 3
    assert event["symbol"] == "NIFTY" and event["level"] == "INFO"
    assert lines[1]["event"] == "log" and lines[1]["message"] == "careful now"
    assert "[RUN-1]" in (logs / f"orb-bot-{DAY}.log").read_text(encoding="utf-8")


def test_configuring_twice_does_not_duplicate_lines(tmp_path: Path) -> None:
    make_output(tmp_path, DAY, "RUN-A")
    make_output(tmp_path, DAY, "RUN-B")
    log_event(logging.getLogger("orb.test"), "once")
    lines = (tmp_path / "logs" / f"orb-bot-{DAY}.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["run_id"] == "RUN-B"


def test_exceptions_are_included_and_values_are_serialisable() -> None:
    record = logging.LogRecord("orb.x", logging.ERROR, "f.py", 1, "failed", (), None)
    try:
        raise ValueError("bad")
    except ValueError:
        import sys

        record.exc_info = sys.exc_info()
    record.when = DAY               # not JSON-native: must not break the formatter
    data = json.loads(JsonFormatter().format(record))
    assert "ValueError: bad" in data["exception"] and data["when"] == "2026-10-15"


def test_the_runner_logs_its_key_events(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="orb.live")
    Harness(tmp_path, at(9, 10)).runner().run()
    events = [r.__dict__["event"] for r in caplog.records if "event" in r.__dict__]
    assert events[0] == "run_started" and events[-1] == "run_finished"
    assert events.count("candle_processed") >= 5
    started = next(r for r in caplog.records if r.__dict__.get("event") == "run_started")
    assert started.__dict__["strategy_hash"] and started.__dict__["symbol"]
