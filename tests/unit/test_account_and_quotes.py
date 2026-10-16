"""Groww account and quote parsing, the read-only trading view, and rounding helpers."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from orb_intraday_bot.adapters.groww.account import GrowwAccountReader
from orb_intraday_bot.adapters.groww.quotes import GrowwQuoteSource
from orb_intraday_bot.adapters.groww.trading_client import (
    ORDER_METHODS,
    READ_METHODS,
    AccountReadView,
    ForbiddenTradingCall,
    GrowwTradingClient,
)
from orb_intraday_bot.core.clock import IST, FixedClock
from orb_intraday_bot.execution.quotes import (
    NoQuoteError,
    Quote,
    round_down_to_tick,
    round_up_to_tick,
)

CLOCK = FixedClock(datetime(2026, 10, 16, 10, tzinfo=IST))


class FakeApi:
    def __init__(self, **responses: Any) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _answer(self, name: str, kwargs: dict[str, Any]) -> Any:
        self.calls.append((name, kwargs))
        value = self.responses[name]
        if isinstance(value, Exception):
            raise value
        return value

    def get_quote(self, **kwargs: Any) -> Any:
        return self._answer("get_quote", kwargs)

    def get_available_margin_details(self, **kwargs: Any) -> Any:
        return self._answer("get_available_margin_details", kwargs)

    def get_positions_for_user(self, **kwargs: Any) -> Any:
        return self._answer("get_positions_for_user", kwargs)


# ---- quotes ------------------------------------------------------------------------------
def test_a_quote_reads_bid_ask_and_last_price() -> None:
    api = FakeApi(get_quote={"bid_price": 99.5, "offer_price": 100.5, "last_price": 100.0})
    quote = GrowwQuoteSource(api, clock=CLOCK).quote("X")
    assert (quote.bid, quote.ask, quote.ltp) == (99.5, 100.5, 100.0)
    assert api.calls[0][1] == {"exchange": "NSE", "segment": "FNO", "trading_symbol": "X"}
    assert quote.time == CLOCK.now()


def test_a_quote_falls_back_to_the_depth_when_top_prices_are_missing() -> None:
    api = FakeApi(
        get_quote={
            "bid_price": 0, "offer_price": None, "last_price": 100.0,
            "depth": {"buy": [{"price": 99.0, "quantity": 75}], "sell": [{"price": 101.0}]},
        }
    )
    quote = GrowwQuoteSource(api, clock=CLOCK).quote("X")
    assert (quote.bid, quote.ask) == (99.0, 101.0)


def test_a_one_sided_quote_keeps_the_missing_side_as_none() -> None:
    api = FakeApi(get_quote={"offer_price": 101.0, "last_price": 100.0})
    quote = GrowwQuoteSource(api, clock=CLOCK).quote("X")
    assert quote.bid is None and quote.ask == 101.0 and quote.spread_pct is None


@pytest.mark.parametrize(
    "answer", [{}, None, [], {"bid_price": "n/a"}, {"last_price": float("nan")}]
)
def test_an_unusable_quote_is_an_error_not_a_guess(answer: Any) -> None:
    with pytest.raises(NoQuoteError):
        GrowwQuoteSource(FakeApi(get_quote=answer), clock=CLOCK).quote("X")


def test_a_failing_quote_call_is_a_no_quote_error() -> None:
    api = FakeApi(get_quote=TimeoutError("slow"))
    with pytest.raises(NoQuoteError, match="TimeoutError"):
        GrowwQuoteSource(api, clock=CLOCK).quote("X")


def test_a_data_error_passes_through() -> None:
    from orb_intraday_bot.core.errors import DataUnavailableError

    api = FakeApi(get_quote=DataUnavailableError("no data"))
    with pytest.raises(DataUnavailableError):
        GrowwQuoteSource(api, clock=CLOCK).quote("X")


def test_spread_is_relative_to_the_mid_price() -> None:
    assert Quote("X", 98.0, 102.0, 100.0, CLOCK.now()).spread_pct == pytest.approx(0.04)
    assert Quote("X", 0.0, 102.0, 100.0, CLOCK.now()).spread_pct is None


def test_tick_rounding_goes_the_safe_way_for_each_side() -> None:
    assert round_up_to_tick(153.204, 0.05) == 153.25
    assert round_up_to_tick(153.2, 0.05) == 153.2
    assert round_down_to_tick(124.4025, 0.05) == 124.4
    assert round_down_to_tick(124.4, 0.05) == 124.4


# ---- account -----------------------------------------------------------------------------
def test_available_option_margin_reads_the_option_buy_balance() -> None:
    funds = {"fno_margin_details": {"option_buy_balance_available": 52000.5}}
    api = FakeApi(get_available_margin_details=funds)
    assert GrowwAccountReader(api).available_option_margin() == 52000.5


@pytest.mark.parametrize(
    "answer",
    [
        {}, None, {"fno_margin_details": {}},
        {"fno_margin_details": {"option_buy_balance_available": "x"}},
    ],
)
def test_unknown_margin_is_none(answer: Any) -> None:
    api = FakeApi(get_available_margin_details=answer)
    assert GrowwAccountReader(api).available_option_margin() is None


def test_a_failing_margin_call_is_none() -> None:
    api = FakeApi(get_available_margin_details=OSError("down"))
    assert GrowwAccountReader(api).available_option_margin() is None


def test_positions_are_net_quantities_by_symbol_without_flat_ones() -> None:
    api = FakeApi(
        get_positions_for_user={
            "positions": [
                {"trading_symbol": "A", "quantity": 75},
                {"trading_symbol": "B", "credit_quantity": 150, "debit_quantity": 75},
                {"trading_symbol": "C", "quantity": 0},
                {"trading_symbol": "A", "quantity": -25},
                {"quantity": 5},
                "junk",
            ]
        }
    )
    assert GrowwAccountReader(api).net_quantities() == {"A": 50, "B": 75}
    assert api.calls[0][1] == {"segment": "FNO"}


@pytest.mark.parametrize(
    "answer",
    [
        None, {}, {"positions": "x"},
        {"positions": [{"trading_symbol": "A", "credit_quantity": 5}]},
    ],
)
def test_positions_in_a_shape_we_do_not_understand_are_unknown(answer: Any) -> None:
    api = FakeApi(get_positions_for_user=answer)
    assert GrowwAccountReader(api).net_quantities() is None


def test_a_failing_positions_call_is_unknown() -> None:
    api = FakeApi(get_positions_for_user=OSError("down"))
    assert GrowwAccountReader(api).net_quantities() is None


# ---- the read-only view of the trading client --------------------------------------------
def _client() -> GrowwTradingClient:
    class Tokens:
        def get_token(self) -> str:
            return "t"

        def invalidate(self) -> None:
            return None

    return GrowwTradingClient(Tokens(), api_factory=lambda token: object(), sleep=lambda _: None)


def test_the_read_view_exposes_reads_and_no_order_method() -> None:
    view = AccountReadView(_client())
    for name in READ_METHODS:
        assert callable(getattr(view, name))
    for name in ORDER_METHODS:
        with pytest.raises(ForbiddenTradingCall, match="dry run"):
            getattr(view, name)
    with pytest.raises(AttributeError):
        view.__getattr__("_anything_private")
    assert sorted(dir(view)) == sorted(READ_METHODS)
    assert not (set(dir(view)) & ORDER_METHODS)
