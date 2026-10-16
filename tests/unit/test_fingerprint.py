"""Config fingerprints: a trade is tied to the rules it was opened under."""

from __future__ import annotations

from orb_intraday_bot.config.fingerprint import (
    ASSUMPTION_SECTIONS,
    RUNTIME_SECTIONS,
    STRATEGY_SECTIONS,
    fingerprint,
)
from orb_intraday_bot.config.settings import StrategyConfig, apply_overrides
from orb_intraday_bot.paper.models import Position
from tests.unit.test_runtime import CFG, make_day, real_day


def test_every_config_section_belongs_to_exactly_one_group() -> None:
    groups = [*STRATEGY_SECTIONS, *ASSUMPTION_SECTIONS, *RUNTIME_SECTIONS]
    assert sorted(groups) == sorted(StrategyConfig.model_fields)
    assert len(groups) == len(set(groups))


def test_the_hash_is_stable_and_short() -> None:
    a, b = fingerprint(StrategyConfig()), fingerprint(StrategyConfig())
    assert a == b
    assert len(a.strategy) == 12


def test_a_rule_change_moves_only_the_strategy_hash() -> None:
    base = fingerprint(StrategyConfig())
    changed = fingerprint(apply_overrides(StrategyConfig(), ["stops.hard_stop_points=30"]))
    assert changed.strategy != base.strategy
    assert changed.assumptions == base.assumptions and changed.runtime == base.runtime


def test_slippage_moves_only_the_assumptions_hash() -> None:
    base = fingerprint(StrategyConfig())
    changed = fingerprint(
        apply_overrides(StrategyConfig(), ["paper.slippage_option_points=1"])
    )
    assert changed.assumptions != base.assumptions
    assert changed.strategy == base.strategy


def test_runtime_timing_moves_only_the_runtime_hash() -> None:
    base = fingerprint(StrategyConfig())
    changed = fingerprint(apply_overrides(StrategyConfig(), ["runtime.poll_interval_sec=30"]))
    assert changed.runtime != base.runtime
    assert changed.strategy == base.strategy


def test_positions_record_the_hash_and_survive_storage() -> None:
    day = make_day()
    for candle in real_day():
        day.on_candle(candle)
    positions = day.positions_today()
    assert positions
    expected = fingerprint(CFG).strategy
    assert all(p.strategy_hash == expected for p in positions)
    reloaded = Position.from_dict(positions[0].to_dict())
    assert reloaded.strategy_hash == expected


def test_old_stored_positions_without_a_hash_still_load() -> None:
    day = make_day()
    for candle in real_day():
        day.on_candle(candle)
    raw = day.positions_today()[0].to_dict()
    del raw["strategy_hash"]
    assert Position.from_dict(raw).strategy_hash == ""
