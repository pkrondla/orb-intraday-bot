"""Pivot-zone rules (spec section 4).

Pivots are reference. OR setups are taken in every zone. The only hard block is
**aggressive** entries in the neutral band (R1 to S1): Case 1/2 follow-ups and the
consolidation breakout. Those wait for the standard trigger of the same sign
(trap confirmation or ORB retest).

When a standard entry is taken in the neutral zone, `plan_targets` switches it to
scalp-and-run (T1 = nearest in-zone pivot; T2 is T1 ± OR-range or the next in-zone
pivot, capped at R1/S1).

The zone is read from the close of the signal candle. Rejections or bounces from
R2, R4, S2, S4 are not a separate setup: they still need an OR pattern, and a
target with no room is rejected by the target plan.
"""

from __future__ import annotations

from orb_intraday_bot.core.models import Zone
from orb_intraday_bot.core.signals import SetupSignal


def zone_block_reason(signal: SetupSignal, zone: Zone) -> str | None:
    if zone is Zone.NEUTRAL and signal.aggressive:
        return "neutral zone (R1 to S1): no aggressive entries, wait for the standard trigger"
    return None
