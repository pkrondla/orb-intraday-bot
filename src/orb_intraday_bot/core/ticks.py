"""Quote steps. Nifty futures trade in tenths; option premiums trade in 0.05."""

from __future__ import annotations

from orb_intraday_bot.core.models import Candle

# Same step as InstrumentConfig.futures_tick_size. A pivot formula can land between
# these quotes; the tape cannot.
FUTURES_TICK = 0.1


def futures_quote(price: float, tick: float = FUTURES_TICK) -> float:
    """Nearest futures quote. 24315.85 is 24315.8; a price already on the tenth stays there."""
    cleaned = round(float(price), 10)  # drop binary dust so an exact half stays a half
    return round(round(cleaned / tick) * tick, 4)


def snap_candle(candle: Candle, tick: float = FUTURES_TICK) -> Candle:
    """OHLC on the futures quote. High and low stay outside the open and close."""
    open_ = futures_quote(candle.open, tick)
    high = futures_quote(candle.high, tick)
    low = futures_quote(candle.low, tick)
    close = futures_quote(candle.close, tick)
    high = max(high, open_, close)
    low = min(low, open_, close)
    if (open_, high, low, close) == (candle.open, candle.high, candle.low, candle.close):
        return candle
    return Candle(candle.start, open_, high, low, close, candle.volume, candle.oi)
