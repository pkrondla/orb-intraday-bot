"""GrowwTradingClient: whitelist, constants, retries."""

from __future__ import annotations

from typing import Any

import pytest

from orb_intraday_bot.adapters.groww.trading_client import (
    ALLOWED_METHODS,
    ORDER_METHODS,
    ForbiddenTradingCall,
    GrowwTradingClient,
)


class _Tokens:
    def __init__(self) -> None:
        self.token = "tok"
        self.invalidated = 0

    def get_token(self) -> str:
        return self.token

    def invalidate(self) -> None:
        self.invalidated += 1


class _Api:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.fail_with: Exception | None = None
        self.fail_times = 0

    def place_order(self, **kwargs: Any) -> dict[str, Any]:
        if self.fail_with and self.fail_times > 0:
            self.fail_times -= 1
            raise self.fail_with
        self.calls.append(("place_order", (), kwargs))
        return {"groww_order_id": "1"}

    def get_order_status(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_order_status", (), kwargs))
        return {"order_status": "COMPLETE"}

    def get_ltp(self, **kwargs: Any) -> dict[str, Any]:
        return {"X": 1.0}


def test_order_methods_are_allowed() -> None:
    assert ORDER_METHODS <= ALLOWED_METHODS


def test_place_order_works_and_unknown_is_blocked() -> None:
    api = _Api()
    client = GrowwTradingClient(_Tokens(), api_factory=lambda _: api, sleep=lambda _: None)
    assert client.place_order(quantity=1)["groww_order_id"] == "1"
    with pytest.raises(ForbiddenTradingCall):
        client.get_holdings_for_user()  # type: ignore[attr-defined]


def test_auth_error_reauths_once() -> None:
    api = _Api()
    api.fail_with = RuntimeError("401 unauthorized")
    api.fail_times = 1
    tokens = _Tokens()
    client = GrowwTradingClient(tokens, api_factory=lambda _: api, sleep=lambda _: None)
    assert client.place_order(quantity=1)["groww_order_id"] == "1"
    assert tokens.invalidated == 1


def test_transient_retries() -> None:
    api = _Api()
    api.fail_with = RuntimeError("503 server error")
    api.fail_times = 2
    client = GrowwTradingClient(_Tokens(), api_factory=lambda _: api, sleep=lambda _: None)
    assert client.place_order(quantity=1)["groww_order_id"] == "1"


def test_dir_lists_allowed() -> None:
    client = GrowwTradingClient(_Tokens(), api_factory=lambda _: _Api())
    assert "place_order" in dir(client)


def test_private_attr_raises() -> None:
    client = GrowwTradingClient(_Tokens(), api_factory=lambda _: _Api())
    with pytest.raises(AttributeError):
        _ = client._not_real  # noqa: SLF001


def test_hard_failure_propagates() -> None:
    api = _Api()
    api.fail_with = RuntimeError("permanent failure xyz")
    api.fail_times = 10
    client = GrowwTradingClient(_Tokens(), api_factory=lambda _: api, sleep=lambda _: None)
    with pytest.raises(RuntimeError):
        client.place_order(quantity=1)
