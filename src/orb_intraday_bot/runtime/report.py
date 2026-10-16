"""Text for the per-candle report and the trade alerts (spec section 8)."""

from __future__ import annotations

from datetime import timedelta

from orb_intraday_bot.paper.exits import ExitEvent
from orb_intraday_bot.paper.models import Position
from orb_intraday_bot.paper.report import (
    DaySummary,
    format_closed_position,
    format_open_position,
    format_summary,
)
from orb_intraday_bot.runtime.day import CandleReport, SignalOutcome


def _price(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}"


def format_opened(outcome: SignalOutcome) -> str:
    sig, plan, pos = outcome.signal, outcome.plan, outcome.position
    assert plan is not None and pos is not None and outcome.trade is not None
    flags = f"  FLAGS: {', '.join(plan.flags)}" if plan.flags else ""
    delta = "n/a" if plan.delta is None else f"{plan.delta:.2f}"
    close = (
        f" or a close back {'below' if pos.sign > 0 else 'above'} {pos.close_exit_level:.1f}"
        if pos.close_exit_level is not None
        else ""
    )
    return (
        f"OPENED {sig.setup.value} {sig.level_name} {sig.direction.value} "
        f"[{outcome.trade.zone.value}] futures {sig.entry_price:.1f} ({sig.entry_basis})\n"
        f"      buy {plan.contract.trading_symbol} ({plan.strike_label.upper()}, delta {delta}) "
        f"{pos.lots} lot(s) x {pos.lot_size} = {plan.quantity} @ {pos.entry_ltp:.2f}"
        f"  outlay Rs {plan.outlay_inr:,.0f}\n"
        f"      stop: futures {pos.hard_stop:.1f}{close}; option {plan.sl:.2f} "
        f"(sized for Rs {plan.sized_risk_inr:,.0f}, loss at stop Rs {plan.risk_at_stop_inr:,.0f})\n"
        f"      T1 futures {pos.t1:.1f} / option {plan.t1:.2f}"
        f" (book {pos.t1_book_lots} lot(s)), T2 futures {pos.t2:.1f} / option {plan.t2:.2f}"
        f"{flags}"
    )


def format_outcome(outcome: SignalOutcome) -> str:
    sig = outcome.signal
    head = f"{sig.setup.value} {sig.level_name} {sig.direction.value} @ {sig.entry_price:.1f}"
    if outcome.status == "opened":
        return format_opened(outcome)
    if outcome.status == "rejected":
        return f"BLOCKED {head} [{outcome.stage}] {outcome.detail}"
    if outcome.status == "stale":
        return f"MISSED  {head}: {outcome.detail}"
    return f"SKIPPED {head}: {outcome.detail}"


def format_exit(event: ExitEvent) -> str:
    return f"EXIT {event.time:%H:%M:%S} {event.message}"


def format_candle_report(report: CandleReport) -> str:
    c = report.candle
    end = c.start + timedelta(minutes=3)
    tag = "  [catch-up]" if report.catch_up else ""
    lines = [
        f"===== {c.start:%H:%M}-{end:%H:%M}  {report.symbol}  O {c.open:.1f}  H {c.high:.1f}  "
        f"L {c.low:.1f}  C {c.close:.1f}{tag} =====",
    ]
    zone = f"zone {report.zone.value}"
    if report.pivots is not None:
        p = report.pivots
        zone += f" (S1 {p.s1:.1f} | P {p.p:.1f} | R1 {p.r1:.1f}; S2 {p.s2:.1f} | R2 {p.r2:.1f})"
    lines.append(f"ORH {_price(report.orh)}  ORL {_price(report.orl)}  {zone}")
    for lv in report.level_sets:
        lines.append(f"  level set: {lv.kind} = {lv.value:.1f}")
    for ev in report.events:
        lines.append(f"  pattern: {ev.kind.value} {ev.level_name} {ev.direction.value} {ev.detail}")

    for outcome in report.outcomes:
        lines.append("  " + format_outcome(outcome))
    for exit_event in report.exits:
        lines.append("  " + format_exit(exit_event))
    if not report.outcomes and not report.exits:
        lines.append("  no signal")

    if report.marks:
        lines.append("  open positions:")
        lines += [f"    {format_open_position(p, c.close, price)}" for p, price in report.marks]
    else:
        lines.append("  open positions: none")
    if report.summary is not None:
        lines.append(f"  day: {format_summary(report.summary)}")
    return "\n".join(lines)


def format_day_end(summary: DaySummary, positions: list[Position]) -> str:
    lines = ["===== END OF DAY ====="]
    if not positions:
        lines.append("  no trades today")
    for p in positions:
        status = "" if not p.is_open else " (STILL OPEN)"
        lines.append(f"  {format_closed_position(p)}{status}")
    lines.append(f"  {format_summary(summary)}")
    return "\n".join(lines)
