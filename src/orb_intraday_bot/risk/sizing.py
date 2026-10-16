"""Position sizing (spec section 6.3).

    lots = floor(risk amount / (option stop points * lot size))

The option stop points come from the delta conversion (Phase 6); this module only does the maths.
[Assumption] Capital 5,00,000 and risk 1% per trade give a Rs 5,000 budget.
"""

from __future__ import annotations

import math

from orb_intraday_bot.config.settings import SizingConfig


def risk_budget_inr(cfg: SizingConfig) -> float:
    return cfg.capital_inr * cfg.risk_pct_per_trade


def lots_for(risk_inr: float, option_sl_points: float, lot_size: int) -> int:
    """Whole lots that keep the loss at the option stop within the risk budget (0 if none fit)."""
    if risk_inr <= 0 or option_sl_points <= 0 or lot_size <= 0:
        return 0
    return math.floor(risk_inr / (option_sl_points * lot_size))
