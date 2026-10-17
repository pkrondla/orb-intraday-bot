"""Pre-trade risk gate and runtime kill switch.

Vetoes every new entry that breaches a limit. Can trip the kill switch (halt new entries and
request flatten) on daily loss or loss streak.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

from orb_intraday_bot.risk.limits import RiskLimits, load_risk_limits


@dataclass(frozen=True, slots=True)
class RiskDecision:
    allowed: bool
    reason: str = ""

    @staticmethod
    def ok() -> RiskDecision:
        return RiskDecision(True)

    @staticmethod
    def deny(reason: str) -> RiskDecision:
        return RiskDecision(False, reason)


@dataclass
class RiskState:
    day: date | None = None
    trades_today: int = 0
    open_positions: int = 0
    realized_pnl_inr: float = 0.0
    loss_streak: int = 0
    halted: bool = False
    halt_reason: str = ""
    # No new entries, but open positions are left alone (unlike `halted`, which also flattens).
    # Used when the bot's books and the broker's disagree: selling could make things worse.
    entries_blocked: bool = False
    block_reason: str = ""


@dataclass
class RiskEngine:
    limits: RiskLimits = field(default_factory=load_risk_limits)
    capital_inr: float = 500_000.0
    state: RiskState = field(default_factory=RiskState)

    def reset_day(self, day: date) -> None:
        if self.state.day == day:
            return
        self.state = RiskState(day=day)

    def rebuild(
        self, *, trades_today: int, open_positions: int, closed_pnls: Sequence[float]
    ) -> None:
        """Restore today's counters after a restart. `closed_pnls` is in closing order.

        The limits are applied again, so a day that already breached one stays halted.
        """
        self.state.trades_today = trades_today
        self.state.open_positions = open_positions
        self.state.realized_pnl_inr = 0.0
        self.state.loss_streak = 0
        for pnl in closed_pnls:
            self.state.realized_pnl_inr += pnl
            self.state.loss_streak = self.state.loss_streak + 1 if pnl < 0 else 0
        limit = self.limits.daily_loss_limit_inr(self.capital_inr)
        if self.state.realized_pnl_inr <= -limit:
            self.trip(f"daily loss limit hit ({self.state.realized_pnl_inr:.0f} <= -{limit:.0f})")
        elif self.state.loss_streak >= self.limits.loss_streak_halt:
            self.trip(f"loss streak {self.state.loss_streak}")

    def recheck_daily_loss(self) -> None:
        """After the capital or the loss limit changed: trip if today's loss is already past it."""
        limit = self.limits.daily_loss_limit_inr(self.capital_inr)
        if not self.state.halted and self.state.realized_pnl_inr <= -limit:
            self.trip(f"daily loss limit hit ({self.state.realized_pnl_inr:.0f} <= -{limit:.0f})")

    def record_open(self) -> None:
        self.state.trades_today += 1
        self.state.open_positions += 1

    def record_close(self, pnl_inr: float) -> None:
        self.state.open_positions = max(0, self.state.open_positions - 1)
        self.state.realized_pnl_inr += pnl_inr
        if pnl_inr < 0:
            self.state.loss_streak += 1
        else:
            self.state.loss_streak = 0
        limit = self.limits.daily_loss_limit_inr(self.capital_inr)
        if self.state.realized_pnl_inr <= -limit:
            self.trip(f"daily loss limit hit ({self.state.realized_pnl_inr:.0f} <= -{limit:.0f})")
        elif self.state.loss_streak >= self.limits.loss_streak_halt:
            self.trip(f"loss streak {self.state.loss_streak}")

    def trip(self, reason: str) -> None:
        self.state.halted = True
        self.state.halt_reason = reason

    def clear_halt(self) -> None:
        self.state.halted = False
        self.state.halt_reason = ""
        self.state.entries_blocked = False
        self.state.block_reason = ""

    def block_entries(self, reason: str) -> None:
        """Stop opening trades without touching open positions (needs a human to look)."""
        self.state.entries_blocked = True
        self.state.block_reason = reason

    def check_entry(
        self,
        *,
        lots: int,
        data_age_sec: float | None = None,
        option_spread_pct: float | None = None,
        available_margin_inr: float | None = None,
        required_margin_inr: float | None = None,
        price_sane: bool = True,
    ) -> RiskDecision:
        if self.state.halted:
            return RiskDecision.deny(f"halted: {self.state.halt_reason}")
        if self.state.entries_blocked:
            return RiskDecision.deny(f"entries blocked: {self.state.block_reason}")
        if lots < 1:
            return RiskDecision.deny("lots < 1")
        if lots > self.limits.max_lots_per_trade:
            return RiskDecision.deny(
                f"lots {lots} > max {self.limits.max_lots_per_trade}"
            )
        if self.state.open_positions >= self.limits.max_open_positions:
            return RiskDecision.deny("max open positions")
        if self.state.trades_today >= self.limits.max_trades_per_day:
            return RiskDecision.deny("max trades per day")
        if data_age_sec is not None and data_age_sec > self.limits.max_data_staleness_sec:
            return RiskDecision.deny(f"stale data ({data_age_sec:.0f}s)")
        if (
            option_spread_pct is not None
            and option_spread_pct > self.limits.max_option_spread_pct
        ):
            return RiskDecision.deny(f"option spread {option_spread_pct:.2%}")
        if not price_sane:
            return RiskDecision.deny("price sanity check failed")
        if (
            available_margin_inr is not None
            and required_margin_inr is not None
            and available_margin_inr < required_margin_inr
        ):
            return RiskDecision.deny(
                f"insufficient margin ({available_margin_inr:.0f} < {required_margin_inr:.0f})"
            )
        return RiskDecision.ok()
