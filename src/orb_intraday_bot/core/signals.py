"""Setup signals and pattern events produced by the setup detectors.

A `SetupSignal` is a *candidate* trade. It carries the setup's own structure (entry, stop level,
target reference) but has not yet been through the zone filter, arbiter, risk sizing or option
selection (Phase 5/6). Several signals can share a `sign_id`: only one trade is taken per sign.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Literal

from orb_intraday_bot.core.models import Direction


class Setup(StrEnum):
    ORB_BREAKOUT = "ORB_BREAKOUT"                # ORH/ORL sign -> retest -> confirmation
    PIVOT_BREAKOUT = "PIVOT_BREAKOUT"            # R2/S2 traded as an OR level
    TRAP_REVERSE_RETEST = "TRAP_REVERSE_RETEST"  # bull/bear trap, confirmation entry
    FOLLOWUP_CASE_1 = "FOLLOWUP_CASE_1"          # aggressive entry, ORB never confirmed
    FOLLOWUP_CASE_2 = "FOLLOWUP_CASE_2"          # aggressive entry, ORB had confirmed
    ADDITIONAL_WHIPSAW = "ADDITIONAL_WHIPSAW"    # continuation after 2+ rejections (ORB or trap)
    CONSOLIDATION_BREAKOUT = "CONSOLIDATION_BREAKOUT"  # direct entry at the close, no retest
    STRUCTURE_WHIPSAW = "STRUCTURE_WHIPSAW"      # 2LL/3LL (long) or 2HH/3HH (short) continuation
    FAKE_BREAK = "FAKE_BREAK"                    # fake breakdown (long) / breakout (short)


StopOn = Literal["close", "touch", "none"]
"""How the structural stop works.

- `close`: exit when a candle *closes* beyond `stop_level` (no price; evaluated at candle close)
- `touch`: exit when price trades through `stop_level`
- `none`: no structural stop; only the hard stop applies
"""


@dataclass(frozen=True, slots=True)
class SetupSignal:
    setup: Setup
    direction: Direction
    level_name: str            # "ORH", "ORL", "R2", "S2" - the level the pattern is built on
    level_value: float
    candle_start: datetime     # candle whose close produced the signal
    entry_price: float
    entry_basis: str           # how the entry price was derived (shown in reports)
    sign_id: str               # shared by every signal that stems from the same sign
    stop_level: float | None
    stop_on: StopOn
    target_ref: str | None     # "R2", "S2", "R4", "S4", "ORH", "ORL" (resolved in Phase 5)
    # Price target the setup itself supplies (consolidation: OR-range projection; fake break:
    # opposite end of the range). For "OR_PROJECTION_OR_PIVOT" Phase 5 uses whichever of this
    # price and the next pivot in the trade direction is nearer.
    target_price: float | None = None
    aggressive: bool = False
    case: int | None = None    # 1 or 2 for follow-up failures and the trap entries that follow
    note: str = field(default="", compare=False)

    @property
    def signal_id(self) -> str:
        """Deterministic id: the same signal replayed gives the same id (idempotent storage)."""
        key = "|".join(
            [
                self.candle_start.date().isoformat(),
                self.setup.value,
                self.level_name,
                self.direction.value,
                self.candle_start.strftime("%H:%M"),
            ]
        )
        return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


class PatternKind(StrEnum):
    SIGN = "SIGN"                      # close beyond the level
    RETEST = "RETEST"                  # price came back to the level, still closing beyond
    CONFIRMED = "CONFIRMED"            # break of the swing -> ORB confirmed
    FAILED_CASE_1 = "FAILED_CASE_1"    # closed back inside, ORB never confirmed
    FAILED_CASE_2 = "FAILED_CASE_2"    # closed back inside after the ORB was confirmed
    TRAP_PULLBACK = "TRAP_PULLBACK"    # pullback candle identified after a trap sign
    TRAP_TRIGGERED = "TRAP_TRIGGERED"  # pullback candle low/high broken -> entry
    TRAP_CANCELLED = "TRAP_CANCELLED"  # price closed beyond the level again
    WHIPSAW_REJECTION = "WHIPSAW_REJECTION"      # level rejected again after a confirmation
    WHIPSAW_CANCELLED = "WHIPSAW_CANCELLED"      # stop condition hit, tracking stops
    CONSOLIDATION_TOUCH = "CONSOLIDATION_TOUCH"  # new rejection at ORH/ORL while consolidating
    FAKE_BREAK = "FAKE_BREAK"                    # price left a consolidation range
    FAKE_REVERSAL = "FAKE_REVERSAL"              # ...and closed back inside: the break was fake
    FAKE_CANCELLED = "FAKE_CANCELLED"            # the fake-break setup is no longer valid
    TREND_DAY = "TREND_DAY"                      # trending-day label reached (informational)


@dataclass(frozen=True, slots=True)
class PatternEvent:
    """State change in a pattern machine, for reports and the trending-day tracker."""

    kind: PatternKind
    level_name: str
    direction: Direction   # trade direction of the pattern (a bull trap is SHORT)
    candle_start: datetime
    detail: str = ""


@dataclass(slots=True)
class StepResult:
    signals: list[SetupSignal] = field(default_factory=list)
    events: list[PatternEvent] = field(default_factory=list)

    def extend(self, other: StepResult) -> None:
        self.signals.extend(other.signals)
        self.events.extend(other.events)
