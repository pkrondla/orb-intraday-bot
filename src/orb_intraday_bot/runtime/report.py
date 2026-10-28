"""Text for the per-candle report and the trade alerts (spec section 8)."""

from __future__ import annotations

import re
from datetime import timedelta
from html import escape

from orb_intraday_bot.core.clock import IST
from orb_intraday_bot.engine.signal_engine import PlannedTrade
from orb_intraday_bot.paper.exits import ExitEvent
from orb_intraday_bot.paper.models import ExitReason, Position
from orb_intraday_bot.paper.report import (
    DaySummary,
    format_closed_position,
    format_open_position,
    format_summary,
)
from orb_intraday_bot.runtime.day import CandleReport, SignalOutcome

_NAMED_LEVEL = re.compile(r"\b(ORH|ORL|R[1-5]|S[1-5]|P)\b")
_CAPPED_LEVEL = re.compile(r"capped at (ORH|ORL|R[1-5]|S[1-5]|P)\b")
_HARD_STOP_PTS = re.compile(r"(?:hard stop |standard )(\d+(?:\.\d+)?)")


def _price(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}"


def _opened_stamp(pos: Position) -> str:
    return pos.opened_at.astimezone(IST).strftime("%H:%M IST")


def _rr(entry: float, stop: float, target: float) -> str:
    risk = abs(entry - stop)
    if risk < 1e-6:
        return "n/a"
    return f"1 : {abs(target - entry) / risk:.2f}"


def _rr_line(entry: float, sl: float, t1: float, t2: float | None = None) -> str:
    line = f"R:R  {_rr(entry, sl, t1)} (T1)"
    if t2 is not None and abs(t2 - t1) > 1e-6:
        line += f"   {_rr(entry, sl, t2)} (T2)"
    return line


def _stop_risk_inr(pos: Position) -> float:
    """Rupees at the broker net from the real fill (may exceed the sizing budget)."""
    return round((pos.entry_ltp - pos.sl_exit) * pos.lots * pos.lot_size, 2)


def _named_level(text: str) -> str | None:
    """ORH / P / S2 etc. Prefer an explicit cap, then a name at the start of the basis."""
    capped = _CAPPED_LEVEL.search(text)
    if capped:
        return capped.group(1)
    start = _NAMED_LEVEL.match(text.strip())
    if start:
        return start.group(1)
    found = _NAMED_LEVEL.findall(text)
    return found[-1] if found else None


def _target_trigger_note(level: float, trigger: float, *, plain: bool = False) -> str:
    """Shown when a target fires in front of the chart level (proximity band)."""
    if abs(trigger - level) < 1e-6:
        return ""
    if plain:
        return f" (trigger {trigger:.1f})"
    return f"\n      trigger <code>{trigger:.1f}</code>"


def _t1_trigger_note(pos: Position, *, plain: bool = False) -> str:
    """Shown when T1 fires in front of the chart level (proximity band)."""
    return _target_trigger_note(pos.t1, pos.t1_hit_level, plain=plain)


def sl_level_name(trade: PlannedTrade) -> str:
    """Short stop label: ORH, ORL, 35 pts, 2HH, ..."""
    basis = trade.stop.basis
    if "close through" in basis or "hard stop" in basis or basis.startswith("standard "):
        found = _HARD_STOP_PTS.search(basis)
        pts = found.group(1) if found else "35"
        if pts.endswith(".0"):
            pts = pts[:-2]
        return f"{pts} pts"
    if "floor" in basis:
        return "10 pts"
    return _named_level(basis) or trade.signal.level_name or "level"


def target_level_name(basis: str) -> str:
    """Short target label: P, S2, halfway, ..."""
    if "halfway" in basis:
        return "halfway"
    named = _named_level(basis)
    if named:
        return named
    if "projection" in basis or "OR-range" in basis:
        return "OR proj"
    if "consolidation" in basis or "opposite end" in basis:
        return "range"
    return basis


def trend_label(trend: str) -> str:
    """Notes 3.9 trending-day state for the trade-signal line."""
    if not trend or trend == "none":
        return "no trend"
    return trend


def signal_context(zone: str, trend: str) -> str:
    return f"{zone} · {trend_label(trend)}"


