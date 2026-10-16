"""Staged arming: paper -> shadow -> live.

Live requires ALL of: LIVE_MODE=live, LIVE_ARMED=true, trading credentials present, and a
daily Telegram `/arm <code>` confirmation. Anything missing falls back to paper and is reported.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from orb_intraday_bot.config.settings import Settings

Mode = Literal["paper", "shadow", "live"]


@dataclass(frozen=True, slots=True)
class ArmingDecision:
    mode: Mode
    reason: str

    @property
    def is_live(self) -> bool:
        return self.mode == "live"


def resolve_mode(
    settings: Settings,
    *,
    armed_today: bool,
    trading_credentials_present: bool,
) -> ArmingDecision:
    requested: Mode = settings.live_mode
    if requested == "paper":
        return ArmingDecision("paper", "LIVE_MODE=paper")
    if requested == "shadow":
        return ArmingDecision("shadow", "LIVE_MODE=shadow")
    # requested == live
    if not settings.live_armed:
        return ArmingDecision(
            "paper", "LIVE_MODE=live but LIVE_ARMED is false; falling back to paper"
        )
    if not trading_credentials_present:
        return ArmingDecision(
            "paper", "LIVE_MODE=live but trading credentials missing; falling back to paper"
        )
    if not armed_today:
        return ArmingDecision(
            "paper",
            "LIVE_MODE=live but not armed via Telegram /arm today; falling back to paper",
        )
    return ArmingDecision("live", "armed for live trading today")
