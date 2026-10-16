"""Static check: broker order methods may exist only in the trading client (and its tests)."""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
FORBIDDEN = re.compile(
    r"\b(place_order|modify_order|cancel_order|create_smart_order|"
    r"modify_smart_order|cancel_smart_order)\b"
)
# trading_client.py owns the names; venue.py calls them via a Protocol / injected client;
# client.py still lists them in BLOCKED_METHODS for the read-only guard.
ALLOWED_FILES = {
    "trading_client.py",
    "venue.py",
    "client.py",
}


def test_order_methods_only_in_allowed_files() -> None:
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        if path.name in ALLOWED_FILES:
            continue
        if FORBIDDEN.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(SRC)))
    assert not offenders, f"Order-method references found in: {offenders}"


def test_readonly_client_still_blocks_orders() -> None:
    from orb_intraday_bot.adapters.groww.client import (
        BLOCKED_METHODS,
        ForbiddenBrokerCall,
        ReadOnlyGrowwClient,
    )

    class Tokens:
        def get_token(self) -> str:
            return "x"

        def invalidate(self) -> None:
            return None

    client = ReadOnlyGrowwClient(Tokens())  # type: ignore[arg-type]
    for name in BLOCKED_METHODS:
        try:
            getattr(client, name)
            raise AssertionError(f"{name} should be blocked")
        except ForbiddenBrokerCall:
            pass
