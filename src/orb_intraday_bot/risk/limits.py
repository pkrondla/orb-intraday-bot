"""Risk limit config (separate YAML from strategy rules)."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT = _PROJECT_ROOT / "config" / "risk.yaml"


class RiskLimits(BaseModel):
    max_lots_per_trade: int = Field(default=1, ge=1)
    max_trades_per_day: int = Field(default=3, ge=1)
    max_open_positions: int = Field(default=1, ge=1)
    daily_loss_limit_pct: float = Field(default=0.01, gt=0, le=1)
    loss_streak_halt: int = Field(default=2, ge=1)
    max_option_spread_pct: float = Field(default=0.05, gt=0, le=1)
    max_data_staleness_sec: int = Field(default=120, ge=10)
    square_off_time: str = "15:20"
    product: str = "MIS"
    entry_order_type: str = "LIMIT"
    entry_price_protection_pct: float = Field(default=0.02, ge=0, le=0.2)
    # How a live order is worked (also used, identically, by the dry run).
    entry_fill_timeout_sec: float = Field(default=10.0, ge=1, le=120)   # then cancel and skip
    exit_fill_timeout_sec: float = Field(default=6.0, ge=1, le=120)     # then a market sell
    exit_price_protection_pct: float = Field(default=0.03, ge=0, le=0.5)  # limit below the bid
    stop_limit_buffer_pct: float = Field(default=0.05, ge=0, le=0.5)    # stop limit below trigger
    order_poll_sec: float = Field(default=1.0, ge=0.2, le=10)
    reconcile_interval_sec: float = Field(default=60.0, ge=10, le=600)

    def daily_loss_limit_inr(self, capital_inr: float) -> float:
        return capital_inr * self.daily_loss_limit_pct


def load_risk_limits(path: Path | None = None) -> RiskLimits:
    target = path or _DEFAULT
    if not target.exists():
        return RiskLimits()
    raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    return RiskLimits.model_validate(raw)
