"""The strategy must reproduce the recorded replay exactly (see `snapshot.py`)."""

from __future__ import annotations

import json
from typing import Any

from orb_intraday_bot.config.settings import apply_overrides
from tests.golden.snapshot import (
    CANDLES,
    EXPECTED,
    default_config,
    dumps,
    load_candles,
    replay_snapshot,
)

HINT = (
    "The replay no longer matches tests/golden/expected_replay.json. If this change is a "
    "refactor, it changed behaviour: fix it. If the strategy change is intended, run "
    "`python -m tests.golden.snapshot --update`, bump the version and explain the diff."
)


def test_fixture_files_exist() -> None:
    assert CANDLES.exists() and EXPECTED.exists()


def test_replay_matches_golden_file_exactly() -> None:
    expected: dict[str, Any] = json.loads(EXPECTED.read_text(encoding="utf-8"))
    actual = replay_snapshot()
    assert actual["summary"] == expected["summary"], HINT
    assert list(actual["days"]) == list(expected["days"]), HINT
    for day, body in expected["days"].items():
        assert actual["days"][day] == body, f"{day}: {HINT}"


def test_golden_file_is_in_canonical_form() -> None:
    expected = json.loads(EXPECTED.read_text(encoding="utf-8"))
    assert dumps(expected) == EXPECTED.read_text(encoding="utf-8").replace("\r\n", "\n")


def test_replay_is_deterministic() -> None:
    candles = load_candles()
    first = replay_snapshot(candles, default_config())
    second = replay_snapshot(candles, default_config())
    assert first == second


def test_the_golden_file_notices_a_strategy_change() -> None:
    """Guards the guard: a different stop must not reproduce the recorded replay."""
    expected = json.loads(EXPECTED.read_text(encoding="utf-8"))
    changed = apply_overrides(default_config(), ["stops.hard_stop_points=30"])
    assert replay_snapshot(load_candles(), changed)["days"] != expected["days"]


def test_golden_covers_a_real_range_of_behaviour() -> None:
    expected = json.loads(EXPECTED.read_text(encoding="utf-8"))
    summary = expected["summary"]
    assert summary["days"] >= 30
    assert summary["trades"] >= 50
    reasons = {
        fill["reason"]
        for day in expected["days"].values()
        for position in day["positions"]
        for fill in position["fills"]
    }
    # Stops, a target and the end-of-day exit all appear, so a refactor of any exit path is seen.
    assert {"HARD_STOP", "T1"} <= reasons
    assert any("EOD" in r or "END" in r for r in reasons)
