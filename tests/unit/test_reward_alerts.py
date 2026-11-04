"""Reward-rung Telegram notes. They do not sell, and a rung is sent once."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from orb_intraday_bot.config.fingerprint import fingerprint
from orb_intraday_bot.config.settings import StrategyConfig, apply_overrides
from orb_intraday_bot.paper.models import ExitReason, Fill, Position, PositionStatus
from orb_intraday_bot.runtime.live import LiveRunner
from orb_intraday_bot.runtime.report import format_reward_alert
from orb_intraday_bot.runtime.reward import (
    pending_reward_alerts,
    reward_progress,
    reward_rungs,
    sizing_risk_inr,
)
from tests.helpers import at
from tests.runtime_support import CONTRACT, Harness, make_day, real_day
from tests.unit.test_paper import OPENED, make_trade


class _FullOutbox:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, text: str) -> bool:
        self.messages.append(text)
        return False


def _premium(position: Position, multiple: float) -> float:
    return position.entry_ltp + multiple * sizing_risk_inr(position) / position.quantity


def test_rungs_fire_once_each_and_a_jump_is_one_update() -> None:
    position = _open()
    assert reward_progress(position, _premium(position, 0.4), 0.5) is None

    first = reward_progress(position, _premium(position, 1.1), 0.5)
    assert first is not None
    assert first.crossed == (0.5, 1.0)
    assert first.latch == 1.0
    assert first.gross_inr == pytest.approx(1.1 * first.risk_inr, abs=0.02)

    position.reward_rung_sent = first.latch
    assert reward_progress(position, _premium(position, 0.7), 0.5) is None
    nxt = reward_progress(position, _premium(position, 1.6), 0.5)
    assert nxt is not None and nxt.crossed == (1.5,)


def test_booked_fills_count_toward_the_rung() -> None:
    position = _open()
    booked_lots = 2
    position.fills.append(
        Fill(
            time=at(9, 40),
            reason=ExitReason.T1,
            lots=booked_lots,
            quantity=booked_lots * position.lot_size,
            futures_price=22900.0,
            option_price=170.0,
            price_source="live",
            option_gross=(170.0 - position.entry_ltp) * booked_lots * position.lot_size,
            futures_points=100.0,
            charges=10.0,
        )
    )
    position.remaining_lots = 1
    progress = reward_progress(position, 160.0, 0.5)
    assert progress is not None
    assert progress.crossed == (0.5,)
    open_pnl = position.gross_option_pnl + position.unrealized(160.0)
    assert progress.gross_inr == pytest.approx(open_pnl)
    card = format_reward_alert(progress, at(9, 42))
    assert "0.5R" in card
    assert "already booked" in card
    assert "Bot has not sold" in card
    assert f"/exit {position.trading_symbol}" in card
    assert "1.5R" not in card


def test_no_rung_without_risk_or_a_live_premium() -> None:
    position = _open()
    position.sl_exit = position.entry_ltp
    assert reward_progress(position, 200.0, 0.5) is None
    fresh = _open()
    assert pending_reward_alerts([fresh], {}, 0.5) == []
    fresh.status = PositionStatus.CLOSED
    assert reward_progress(fresh, 200.0, 0.5) is None


def test_reward_settings_are_runtime_only() -> None:
    base = fingerprint(StrategyConfig())
    changed = fingerprint(apply_overrides(StrategyConfig(), ["runtime.reward_alerts=false"]))
    assert changed.strategy == base.strategy
    assert changed.runtime != base.runtime
    with pytest.raises(ValidationError):
        apply_overrides(StrategyConfig(), ["runtime.reward_step=0"])


def test_steps_stop_at_t1_and_continue_when_t2_has_a_runner() -> None:
    position = _open()
    risk = sizing_risk_inr(position)
    one_r = risk / position.quantity
    position.t2 = position.t1
    position.t1_option = position.entry_ltp + 1.2 * one_r
    position.t2_option = position.t1_option
    assert reward_rungs(position, 0.5) == (0.5, 1.0)
    capped = reward_progress(position, _premium(position, 3.0), 0.5)
    assert capped is not None and capped.crossed == (0.5, 1.0)

    position.reward_rung_sent = 0
    position.t2 = position.t1 + 100
    position.t1_book_lots = position.lots - 1
    position.t1_option = position.entry_ltp + 1.2 * one_r
    position.t2_option = position.entry_ltp + 4.0 * one_r
    assert reward_rungs(position, 0.5) == (0.5, 1.0, 1.5, 2.0)
    extended = reward_progress(position, _premium(position, 3.0), 0.5)
    assert extended is not None and extended.crossed == (0.5, 1.0, 1.5, 2.0)


def test_candle_processing_does_not_mark_a_rung() -> None:
    day = make_day()
    for candle in real_day():
        day.on_candle(candle)
    seen = day.paper.open_positions + day.paper.closed_positions
    assert seen
    assert all(p.reward_rung_sent == 0 for p in seen)


def test_live_poll_sends_once_and_remembers_across_a_restart(tmp_path: Path) -> None:
    h, runner, position = _polled(tmp_path, multiple=1.1)
    runner._poll_ticks()
    text = h.notes.joined()
    assert "REWARD" in text
    assert "0.5R, 1R" in text
    assert "Bot has not sold" in text
    assert f"/exit {position.trading_symbol}" in text
    assert "1.5R" not in text
    assert position.is_open
    assert position.reward_rung_sent == 1.0
    assert "bot has not sold" in h.text

    h.notes.messages.clear()
    runner._last_poll = None
    runner._poll_ticks()
    assert h.notes.messages == []

    restored = make_day(h.store)
    again = restored.paper.open_positions
    assert len(again) == 1 and again[0].reward_rung_sent == 1.0
    runner._day = restored
    runner._last_poll = None
    runner._poll_ticks()
    assert h.notes.messages == []


def test_a_stopped_trade_is_not_a_reward_alert(tmp_path: Path) -> None:
    h, runner, position = _polled(tmp_path, multiple=1.1)
    h.source.price_override = (at(9, 0), position.hard_stop - 1)
    runner._poll_ticks()
    assert "REWARD" not in h.notes.joined()
    assert not position.is_open
    assert position.reward_rung_sent == 0


def test_a_failed_send_does_not_latch(tmp_path: Path) -> None:
    h, runner, position = _polled(tmp_path, multiple=1.1)
    outbox = _FullOutbox()
    runner._alerts._notifier = outbox  # noqa: SLF001
    runner._poll_ticks()
    assert outbox.messages
    assert position.reward_rung_sent == 0
    assert position.is_open


def test_reward_alerts_can_be_switched_off(tmp_path: Path) -> None:
    h, runner, position = _polled(tmp_path, multiple=2.0, reward_alerts=False)
    runner._poll_ticks()
    assert "REWARD" not in h.notes.joined()
    assert position.reward_rung_sent == 0
    assert position.is_open


def _open() -> Position:
    from tests.unit.test_paper import opened

    return opened()


def _polled(
    tmp_path: Path, *, multiple: float, reward_alerts: bool = True
) -> tuple[Harness, LiveRunner, Position]:
    h = Harness(tmp_path, at(10, 0))
    day = make_day(h.store)
    trade, plan = make_trade()
    position = day.paper.open(trade, plan, OPENED)
    assert position is not None
    h.source.option_ltp = _premium(position, multiple)
    h.source.price_override = (at(9, 0), position.futures_entry)
    cfg = StrategyConfig.model_validate(
        {
            "runtime": {
                "fetch_retry_sec": 60,
                "poll_interval_sec": 30,
                "reward_alerts": reward_alerts,
            }
        }
    )
    runner = LiveRunner(
        cfg, h.data_dir, h.source, h.store, h.notes, h.clock, h.sleep, h.lines.append,
    )
    runner._day = day  # noqa: SLF001
    runner._contract = CONTRACT  # noqa: SLF001
    return h, runner, position
