from __future__ import annotations

from datetime import datetime
from pathlib import Path

from orb_intraday_bot.config import load_strategy_config
from orb_intraday_bot.core.clock import IST, candle_close, candle_start


def test_candle_start_anchored_at_0915() -> None:
    ts = datetime(2026, 10, 15, 9, 20, 41, tzinfo=IST)
    assert candle_start(ts) == datetime(2026, 10, 15, 9, 18, tzinfo=IST)
    assert candle_close(candle_start(ts)) == datetime(2026, 10, 15, 9, 21, tzinfo=IST)


def test_first_and_last_candle() -> None:
    assert candle_start(datetime(2026, 10, 15, 9, 15, tzinfo=IST)).minute == 15
    assert candle_start(datetime(2026, 10, 15, 15, 29, tzinfo=IST)) == datetime(
        2026, 10, 15, 15, 27, tzinfo=IST
    )


def test_default_yaml_loads() -> None:
    cfg = load_strategy_config(Path(__file__).resolve().parents[2] / "config" / "default.yaml")
    assert cfg.instrument.underlying == "NIFTY"
    assert cfg.stops.hard_stop_points == 35
    assert cfg.options.delta_target == 0.55
    assert cfg.enabled_setups.both_sides_trap is False
