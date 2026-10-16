"""Option chain and option contract models, and parsing of the Groww responses.

Groww chain shape (verified in the Phase 0 spike):

    {"underlying_ltp": 22965.35,
     "strikes": {"23000": {"CE": {"greeks": {"delta": 0.4615, ...}, "trading_symbol": "...",
                                  "ltp": 114.1, "open_interest": 143280, "volume": 0},
                           "PE": {...}}}}

Delta of 0 is how Groww reports "no value" (far strikes, stale quotes), so it is treated as
missing. A delta with the wrong sign for its side (a negative CE delta, a positive PE delta) is
also treated as missing rather than trusted.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any

from orb_intraday_bot.data.contract import parse_date


class OptionKind(StrEnum):
    CE = "CE"
    PE = "PE"


@dataclass(frozen=True, slots=True)
class OptionQuote:
    strike: int
    kind: OptionKind
    trading_symbol: str
    ltp: float | None          # None when there is no usable price
    delta: float | None        # absolute delta, None when missing or invalid
    iv: float | None = None
    open_interest: float = 0.0
    volume: float = 0.0


@dataclass(frozen=True, slots=True)
class OptionChain:
    expiry: date
    underlying_ltp: float | None
    quotes: Mapping[tuple[int, OptionKind], OptionQuote]

    def get(self, strike: int, kind: OptionKind) -> OptionQuote | None:
        return self.quotes.get((strike, kind))


@dataclass(frozen=True, slots=True)
class OptionContract:
    """Static data from the broker's instruments file."""

    trading_symbol: str
    groww_symbol: str
    expiry: date
    strike: int
    kind: OptionKind
    lot_size: int
    tick_size: float


def _number(raw: Any) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value == value else None   # NaN check


def _delta(raw: Any, kind: OptionKind) -> float | None:
    value = _number(raw)
    if value is None or value == 0:
        return None
    if kind is OptionKind.CE and value < 0:
        return None
    if kind is OptionKind.PE and value > 0:
        return None
    magnitude = abs(value)
    return magnitude if magnitude <= 1 else None


def parse_option_chain(response: Mapping[str, Any], expiry: date) -> OptionChain:
    quotes: dict[tuple[int, OptionKind], OptionQuote] = {}
    strikes = response.get("strikes") or {}
    for raw_strike, sides in strikes.items():
        try:
            strike = int(float(raw_strike))
        except (TypeError, ValueError):
            continue
        for kind in OptionKind:
            side = (sides or {}).get(kind.value)
            if not side:
                continue
            greeks = side.get("greeks") or {}
            ltp = _number(side.get("ltp"))
            iv = _number(greeks.get("iv"))
            quotes[(strike, kind)] = OptionQuote(
                strike=strike,
                kind=kind,
                trading_symbol=str(side.get("trading_symbol", "")),
                ltp=ltp if ltp is not None and ltp > 0 else None,
                delta=_delta(greeks.get("delta"), kind),
                iv=iv if iv else None,
                open_interest=_number(side.get("open_interest")) or 0.0,
                volume=_number(side.get("volume")) or 0.0,
            )
    return OptionChain(
        expiry=expiry,
        underlying_ltp=_number(response.get("underlying_ltp")),
        quotes=quotes,
    )


def parse_option_contracts(
    rows: Iterable[Mapping[str, Any]], expiry: date, underlying: str = "NIFTY"
) -> dict[str, OptionContract]:
    """Option contracts of one expiry, keyed by trading symbol."""
    out: dict[str, OptionContract] = {}
    for row in rows:
        if str(row.get("underlying_symbol", "")).upper() != underlying:
            continue
        kind_raw = str(row.get("instrument_type", "")).upper()
        if kind_raw not in {"CE", "PE"}:
            continue
        if parse_date(row.get("expiry_date")) != expiry:
            continue
        symbol = str(row["trading_symbol"])
        out[symbol] = OptionContract(
            trading_symbol=symbol,
            groww_symbol=str(row.get("groww_symbol", "")),
            expiry=expiry,
            strike=int(float(row["strike_price"])),
            kind=OptionKind(kind_raw),
            lot_size=int(float(row["lot_size"])),
            tick_size=float(row["tick_size"]),
        )
    return out
