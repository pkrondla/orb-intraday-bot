"""Builds Groww clients from Settings (data-only and optional trading keys)."""

from __future__ import annotations

from orb_intraday_bot.adapters.groww.client import ReadOnlyGrowwClient
from orb_intraday_bot.adapters.groww.token import TokenManager
from orb_intraday_bot.adapters.groww.trading_client import GrowwTradingClient
from orb_intraday_bot.config.settings import Settings
from orb_intraday_bot.core.errors import ConfigurationError


class MissingCredentialsError(ConfigurationError, RuntimeError):
    pass


def build_token_manager(settings: Settings) -> TokenManager:
    totp_token = settings.groww_totp_token.get_secret_value()
    totp_secret = settings.groww_totp_secret.get_secret_value()
    if not totp_token or not totp_secret:
        raise MissingCredentialsError("Set GROWW_TOTP_TOKEN and GROWW_TOTP_SECRET in .env.")
    return TokenManager(
        totp_token,
        totp_secret,
        settings.data_dir / "groww_token.json",
        cooldown_sec=settings.token_cooldown_sec,
    )


def build_readonly_client(settings: Settings) -> ReadOnlyGrowwClient:
    return ReadOnlyGrowwClient(build_token_manager(settings))


def build_trading_client(settings: Settings) -> GrowwTradingClient:
    """Trading client. Refuses to build without a separate trading key."""
    token = settings.groww_trading_totp_token.get_secret_value()
    secret = settings.groww_trading_totp_secret.get_secret_value()
    if not token or not secret:
        raise MissingCredentialsError(
            "Set GROWW_TRADING_TOTP_TOKEN and GROWW_TRADING_TOTP_SECRET for live trading."
        )
    # Refuse to reuse the data-only key.
    if (
        token == settings.groww_totp_token.get_secret_value()
        and secret == settings.groww_totp_secret.get_secret_value()
    ):
        raise MissingCredentialsError(
            "Trading TOTP key must be different from the data-only GROWW_TOTP_* key."
        )
    manager = TokenManager(
        token,
        secret,
        settings.data_dir / "groww_trading_token.json",
        cooldown_sec=settings.token_cooldown_sec,
    )
    return GrowwTradingClient(manager)
