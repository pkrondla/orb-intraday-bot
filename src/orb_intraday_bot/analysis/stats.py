"""Performance statistics and sanity checks over many days of paper trades.

The same code reads a replay (`DayResult` lists) and the forward-test database, so the two can
be compared like for like.

Definitions:
- win: net option P&L after all charges above zero
- points: futures points per unit, lot-weighted over the lots closed (no charges)
- R: points divided by the trade's initial risk in points (entry to hard stop)
- profit factor: sum of winning net P&L over the sum of losing net P&L (absolute)
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date

from orb_intraday_bot.analysis.backtest import (
    FULL_SESSION_CANDLES,
    DayResult,
    SignalRecord,
)
from orb_intraday_bot.config.settings import StrategyConfig
from orb_intraday_bot.paper.models import Position


@dataclass(frozen=True, slots=True)
class TradeRow:
    day: date
    setup: str
    level: str
    direction: str
    zone: str
    points: float
    risk_points: float
    net: float
    gross: float
    charges: float
    exits: str
    t1_reached: bool
    minutes_held: float
    position: Position

    @property
    def r(self) -> float:
        return self.points / self.risk_points if self.risk_points else 0.0

    @property
    def win(self) -> bool:
        return self.net > 0


def trade_rows(day: date, positions: Iterable[Position]) -> list[TradeRow]:
    rows = []
    for p in positions:
        if p.is_open:
            continue
        end = p.closed_at or p.opened_at
        rows.append(
            TradeRow(
                day=day,
                setup=p.setup,
                level=p.level_name,
                direction=p.direction.value,
                zone=p.zone,
                points=p.futures_points,
                risk_points=abs(p.futures_entry - p.hard_stop),
                net=p.net_option_pnl,
                gross=p.gross_option_pnl,
                charges=p.total_charges,
                exits="+".join(sorted({f.reason.value for f in p.fills})),
                t1_reached=p.t1_done,
                minutes_held=(end - p.opened_at).total_seconds() / 60,
                position=p,
            )
        )
    return rows


def rows_from_results(results: Iterable[DayResult]) -> list[TradeRow]:
    return [r for res in results for r in trade_rows(res.day, res.positions)]


@dataclass(frozen=True, slots=True)
class Group:
    name: str
    trades: int
    wins: int
    points: float
    avg_points: float
    avg_r: float
    net: float
    charges: float
    profit_factor: float | None

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0


def summarize_group(name: str, rows: Sequence[TradeRow]) -> Group:
    wins = [r for r in rows if r.win]
    won = sum(r.net for r in rows if r.net > 0)
    lost = -sum(r.net for r in rows if r.net < 0)
    return Group(
        name=name,
        trades=len(rows),
        wins=len(wins),
        points=round(sum(r.points for r in rows), 1),
        avg_points=round(statistics.fmean(r.points for r in rows), 1) if rows else 0.0,
        avg_r=round(statistics.fmean(r.r for r in rows), 2) if rows else 0.0,
        net=round(sum(r.net for r in rows), 0),
        charges=round(sum(r.charges for r in rows), 0),
        profit_factor=round(won / lost, 2) if lost > 0 else None,
    )


def group_by(rows: Sequence[TradeRow], key: Callable[[TradeRow], str]) -> list[Group]:
    buckets: dict[str, list[TradeRow]] = defaultdict(list)
    for row in rows:
        buckets[key(row)].append(row)
    return sorted(
        (summarize_group(name, items) for name, items in buckets.items()),
        key=lambda g: -g.points,
    )


@dataclass(frozen=True, slots=True)
class Overall:
    days: int
    days_with_trades: int
    trades: int
    group: Group
    best_day_points: float
    worst_day_points: float
    max_drawdown_net: float
    losing_streak: int
    median_minutes_held: float


def overall(rows: Sequence[TradeRow], days: int) -> Overall:
    by_day: dict[date, float] = defaultdict(float)
    by_day_net: dict[date, float] = defaultdict(float)
    for row in rows:
        by_day[row.day] += row.points
        by_day_net[row.day] += row.net
    peak = equity = drawdown = 0.0
    for day in sorted(by_day_net):
        equity += by_day_net[day]
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    streak = longest = 0
    for row in sorted(rows, key=lambda r: (r.day, r.position.opened_at)):
        streak = 0 if row.win else streak + 1
        longest = max(longest, streak)
    return Overall(
        days=days,
        days_with_trades=len(by_day),
        trades=len(rows),
        group=summarize_group("all", rows),
        best_day_points=round(max(by_day.values(), default=0.0), 1),
        worst_day_points=round(min(by_day.values(), default=0.0), 1),
        max_drawdown_net=round(drawdown, 0),
        losing_streak=longest,
        median_minutes_held=round(
            statistics.median(r.minutes_held for r in rows) if rows else 0.0, 0
        ),
    )


# ---- text ----------------------------------------------------------------------------------
def _row(g: Group, width: int) -> str:
    pf = "-" if g.profit_factor is None else f"{g.profit_factor:.2f}"
    return (
        f"  {g.name:<{width}} {g.trades:>4} {g.win_rate:>5.0%} {g.points:>+9.1f} "
        f"{g.avg_points:>+7.1f} {g.avg_r:>+6.2f} {pf:>6} {g.net:>+11,.0f}"
    )


def format_groups(title: str, groups: Sequence[Group]) -> str:
    width = max([len(g.name) for g in groups] + [len(title), 8])
    head = (
        f"  {title:<{width}} {'n':>4} {'win':>5} {'points':>9} {'avg':>7} {'avgR':>6} "
        f"{'PF':>6} {'net Rs':>11}"
    )
    return "\n".join([head, *(_row(g, width) for g in groups)])


def format_report(
    rows: Sequence[TradeRow], days: int, signals: Sequence[SignalRecord] = ()
) -> str:
    if not rows:
        return "No closed trades."
    o = overall(rows, days)
    g = o.group
    pf = "-" if g.profit_factor is None else f"{g.profit_factor:.2f}"
    lines = [
        f"{o.trades} trades on {o.days_with_trades} of {o.days} days "
        f"({o.trades / max(o.days, 1):.1f} per day) | win rate {g.win_rate:.0%} | "
        f"profit factor {pf}",
        f"futures {g.points:+.1f} pts (avg {g.avg_points:+.1f} per trade, {g.avg_r:+.2f} R) | "
        f"option Rs {g.net:+,.0f} net after Rs {g.charges:,.0f} charges "
        "(synthetic premium in a replay)",
        f"best day {o.best_day_points:+.1f} pts, worst day {o.worst_day_points:+.1f} pts | "
        f"max drawdown Rs {o.max_drawdown_net:,.0f} | longest losing streak {o.losing_streak} | "
        f"median hold {o.median_minutes_held:.0f} min",
        "",
        format_groups("by setup", group_by(rows, lambda r: r.setup)),
        "",
        format_groups("by direction", group_by(rows, lambda r: r.direction)),
        "",
        format_groups("by zone", group_by(rows, lambda r: r.zone)),
        "",
        format_groups("by exits", group_by(rows, lambda r: r.exits)),
    ]
    if signals:
        counts = Counter(
            f"{s.status}: {s.stage or 'taken'}" for s in signals if s.status != "accepted"
        )
        accepted = sum(1 for s in signals if s.status == "accepted")
        lines += ["", f"signals: {accepted} accepted"]
        lines += [f"  {n:>4}  {k}" for k, n in counts.most_common()]
    return "\n".join(lines)


# ---- sanity checks -------------------------------------------------------------------------
def check_results(results: Sequence[DayResult], cfg: StrategyConfig) -> list[str]:
    """Things that must hold in any replay. Returns the problems found (empty is good)."""
    problems: list[str] = []
    seen_ids: set[str] = set()
    for res in results:
        label = str(res.day)
        taken = [p for p in res.positions]
        if len(taken) > cfg.arbiter.max_trades_per_day:
            problems.append(f"{label}: {len(taken)} trades, above the daily cap")
        for p in taken:
            if p.position_id in seen_ids:
                problems.append(f"{label}: duplicate position {p.position_id}")
            seen_ids.add(p.position_id)
            if p.is_open:
                problems.append(f"{label}: {p.setup} still open after the session")
                continue
            if sum(f.lots for f in p.fills) != p.lots:
                problems.append(f"{label}: {p.setup} fills do not add up to {p.lots} lots")
            risk = abs(p.futures_entry - p.hard_stop)
            if risk > cfg.stops.hard_stop_points + 0.01:
                problems.append(f"{label}: {p.setup} stop is {risk:.1f} pts, above the cap")
            if p.futures_entry and (p.t1 - p.futures_entry) * p.sign <= 0:
                problems.append(f"{label}: {p.setup} T1 is behind the entry")
            if (p.t2 - p.t1) * p.sign < 0:
                problems.append(f"{label}: {p.setup} T2 is before T1")
        if not cfg.arbiter.allow_opposite_positions:
            for i, a in enumerate(taken):
                for b in taken[i + 1 :]:
                    if a.direction is b.direction or a.closed_at is None or b.closed_at is None:
                        continue
                    if a.opened_at < b.closed_at and b.opened_at < a.closed_at:
                        problems.append(f"{label}: {a.setup} and {b.setup} overlap, opposite")
    return problems


def data_quality(results: Sequence[DayResult]) -> str | None:
    """One line on sessions with missing candles (thin trading or a feed gap), or None."""
    short = [r for r in results if r.candle_count != FULL_SESSION_CANDLES]
    if not short:
        return None
    missing = sum(FULL_SESSION_CANDLES - r.candle_count for r in short)
    worst = min(short, key=lambda r: r.candle_count)
    return (
        f"Data: {len(short)} of {len(results)} sessions have missing candles ({missing} in "
        f"total; worst {worst.day} with {worst.candle_count}). "
        "Use --min-candles 125 for complete days only."
    )