def format_opened(outcome: SignalOutcome, *, trend: str = "none") -> str:
    sig, plan, pos = outcome.signal, outcome.plan, outcome.position
    assert plan is not None and pos is not None and outcome.trade is not None
    flags = f"  FLAGS: {', '.join(plan.flags)}" if plan.flags else ""
    delta = "n/a" if plan.delta is None else f"{plan.delta:.2f}"
    close = (
        f" or a close back {'below' if pos.sign > 0 else 'above'} {pos.close_exit_level:.1f}"
        if pos.close_exit_level is not None
        else ""
    )
    sl_name = sl_level_name(outcome.trade)
    t1_name = target_level_name(outcome.trade.targets.t1_basis)
    t2_name = target_level_name(outcome.trade.targets.t2_basis)
    t2_line = ""
    if abs(pos.t1 - pos.t2) > pos.tick_size:
        t2_trig = _target_trigger_note(pos.t2, pos.t2_hit_level, plain=True)
        t2_line = (
            f"      T2 {t2_name} futures {pos.t2:.1f} / option {pos.t2_option:.2f}"
            f"{t2_trig} (1 lot runner)\n"
        )
    return (
        f"OPENED {_opened_stamp(pos)} {sig.setup.value} {sig.level_name} {sig.direction.value} "
        f"[{signal_context(outcome.trade.zone.value, trend)}] futures {sig.entry_price:.1f} "
        f"({sig.entry_basis})\n"
        f"      BUY {plan.contract.trading_symbol} ({plan.strike_label.upper()}, delta {delta}) "
        f"{pos.lots} lot(s) x {pos.lot_size} = {plan.quantity} @ {pos.entry_ltp:.2f}"
        f"  outlay Rs {plan.outlay_inr:,.0f}\n"
        f"      SL {sl_name} futures {pos.hard_stop:.1f}{close}; broker net option "
        f"{pos.sl_exit:.2f} (sized for Rs {plan.sized_risk_inr:,.0f}, loss at net "
        f"Rs {_stop_risk_inr(pos):,.0f})\n"
        f"      T1 {t1_name} futures {pos.t1:.1f} / option {pos.t1_option:.2f}"
        f"{_t1_trigger_note(pos, plain=True)}\n"
        f"{t2_line}"
        f"      {_rr_line(pos.entry_ltp, pos.sl_exit, pos.t1_option, pos.t2_option)}"
        f"{flags}"
    )


def format_opened_alert(outcome: SignalOutcome, *, trend: str = "none") -> str:
    """Telegram card: time, BUY / SL / targets, and R:R. HTML; send with parse_mode."""
    sig, plan, pos = outcome.signal, outcome.plan, outcome.position
    assert plan is not None and pos is not None and outcome.trade is not None
    delta = "n/a" if plan.delta is None else f"{plan.delta:.2f}"
    symbol = escape(plan.contract.trading_symbol)
    setup = escape(sig.setup.value)
    level = escape(sig.level_name)
    direction = escape(sig.direction.value)
    context = escape(signal_context(outcome.trade.zone.value, trend))
    basis = escape(sig.entry_basis)
    strike = escape(plan.strike_label.upper())
    close = (
        f"\nor close back {'below' if pos.sign > 0 else 'above'} "
        f"<code>{pos.close_exit_level:.1f}</code>"
        if pos.close_exit_level is not None
        else ""
    )
    flags = ""
    if plan.flags:
        flags = "\nFLAGS  " + escape(", ".join(plan.flags))
    sl_name = escape(sl_level_name(outcome.trade))
    t1_name = escape(target_level_name(outcome.trade.targets.t1_basis))
    t2_name = escape(target_level_name(outcome.trade.targets.t2_basis))
    t2_block = ""
    if abs(pos.t1 - pos.t2) > pos.tick_size:
        t2_block = (
            f"\n<b>T2</b>   {t2_name}  (1 lot runner)\n"
            f"      option <code>{pos.t2_option:.2f}</code>   "
            f"fut <code>{pos.t2:.1f}</code>"
            f"{_target_trigger_note(pos.t2, pos.t2_hit_level)}\n"
        )
    return (
        f"🟢 <b>OPENED</b>  <code>{_opened_stamp(pos)}</code>\n"
        f"{setup} · {level} · {direction} · {context}\n"
        f"<i>{basis}</i>\n"
        f"\n<b>BUY</b>  <code>{symbol}</code>\n"
        f"      {strike}  δ {delta} · {pos.lots} lot(s) × {pos.lot_size}\n"
        f"      option <code>{pos.entry_ltp:.2f}</code>   "
        f"fut <code>{pos.futures_entry:.1f}</code>   Rs {plan.outlay_inr:,.0f}\n"
        f"\n<b>SL</b>   {sl_name}\n"
        f"      fut <code>{pos.hard_stop:.1f}</code>"
        f"{close}\n"
        f"      broker net option <code>{pos.sl_exit:.2f}</code>\n"
        f"      risk Rs {_stop_risk_inr(pos):,.0f}\n"
        f"\n<b>T1</b>   {t1_name}\n"
        f"      option <code>{pos.t1_option:.2f}</code>   "
        f"fut <code>{pos.t1:.1f}</code>"
        f"{_t1_trigger_note(pos)}\n"
        f"{t2_block}"
        f"\n<b>{escape(_rr_line(pos.entry_ltp, pos.sl_exit, pos.t1_option, pos.t2_option))}</b>"
        f"{flags}"
    )


