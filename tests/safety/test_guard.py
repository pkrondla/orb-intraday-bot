from __future__ import annotations

import pytest

from orb_intraday_bot.config import Settings
from orb_intraday_bot.runtime.guard import (
    SafetyError,
    assert_no_order_surface,
    assert_order_config,
    run_startup_checks,
)


def _settings(**env: object) -> Settings:
    return Settings(_env_file=None, **env)  # type: ignore[call-arg]


def test_default_never_sends_orders() -> None:
    settings = _settings()
    assert settings.dry_run is True
    assert settings.live_mode == "paper"
    run_startup_checks(settings)


def test_dry_run_is_always_allowed_whatever_the_mode() -> None:
    for mode in ("paper", "shadow", "live"):
        assert_order_config(_settings(LIVE_MODE=mode, DRY_RUN=True))


def test_real_orders_need_live_mode() -> None:
    with pytest.raises(SafetyError, match="LIVE_MODE=live"):
        assert_order_config(_settings(LIVE_MODE="paper", DRY_RUN=False))
    with pytest.raises(SafetyError, match="LIVE_MODE=live"):
        assert_order_config(_settings(LIVE_MODE="shadow", DRY_RUN=False))


def test_real_orders_need_the_armed_flag() -> None:
    with pytest.raises(SafetyError, match="LIVE_ARMED"):
        assert_order_config(_settings(LIVE_MODE="live", DRY_RUN=False, LIVE_ARMED=False))


def test_real_orders_need_credentials() -> None:
    with pytest.raises(SafetyError, match="credentials"):
        assert_order_config(
            _settings(
                LIVE_MODE="live", DRY_RUN=False, LIVE_ARMED=True,
                GROWW_TOTP_TOKEN="", GROWW_TOTP_SECRET="",
            )
        )


def test_real_orders_pass_when_everything_is_set() -> None:
    assert_order_config(
        _settings(
            LIVE_MODE="live", DRY_RUN=False, LIVE_ARMED=True,
            GROWW_TOTP_TOKEN="token", GROWW_TOTP_SECRET="secret",
        )
    )


def test_no_order_surface() -> None:
    assert_no_order_surface()
