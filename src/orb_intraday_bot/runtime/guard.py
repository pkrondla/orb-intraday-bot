"""Startup safety checks. The bot refuses to run if any of these fail.

Real orders need all of: LIVE_MODE=live, LIVE_ARMED=true, trading credentials and DRY_RUN=false.
Telegram /stop and /start switch a day off and on. DRY_RUN defaults to true, so a missing or
misspelt setting never sends an order. The market-data client stays read-only whatever the mode is.
"""

from __future__ import annotations

from orb_intraday_bot.adapters.groww.client import (
    ALLOWED_METHODS,
    BLOCKED_METHODS,
    ReadOnlyGrowwClient,
)
from orb_intraday_bot.config.settings import Settings
from orb_intraday_bot.core.errors import SafetyViolationError


class SafetyError(SafetyViolationError, RuntimeError):
    pass


def assert_order_config(settings: Settings) -> None:
    """Real orders (DRY_RUN=false) only make sense in live mode, armed, with credentials."""
    if settings.dry_run:
        return
    if settings.live_mode != "live":
        raise SafetyError(
            f"DRY_RUN=false needs LIVE_MODE=live (it is {settings.live_mode!r}). "
            "Set DRY_RUN=true or LIVE_MODE=live."
        )
    if not settings.live_armed:
        raise SafetyError("DRY_RUN=false needs LIVE_ARMED=true.")
    if not settings.has_trading_credentials():
        raise SafetyError("DRY_RUN=false needs the Groww TOTP credentials.")


def assert_no_order_surface() -> None:
    overlap = ALLOWED_METHODS & BLOCKED_METHODS
    if overlap:
        raise SafetyError(f"Order methods present in the allow-list: {sorted(overlap)}")
    for name in BLOCKED_METHODS:
        if hasattr(ReadOnlyGrowwClient, name):
            raise SafetyError(f"ReadOnlyGrowwClient exposes order method '{name}'")


def run_startup_checks(settings: Settings) -> None:
    assert_order_config(settings)
    assert_no_order_surface()
