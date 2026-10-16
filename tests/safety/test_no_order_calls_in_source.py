"""Static check: no source file may reference a broker order method (except the block-list)."""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
FORBIDDEN = re.compile(
    r"\b(place_order|modify_order|cancel_order|create_smart_order|"
    r"modify_smart_order|cancel_smart_order)\b"
)
ALLOWED_FILES = {"client.py"}  # holds the BLOCKED_METHODS names


def test_no_order_methods_referenced() -> None:
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        if path.name in ALLOWED_FILES:
            continue
        if FORBIDDEN.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(SRC)))
    assert not offenders, f"Order-method references found in: {offenders}"
