from __future__ import annotations

import pytest
from pydantic import ValidationError

from orb_intraday_bot.config import Settings
from orb_intraday_bot.runtime.guard import assert_no_order_surface, run_startup_checks


def test_live_trading_true_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, LIVE_TRADING=True)  # type: ignore[call-arg]


def test_default_is_paper() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.live_trading is False
    run_startup_checks(settings)


def test_no_order_surface() -> None:
    assert_no_order_surface()
