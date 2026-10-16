"""Trending-day label (notes 3.9). Informational only: it never changes a trade rule.

Setups seen today are collected as labels:

- ORH_BO / ORL_BD      an ORB confirmed on ORH / ORL (or a consolidation breakout on that level)
- BULL_TRAP            a breakout above ORH failed (close back inside)
- BEAR_TRAP            a breakdown below ORL failed
- FAILED_BULL_TRAP     after a bull trap, price closes above ORH again
- FAILED_BEAR_TRAP     after a bear trap, price closes below ORL again

Two setups in the same direction mark a trending day (pairings from the notes):

- Uptrend:   ORH_BO together with BEAR_TRAP or FAILED_BULL_TRAP
- Downtrend: ORL_BD together with BULL_TRAP or FAILED_BEAR_TRAP

[Assumption] A Failed Bear Trap is a bear trap whose reversal fails: price closes below ORL again
(a short signal). A Failed Bull Trap is the mirror. The notes list the names but not the
definitions.
"""

from __future__ import annotations

from orb_intraday_bot.core.models import Candle, Direction
from orb_intraday_bot.core.signals import (
    PatternEvent,
    PatternKind,
    Setup,
    SetupSignal,
)

UP_PAIRS = ({"ORH_BO"}, {"BEAR_TRAP", "FAILED_BULL_TRAP"})
DOWN_PAIRS = ({"ORL_BD"}, {"BULL_TRAP", "FAILED_BEAR_TRAP"})


class TrendingDayTracker:
    def __init__(self) -> None:
        self.labels: set[str] = set()
        self.uptrend = False
        self.downtrend = False

    @property
    def state(self) -> str:
        if self.uptrend and self.downtrend:
            return "UP and DOWN (choppy)"
        if self.uptrend:
            return "UPTREND"
        if self.downtrend:
            return "DOWNTREND"
        return "none"

    def update(
        self, candle: Candle, events: list[PatternEvent], signals: list[SetupSignal]
    ) -> list[PatternEvent]:
        new: list[str] = []
        for ev in events:
            label = self._label(ev)
            if label:
                new.append(label)
        for sig in signals:
            if sig.setup is Setup.CONSOLIDATION_BREAKOUT:
                new.append("ORH_BO" if sig.level_name == "ORH" else "ORL_BD")
        self.labels.update(new)

        out: list[PatternEvent] = []
        if not self.uptrend and self._pair(UP_PAIRS):
            self.uptrend = True
            out.append(self._event(candle, Direction.LONG, UP_PAIRS))
        if not self.downtrend and self._pair(DOWN_PAIRS):
            self.downtrend = True
            out.append(self._event(candle, Direction.SHORT, DOWN_PAIRS))
        return out

    # -- helpers -------------------------------------------------------------------------
    def _label(self, ev: PatternEvent) -> str | None:
        lvl = ev.level_name
        if ev.kind is PatternKind.CONFIRMED:
            return {"ORH": "ORH_BO", "ORL": "ORL_BD"}.get(lvl)
        if ev.kind in (PatternKind.FAILED_CASE_1, PatternKind.FAILED_CASE_2):
            return {"ORH": "BULL_TRAP", "ORL": "BEAR_TRAP"}.get(lvl)
        if ev.kind is PatternKind.SIGN:
            if lvl == "ORH" and "BULL_TRAP" in self.labels:
                return "FAILED_BULL_TRAP"
            if lvl == "ORL" and "BEAR_TRAP" in self.labels:
                return "FAILED_BEAR_TRAP"
        return None

    def _pair(self, pairs: tuple[set[str], set[str]]) -> bool:
        first, second = pairs
        return bool(self.labels & first) and bool(self.labels & second)

    def _event(
        self, candle: Candle, direction: Direction, pairs: tuple[set[str], set[str]]
    ) -> PatternEvent:
        have = sorted(self.labels & (pairs[0] | pairs[1]))
        name = "UPTREND" if direction is Direction.LONG else "DOWNTREND"
        return PatternEvent(
            PatternKind.TREND_DAY, "DAY", direction, candle.start, f"{name}: {' + '.join(have)}"
        )
