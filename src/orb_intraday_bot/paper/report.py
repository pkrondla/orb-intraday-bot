"""Trade and day summaries, and the text used in the per-candle report."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from orb_intraday_bot.paper.models import Position


@dataclass(frozen=True, slots=True)
class DaySummary:
    day: date
    trades: int
    open_trades: int
    wins: int
    losses: int
    gross_option_pnl: float
    charges: float
    net_option_pnl: float
    futures_points: float            # sum over closed trades of the lot-weighted points

    @property
    def win_rate(self) -> float | None:
        closed = self.wins + self.losses
        return None if closed == 0 else self.wins / closed


def summarize(day: date, positions: Iterable[Position]) -> DaySummary:
    items = list(positions)
    closed = [p for p in items if not p.is_open]
    return DaySummary(
        day=day,
        trades=len(items),
        open_trades=len(items) - len(closed),
        wins=sum(1 for p in closed if p.net_option_pnl > 0),
        losses=sum(1 for p in closed if p.net_option_pnl <= 0),
        gross_option_pnl=round(sum(p.gross_option_pnl for p in closed), 2),
        charges=round(sum(p.total_charges for p in closed), 2),
        net_option_pnl=round(sum(p.net_option_pnl for p in closed), 2),
        futures_points=round(sum(p.futures_points for p in closed), 2),
    )


def format_open_position(position: Position, futures_price: float, option_price: float) -> str:
    move = position.sign * (futures_price - position.futures_entry)
    t2 = (
        f" T2 {position.t2:.1f}"
        if abs(position.t1 - position.t2) > position.tick_size
        else ""
    )
    return (
        f"{position.direction.value} {position.setup} {position.trading_symbol} "
        f"{position.remaining_lots}/{position.lots} lot(s) | entry {position.entry_ltp:.2f} "
        f"now {option_price:.2f} | futures {futures_price:.1f} ({move:+.1f} pts) | "
        f"open P&L Rs {position.unrealized(option_price):+,.0f} | "
        f"stop {position.active_stop:.1f}"
        f"{' (at entry)' if position.stop_at_entry else ''} T1 {position.t1:.1f}{t2}"
    )


def format_closed_position(position: Position) -> str:
    reasons = ", ".join(f"{f.reason.value}x{f.lots}" for f in position.fills) or "-"
    return (
        f"{position.direction.value} {position.setup} {position.trading_symbol} "
        f"{position.lots} lot(s) | exits {reasons} | futures {position.futures_points:+.1f} pts | "
        f"option Rs {position.gross_option_pnl:+,.0f} gross, charges Rs "
        f"{position.total_charges:,.0f}, net Rs {position.net_option_pnl:+,.0f}"
    )


def format_summary(summary: DaySummary) -> str:
    rate = "n/a" if summary.win_rate is None else f"{summary.win_rate:.0%}"
    return (
        f"{summary.day}: {summary.trades} trade(s), {summary.open_trades} open | closed "
        f"{summary.wins} win / {summary.losses} loss ({rate}) | futures "
        f"{summary.futures_points:+.1f} pts | option Rs {summary.gross_option_pnl:+,.0f} "
        f"gross - Rs {summary.charges:,.0f} charges = Rs {summary.net_option_pnl:+,.0f} net"
    )
