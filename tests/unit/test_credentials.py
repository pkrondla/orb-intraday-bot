from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from orb_intraday_bot.adapters.groww import TokenManager
from orb_intraday_bot.adapters.groww.factory import (
    MissingCredentialsError,
    build_readonly_client,
    build_token_manager,
)
from orb_intraday_bot.adapters.groww.token import extract_token, totp_fetcher
from orb_intraday_bot.config import Settings


def settings(**env: str) -> Settings:
    return Settings(_env_file=None, **env)  # type: ignore[call-arg]


def test_totp_credentials_build_token_manager(tmp_path: Path) -> None:
    manager = build_token_manager(
        settings(GROWW_TOTP_TOKEN="t", GROWW_TOTP_SECRET="JBSWY3DPEHPK3PXP",
                 DATA_DIR=str(tmp_path))
    )
    assert isinstance(manager, TokenManager)
    assert manager._fetcher is totp_fetcher  # noqa: SLF001


def test_client_builds_with_totp_credentials(tmp_path: Path) -> None:
    client = build_readonly_client(
        settings(GROWW_TOTP_TOKEN="t", GROWW_TOTP_SECRET="JBSWY3DPEHPK3PXP",
                 DATA_DIR=str(tmp_path))
    )
    assert hasattr(client, "get_ltp")
    assert not hasattr(client, "place_order")


@pytest.mark.parametrize(
    "env",
    [{}, {"GROWW_TOTP_TOKEN": "t"}, {"GROWW_TOTP_SECRET": "s"}],
)
def test_missing_or_partial_credentials_raise(env: dict[str, str]) -> None:
    with pytest.raises(MissingCredentialsError):
        build_token_manager(settings(**env))


def test_old_credential_options_are_gone() -> None:
    fields = Settings.model_fields
    assert "groww_access_token" not in fields
    assert "groww_api_key" not in fields
    assert "groww_api_secret" not in fields


def test_totp_fetcher_sends_token_and_generated_code(monkeypatch: pytest.MonkeyPatch) -> None:
    import growwapi

    seen: dict[str, Any] = {}

    def fake_get_access_token(**kwargs: Any) -> str:
        seen.update(kwargs)
        return "access-123"

    monkeypatch.setattr(growwapi.GrowwAPI, "get_access_token", staticmethod(fake_get_access_token))
    assert totp_fetcher("totp-token", "JBSWY3DPEHPK3PXP") == "access-123"
    assert seen["api_key"] == "totp-token"
    assert len(seen["totp"]) == 6 and seen["totp"].isdigit()
    assert "secret" not in seen


def test_extract_token_accepts_string_and_dict() -> None:
    assert extract_token("abc") == "abc"
    assert extract_token({"token": "abc"}) == "abc"
    assert extract_token({"payload": {"accessToken": "abc"}}) == "abc"
    with pytest.raises(RuntimeError):
        extract_token({"nothing": 1})
