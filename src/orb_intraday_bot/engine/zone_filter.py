"""Pivot-zone hard rules (spec section 4).

- Above R1 (bullish): no shorts. Longs are allowed.
- Below S1 (bearish): no longs. Shorts are allowed.
- R1 to P to S1 (neutral): no aggressive entries. Standard entries are allowed, and
  `plan_targets` switches them to scalp-and-run (T2 = nearest in-zone pivot, T1 = first magnet).

"Aggressive" is an entry with no retest or confirmation: the Case 1/2 follow-up entries and the
consolidation breakout. When one is blocked here, the standard trigger of the same sign (the trap
confirmation entry, or the ORB retest entry) is still available.

The zone is read from the close of the signal candle (the decision is made at candle close).
Rejections or bounces from R2, R4, S2, S4 need no extra rule: they would be shorts above R1 or longs
below S1 (blocked here), or entries with no room to the next mandatory exit (blocked by the
target plan).
"""

from __future__ import annotations

from orb_intraday_bot.core.models import Direction, Zone
from orb_intraday_bot.core.signals import SetupSignal


def zone_block_reason(signal: SetupSignal, zone: Zone) -> str | None:
    if zone is Zone.BULLISH and signal.direction is Direction.SHORT:
        return "above R1: no shorts"
    if zone is Zone.BEARISH and signal.direction is Direction.LONG:
        return "below S1: no longs"
    if zone is Zone.NEUTRAL and signal.aggressive:
        return "neutral zone (R1 to S1): no aggressive entries, wait for the standard trigger"
    return None
