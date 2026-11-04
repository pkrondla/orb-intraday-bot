"""Reward rungs for an open trade. Notification only: nothing here sells.

1R is the sizing loss on the original lots, the "loss at SL" on the open card:
`(entry premium − sl_exit) × lots × lot size`. `sl_exit` is the 35 × delta plan
stop, not the wider broker crash net.

The ladder is one step (default 0.5R) at a time, from the entry up to the planned
profit at T1. When the plan leaves a runner, the same steps continue to the planned
profit at T2. A trade whose T1 is 2.4R and whose T2 is 3.1R is told at 0.5, 1, 1.5,
2, 2.5 and 3. Nothing is sent past that far target.

Profit is gross option rupees: fills already booked, plus the live premium on the
lots still held. Exit charges are not deducted. A rung fires once, the first time
a live premium reaches it, and the highest rung sent is stored on the position.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from orb_intraday_bot.paper.models import Position


@dataclass(frozen=True, slots=True)
class RewardProgress:
    position: Position
    option_price: float
    gross_inr: float
    risk_inr: float
    multiple: float
    crossed: tuple[float, ...]

    @property
    def latch(self) -> float:
        """Highest rung this update is telling the operator about."""
        return self.crossed[-1]


def sizing_risk_inr(position: Position) -> float:
    """Rupees lost if the original lots are stopped at the sizing option SL."""
    return round((position.entry_ltp - position.sl_exit) * position.lots * position.lot_size, 2)


def trade_gross_inr(position: Position, option_price: float) -> float:
    """Booked exit profit plus open profit on the lots still held, before exit charges."""
    return round(position.gross_option_pnl + position.unrealized(option_price), 2)


def far_target_gross_inr(position: Position) -> float:
    """Planned gross at T1, or at T1 plus the runner's T2 when that target is separate."""
    entry = position.entry_ltp
    unit = position.lot_size
    runner = position.lots - position.t1_book_lots
    distinct = abs(position.t1 - position.t2) > position.tick_size
    if distinct and runner > 0:
        at_t1 = (position.t1_option - entry) * position.t1_book_lots * unit
        at_t2 = (position.t2_option - entry) * runner * unit
        return round(at_t1 + at_t2, 2)
    return round((position.t1_option - entry) * position.lots * unit, 2)


def reward_rungs(position: Position, step: float) -> tuple[float, ...]:
    """Steps of `step` R that are still at or before the far target."""
    risk = sizing_risk_inr(position)
    if risk <= 0 or step <= 0:
        return ()
    ceiling = far_target_gross_inr(position)
    if ceiling <= 0:
        return ()
    count = int((ceiling / risk + 1e-9) / step)
    return tuple(round(i * step, 4) for i in range(1, count + 1))


def reward_progress(
    position: Position, option_price: float, step: float
) -> RewardProgress | None:
    """Rungs crossed since the last alert, or None when there is nothing new to say."""
    if not position.is_open or position.remaining_quantity <= 0:
        return None
    rungs = reward_rungs(position, step)
    risk = sizing_risk_inr(position)
    if risk <= 0 or not rungs:
        return None
    gross = trade_gross_inr(position, option_price)
    crossed = tuple(
        rung
        for rung in rungs
        if rung > position.reward_rung_sent + 1e-6 and gross + 0.005 >= rung * risk
    )
    if not crossed:
        return None
    return RewardProgress(
        position=position,
        option_price=option_price,
        gross_inr=gross,
        risk_inr=risk,
        multiple=gross / risk,
        crossed=crossed,
    )


def pending_reward_alerts(
    positions: Sequence[Position],
    option_ltps: Mapping[str, float],
    step: float,
) -> list[RewardProgress]:
    """Open positions whose live premium has reached a new rung. Missing LTPs are skipped."""
    found: list[RewardProgress] = []
    for position in positions:
        price = option_ltps.get(position.trading_symbol)
        if price is None:
            continue
        progress = reward_progress(position, price, step)
        if progress is not None:
            found.append(progress)
    return found
