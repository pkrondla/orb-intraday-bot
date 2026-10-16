"""Staged arming: paper -> shadow -> live, and whether the broker order step runs.

Live requires ALL of: LIVE_MODE=live, LIVE_ARMED=true and Groww TOTP credentials present.
Anything missing falls back to paper and is reported. There is no daily confirmation: trading is
on every market day, and the operator can switch a day off with Telegram `/stop` (and back on
with `/start`).

What actually happens to an entry is the `order_mode`:

- `paper`    the paper engine only. No quotes, no orders, nothing sent anywhere.
- `dry_run`  the full live pipeline (risk gate, real bid/ask, order state machine, protective
             stop, exits, reconciliation, logs) with the broker order step skipped. `shadow`
             always runs this way, and so does `live` while DRY_RUN is true (the default).
- `live`     the same pipeline, with real orders. Only when LIVE_MODE=live, LIVE_ARMED=true and
             DRY_RUN=false.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from orb_intraday_bot.config.settings import Settings

Mode = Literal["paper", "shadow", "live"]
OrderMode = Literal["paper", "dry_run", "live"]


@dataclass(frozen=True, slots=True)
class ArmingDecision:
    mode: Mode
    reason: str
    dry_run: bool = True

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    @property
    def order_mode(self) -> OrderMode:
        """paper, dry_run (broker step skipped) or live (real orders)."""
        if self.mode == "paper":
            return "paper"
        if self.mode == "shadow" or self.dry_run:
            return "dry_run"
        return "live"


def resolve_mode(
    settings: Settings,
    *,
    trading_credentials_present: bool,
) -> ArmingDecision:
    requested: Mode = settings.live_mode
    dry = settings.dry_run
    if requested == "paper":
        return ArmingDecision("paper", "LIVE_MODE=paper", dry)
    if requested == "shadow":
        return ArmingDecision("shadow", "LIVE_MODE=shadow (orders simulated)", dry)
    # requested == live
    if not settings.live_armed:
        return ArmingDecision(
            "paper", "LIVE_MODE=live but LIVE_ARMED is false; falling back to paper", dry
        )
    if not trading_credentials_present:
        return ArmingDecision(
            "paper", "LIVE_MODE=live but trading credentials missing; falling back to paper", dry
        )
    if dry:
        return ArmingDecision("live", "DRY_RUN=true, broker orders are skipped", dry)
    return ArmingDecision("live", "live trading", dry)


def configured_mode_label(settings: Settings) -> str:
    """What the settings ask for, for the startup message ."""
    if settings.live_mode == "paper":
        return "paper (no orders)"
    if settings.live_mode == "shadow":
        return "shadow (dry run: broker orders skipped)"
    if settings.dry_run:
        return "live pipeline, DRY RUN (broker orders skipped); /stop and /start control each day"
    return "LIVE, real orders; /stop and /start control each day"


def describe_order_mode(order_mode: str) -> str:
    return {
        "paper": "PAPER (no orders, simulated fills)",
        "dry_run": "DRY RUN (live pipeline, broker order step skipped)",
        "live": "LIVE (real orders)",
    }.get(order_mode, order_mode)
