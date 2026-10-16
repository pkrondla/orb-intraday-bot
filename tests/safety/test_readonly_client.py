from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from orb_intraday_bot.adapters.groww import (
    ALLOWED_METHODS,
    BLOCKED_METHODS,
    ForbiddenBrokerCall,
    ReadOnlyGrowwClient,
    TokenManager,
)


class FakeApi:
    def __init__(self, token: str, fail_with: list[Exception] | None = None) -> None:
        self.token = token
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self._fail_with = fail_with or []

    def get_ltp(self, *args: Any, **kwargs: Any) -> dict[str, float]:
        self.calls.append(("get_ltp", args, kwargs))
        if self._fail_with:
            raise self._fail_with.pop(0)
        return {"NSE_X": 1.0}

    # Order and account methods exist on the SDK object. They must stay unreachable.
    def place_order(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("place_order must never be called")

    def get_positions_for_user(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("positions must never be called")


def make_client(tmp_path: Path, apis: list[FakeApi]) -> tuple[ReadOnlyGrowwClient, TokenManager]:
    tokens = TokenManager(
        "key", "secret", tmp_path / "tok.json", fetcher=lambda k, s: "tok"
    )
    it = iter(apis)
    client = ReadOnlyGrowwClient(tokens, api_factory=lambda t: next(it), sleep=lambda s: None)
    return client, tokens


def test_allowed_method_passes_through(tmp_path: Path) -> None:
    api = FakeApi("tok")
    client, _ = make_client(tmp_path, [api])
    assert client.get_ltp(exchange_trading_symbols="NSE_X", segment="FNO") == {"NSE_X": 1.0}
    assert api.calls[0][2] == {"exchange_trading_symbols": "NSE_X", "segment": "FNO"}


@pytest.mark.parametrize("name", sorted(BLOCKED_METHODS))
def test_order_methods_unreachable(tmp_path: Path, name: str) -> None:
    client, _ = make_client(tmp_path, [FakeApi("tok")])
    assert not hasattr(client, name)
    with pytest.raises(ForbiddenBrokerCall):
        getattr(client, name)


@pytest.mark.parametrize("name", ["get_positions_for_user", "get_holdings_for_user"])
def test_account_endpoints_unreachable(tmp_path: Path, name: str) -> None:
    client, _ = make_client(tmp_path, [FakeApi("tok")])
    with pytest.raises(ForbiddenBrokerCall):
        getattr(client, name)


def test_allowlist_has_no_order_methods() -> None:
    assert not (ALLOWED_METHODS & BLOCKED_METHODS)
    assert not any("order" in m for m in ALLOWED_METHODS)


def test_transient_error_is_retried(tmp_path: Path) -> None:
    api = FakeApi("tok", fail_with=[RuntimeError("503 server error")])
    client, _ = make_client(tmp_path, [api])
    assert client.get_ltp(exchange_trading_symbols="NSE_X", segment="FNO")
    assert len(api.calls) == 2


def test_auth_error_rebuilds_client_once(tmp_path: Path) -> None:
    first = FakeApi("tok", fail_with=[RuntimeError("403 Access forbidden")])
    second = FakeApi("tok")
    client, _ = make_client(tmp_path, [first, second])
    assert client.get_ltp(exchange_trading_symbols="NSE_X", segment="FNO")
    assert len(second.calls) == 1


def test_non_transient_error_is_not_retried(tmp_path: Path) -> None:
    api = FakeApi("tok", fail_with=[ValueError("bad symbol")])
    client, _ = make_client(tmp_path, [api])
    with pytest.raises(ValueError):
        client.get_ltp(exchange_trading_symbols="NSE_X", segment="FNO")
    assert len(api.calls) == 1
