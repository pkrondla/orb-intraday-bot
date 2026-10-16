"""Charges on an option order (paper). All rates are config, see `paper.charges` in default.yaml.

    brokerage = min(flat per order, brokerage_pct x turnover)
    STT       = stt_sell_pct x turnover            (sell side only)
    exchange  = exchange_txn_pct x turnover        (both sides)
    SEBI      = sebi_pct x turnover                (both sides)
    stamp     = stamp_buy_pct x turnover           (buy side only)
    GST       = gst_pct x (brokerage + exchange + SEBI)

Turnover is premium x quantity. The rates are assumptions to be checked against a real contract
note.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from orb_intraday_bot.config.settings import ChargesConfig

Side = Literal["BUY", "SELL"]


@dataclass(frozen=True, slots=True)
class ChargeBreakdown:
    brokerage: float
    stt: float
    exchange: float
    sebi: float
    stamp: float
    gst: float

    @property
    def total(self) -> float:
        return round(
            self.brokerage + self.stt + self.exchange + self.sebi + self.stamp + self.gst, 2
        )


def order_charges(side: Side, price: float, quantity: int, cfg: ChargesConfig) -> ChargeBreakdown:
    turnover = max(price, 0.0) * max(quantity, 0)
    if turnover <= 0:
        return ChargeBreakdown(0, 0, 0, 0, 0, 0)
    brokerage = min(cfg.brokerage_flat_inr, cfg.brokerage_pct * turnover)
    exchange = cfg.exchange_txn_pct * turnover
    sebi = cfg.sebi_pct * turnover
    return ChargeBreakdown(
        brokerage=brokerage,
        stt=cfg.stt_sell_pct * turnover if side == "SELL" else 0.0,
        exchange=exchange,
        sebi=sebi,
        stamp=cfg.stamp_buy_pct * turnover if side == "BUY" else 0.0,
        gst=cfg.gst_pct * (brokerage + exchange + sebi),
    )
