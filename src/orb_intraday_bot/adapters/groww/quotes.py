"""Groww option quotes with bid and ask (read-only, from the market-data client)."""

from __future__ import annotations

from typing import Any, Protocol

from orb_intraday_bot.core.clock import IST, Clock, SystemClock
from orb_intraday_bot.core.errors import DataUnavailableError
from orb_intraday_bot.execution.quotes import NoQuoteError, Quote


class QuoteApi(Protocol):
    def get_quote(self, **kwargs: Any) -> dict[str, Any]: ...


def _price(raw: Any) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value == value and value > 0 else None


def _depth_price(depth: Any, side: str) -> float | None:
    rows = (depth or {}).get(side) if isinstance(depth, dict) else None
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        return _price(rows[0].get("price"))
    return None


class GrowwQuoteSource:
    def __init__(
        self,
        client: QuoteApi,
        *,
        exchange: str = "NSE",
        segment: str = "FNO",
        clock: Clock | None = None,
    ) -> None:
        self._client = client
        self._exchange = exchange
        self._segment = segment
        self._clock = clock or SystemClock()

    def quote(self, symbol: str) -> Quote:
        try:
            raw = self._client.get_quote(
                exchange=self._exchange, segment=self._segment, trading_symbol=symbol
            )
        except DataUnavailableError:
            raise
        except Exception as exc:
            raise NoQuoteError(f"quote for {symbol} failed: {type(exc).__name__}: {exc}") from exc
        if not isinstance(raw, dict) or not raw:
            raise NoQuoteError(f"empty quote for {symbol}")
        depth = raw.get("depth")
        bid = _price(raw.get("bid_price")) or _depth_price(depth, "buy")
        ask = _price(raw.get("offer_price")) or _depth_price(depth, "sell")
        ltp = _price(raw.get("last_price"))
        if bid is None and ask is None and ltp is None:
            raise NoQuoteError(f"no usable prices in the quote for {symbol}")
        return Quote(symbol, bid, ask, ltp, self._clock.now().astimezone(IST))
