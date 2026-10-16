"""Startup safety checks. The bot refuses to run if any of these fail."""

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


def assert_paper_mode(settings: Settings) -> None:
    if settings.live_trading:
        raise SafetyError("LIVE_TRADING must be false. This bot never places orders.")


def assert_no_order_surface() -> None:
    overlap = ALLOWED_METHODS & BLOCKED_METHODS
    if overlap:
        raise SafetyError(f"Order methods present in the allow-list: {sorted(overlap)}")
    for name in BLOCKED_METHODS:
        if hasattr(ReadOnlyGrowwClient, name):
            raise SafetyError(f"ReadOnlyGrowwClient exposes order method '{name}'")


def run_startup_checks(settings: Settings) -> None:
    assert_paper_mode(settings)
    assert_no_order_surface()
