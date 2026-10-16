"""Events emitted by indicators and (later) patterns, signals and the trade engine."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal


@dataclass(frozen=True, slots=True)
class LevelSet:
    """An opening-range level was fixed at the close of `candle_start`'s candle."""

    kind: Literal["ORH", "ORL"]
    value: float
    candle_start: datetime
