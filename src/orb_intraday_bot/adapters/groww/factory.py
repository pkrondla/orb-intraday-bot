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
    """Trading client. Uses GROWW_TRADING_TOTP_* if set, otherwise the same GROWW_TOTP_* key."""
    token = settings.groww_trading_totp_token.get_secret_value()
    secret = settings.groww_trading_totp_secret.get_secret_value()
    if not token or not secret:
        token = settings.groww_totp_token.get_secret_value()
        secret = settings.groww_totp_secret.get_secret_value()
    if not token or not secret:
        raise MissingCredentialsError(
            "Set GROWW_TOTP_TOKEN and GROWW_TOTP_SECRET for live trading "
            "(or GROWW_TRADING_TOTP_* to override)."
        )
    # Share the data token cache when using the same key so TOTP is not fetched twice.
    cache = (
        settings.data_dir / "groww_token.json"
        if token == settings.groww_totp_token.get_secret_value()
        and secret == settings.groww_totp_secret.get_secret_value()
        else settings.data_dir / "groww_trading_token.json"
    )
    manager = TokenManager(
        token,
        secret,
        cache,
        cooldown_sec=settings.token_cooldown_sec,
    )
    return GrowwTradingClient(manager)