def format_outcome(
    outcome: SignalOutcome, *, zone: str | None = None, trend: str = "none"
) -> str:
    sig = outcome.signal
    head = f"{sig.setup.value} {sig.level_name} {sig.direction.value} @ {sig.entry_price:.1f}"
    context = f" [{signal_context(zone, trend)}]" if zone else ""
    if outcome.status == "opened":
        return format_opened(outcome, trend=trend)
    if outcome.status == "rejected":
        return f"BLOCKED {head}{context} [{outcome.stage}] {outcome.detail}"
    if outcome.status == "stale":
        return f"MISSED  {head}: {outcome.detail}"
    return f"SKIPPED {head}: {outcome.detail}"


def format_exit(event: ExitEvent) -> str:
    return f"EXIT {event.time:%H:%M:%S} {event.message}"


_SL_REASONS = frozenset({
    ExitReason.HARD_STOP,
    ExitReason.BREAKEVEN_STOP,
    ExitReason.CLOSE_STOP,
    ExitReason.OPTION_STOP,
    ExitReason.BROKER_STOP,
})
_T1_MOVED = re.compile(
    r"T1 reached: stop moved to entry \(futures ([0-9.]+), option ([0-9.]+)\)"
)


def _exit_stamp(event: ExitEvent) -> str:
    return event.time.astimezone(IST).strftime("%H:%M:%S IST")


def _exit_heading(reason: ExitReason | None, message: str) -> tuple[str, str]:
    if reason is ExitReason.T1 or (reason is None and "T1 reached" in message):
        return "🟡", "T1"
    if reason is ExitReason.T2:
        return "🟢", "T2"
    if reason in _SL_REASONS:
        return "🔴", "SL"
    if reason is not None:
        return "⚪", reason.value
    return "⚪", "EXIT"


def format_exit_alert(event: ExitEvent) -> str:
    """Telegram card: SL / T1 (or other exit) with the futures fill. HTML."""
    fill = event.fill
    reason = fill.reason if fill is not None else None
    emoji, heading = _exit_heading(reason, event.message)
    symbol = escape(event.message.split()[0]) if event.message else ""
    lines = [f"{emoji} <b>{escape(heading)}</b>  <code>{_exit_stamp(event)}</code>"]
    if fill is not None and heading != fill.reason.value:
        lines.append(escape(fill.reason.value))
    if symbol:
        lines.append(f"<code>{symbol}</code>")
    if fill is not None:
        lines.append(
            f"      option <code>{fill.option_price:.2f}</code>   "
            f"fut <code>{fill.futures_price:.1f}</code>"
        )
        lines.append(f"      sold {fill.lots} lot(s)   {fill.futures_points:+.1f} pts")
        lines.append(f"      option Rs {fill.option_gross:+,.0f}")
        return "\n".join(lines)
    moved = _T1_MOVED.search(event.message)
    if moved:
        lines.append("      stop moved to entry")
        lines.append(
            f"      option <code>{float(moved.group(2)):.2f}</code>   "
            f"fut <code>{float(moved.group(1)):.1f}</code>"
        )
        return "\n".join(lines)
    rest = event.message.split(" ", 1)[1] if " " in event.message else event.message
    lines.append(escape(rest))
    return "\n".join(lines)


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
        lines.append(
            "  " + format_outcome(outcome, zone=report.zone.value, trend=report.trend)
        )
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
