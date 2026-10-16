"""Groww account reads: available option-buying funds and net positions (both read-only).

Response fields follow the Groww API: `fno_margin_details.option_buy_balance_available` for funds,
and `positions[].trading_symbol` with a net `quantity` (or `credit_quantity - debit_quantity`).
Anything that does not parse is returned as None so the caller never acts on a guess.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

log = logging.getLogger("orb.groww.account")


class AccountApi(Protocol):
    def get_available_margin_details(self, **kwargs: Any) -> dict[str, Any]: ...
    def get_positions_for_user(self, **kwargs: Any) -> dict[str, Any]: ...


def _number(raw: Any) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value == value else None   # NaN check


class GrowwAccountReader:
    def __init__(self, client: AccountApi) -> None:
        self._client = client

    def available_option_margin(self) -> float | None:
        try:
            raw = self._client.get_available_margin_details()
        except Exception as exc:
            log.warning("margin lookup failed: %s: %s", type(exc).__name__, exc)
            return None
        fno = (raw or {}).get("fno_margin_details") or {}
        return _number(fno.get("option_buy_balance_available"))

    def net_quantities(self) -> dict[str, int] | None:
        try:
            raw = self._client.get_positions_for_user(segment="FNO")
        except Exception as exc:
            log.warning("positions lookup failed: %s: %s", type(exc).__name__, exc)
            return None
        rows = (raw or {}).get("positions")
        if not isinstance(rows, list):
            return None
        out: dict[str, int] = {}
        for row in rows:
            if not isinstance(row, dict) or not row.get("trading_symbol"):
                continue
            net = _number(row.get("quantity"))
            if net is None:
                credit = _number(row.get("credit_quantity"))
                debit = _number(row.get("debit_quantity"))
                if credit is None or debit is None:
                    return None            # a shape we do not understand: do not guess
                net = credit - debit
            symbol = str(row["trading_symbol"])
            out[symbol] = out.get(symbol, 0) + int(net)
        return {s: q for s, q in out.items() if q != 0}
