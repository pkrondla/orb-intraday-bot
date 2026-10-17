"""Selecting the Nifty futures contract from Groww's instruments file."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any


class NoContractError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class FuturesContract:
    trading_symbol: str
    groww_symbol: str
    exchange_token: str
    expiry: date
    lot_size: int
    tick_size: float


def parse_date(raw: Any) -> date:
    return date.fromisoformat(str(raw)[:10])


def select_futures_contract(
    rows: Iterable[Mapping[str, Any]],
    today: date,
    *,
    underlying: str = "NIFTY",
    roll_days_before_expiry: int = 0,
) -> FuturesContract:
    """The current-month contract.

    A contract is used while `expiry >= today + roll_days_before_expiry`. With 0 (default) the
    bot stays on the contract through its expiry day and rolls on the next session. With N, it
    rolls once fewer than N days remain.
    """
    threshold = today + timedelta(days=roll_days_before_expiry)
    candidates: list[tuple[date, Mapping[str, Any]]] = []
    for row in rows:
        if str(row.get("underlying_symbol", "")).upper() != underlying:
            continue
        if str(row.get("instrument_type", "")).upper() != "FUT":
            continue
        expiry = parse_date(row.get("expiry_date"))
        if expiry >= threshold:
            candidates.append((expiry, row))
    if not candidates:
        raise NoContractError(f"No {underlying} futures contract on or after {threshold}")
    expiry, row = min(candidates, key=lambda item: item[0])
    return FuturesContract(
        trading_symbol=str(row["trading_symbol"]),
        groww_symbol=str(row["groww_symbol"]),
        exchange_token=str(row["exchange_token"]),
        expiry=expiry,
        lot_size=int(float(row["lot_size"])),
        tick_size=float(row["tick_size"]),
    )
