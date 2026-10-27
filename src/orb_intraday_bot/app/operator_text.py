"""The text the operator commands reply with. Pure functions: no I/O except reading a log file.

Everything here reads state (the risk engine, the database, the settings) and returns a message.
The commands that act (/exit, /rearm, /restart, ...) live in `serve.handle_command`.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable, Sequence
from datetime import date, timedelta
from pathlib import Path

from orb_intraday_bot import __version__
from orb_intraday_bot.config.fingerprint import fingerprint
from orb_intraday_bot.config.settings import Settings, StrategyConfig
from orb_intraday_bot.indicators.pivots import SessionLevels
from orb_intraday_bot.paper.models import Position
from orb_intraday_bot.paper.report import summarize
from orb_intraday_bot.risk.engine import RiskEngine
from orb_intraday_bot.runtime.arming import (
    configured_mode_label,
    describe_order_mode,
    resolve_mode,
)
from orb_intraday_bot.runtime.limit_overrides import KNOBS, LimitControls, format_value
from orb_intraday_bot.storage.store import TradeStore

_MAX_ORDERS_SHOWN = 25
_LOG_TAIL_BYTES = 65_536
_LOG_LINE_MAX = 300
_LOG_TOTAL_MAX = 3500
_LOG_DEFAULT_LINES = 15
_LOG_MAX_LINES = 40
PNL_PERIODS = ("day", "week", "month")

_SECRET_PATTERNS = (
    (re.compile(r"bot\d{6,}:[A-Za-z0-9_-]{20,}"), "bot<redacted>"),
    (re.compile(r"(?i)\b(token|secret|password|api[_-]?key)(\s*[=:]\s*)\S+"), r"\1\2<redacted>"),
    (re.compile(r"(?i)\bBearer\s+\S+"), "Bearer <redacted>"),
)


def rs(value: float, *, sign: bool = False) -> str:
    return f"Rs {value:+,.0f}" if sign else f"Rs {value:,.0f}"


def redact(text: str) -> str:
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


# ---- status, mode, version, risk ---------------------------------------------------------
def risk_line(risk: RiskEngine) -> str:
    s, limits = risk.state, risk.limits
    parts = [
        f"trades {s.trades_today}/{limits.max_trades_per_day}",
        f"open {s.open_positions}/{limits.max_open_positions}",
        f"P&L {rs(s.realized_pnl_inr, sign=True)}",
    ]
    if s.halted:
        parts.append(f"HALTED ({s.halt_reason})")
    if s.entries_blocked:
        parts.append(f"ENTRIES BLOCKED ({s.block_reason})")
    return "Risk: " + ", ".join(parts)


def status_extra(
    settings: Settings,
    risk: RiskEngine,
    *,
    stopped_today: bool,
    session_status: str | None,
) -> str:
    """The lines /status adds to the heartbeat report."""
    decision = resolve_mode(
        settings, trading_credentials_present=settings.has_trading_credentials()
    )
    lines = [
        f"Orders today: {describe_order_mode(decision.order_mode)}",
        "Trading today: STOPPED by /stop (send /start to allow it)"
        if stopped_today
        else "Trading today: on",
        f"Session: {session_status}" if session_status else "Session: not running",
        risk_line(risk),
    ]
    return "\n".join(lines)


def mode_text(
    settings: Settings,
    risk: RiskEngine,
    *,
    stopped_today: bool,
    executor_mode: str | None,
    dry_run_source: str = "",
) -> str:
    creds = settings.has_trading_credentials()
    decision = resolve_mode(settings, trading_credentials_present=creds)
    limits = risk.limits
    lines = [
        f"Configured: {configured_mode_label(settings)}",
        f"LIVE_MODE={settings.live_mode}  LIVE_ARMED={str(settings.live_armed).lower()}  "
        f"DRY_RUN={str(settings.dry_run).lower()}  trading credentials: "
        f"{'yes' if creds else 'no'}"
        + (f"\nDRY_RUN is {dry_run_source} (change it with /dryrun)" if dry_run_source else ""),
        f"Resolved: {describe_order_mode(decision.order_mode)} - {decision.reason}",
        f"Session executor: {describe_order_mode(executor_mode)}"
        if executor_mode
        else "Session executor: none (paper, or no session running)",
        "Trading today: STOPPED by /stop" if stopped_today else "Trading today: on",
        f"Orders: product {limits.product}, max {limits.max_lots_per_trade} lot(s) per trade, "
        f"square-off {limits.square_off_time}",
    ]
    return "\n".join(lines)


def version_text(cfg: StrategyConfig) -> str:
    fp = fingerprint(cfg)
    return (
        f"orb-intraday-bot v{__version__}\n"
        f"rules {fp.strategy}, assumptions {fp.assumptions}, runtime {fp.runtime}\n"
        f"python {sys.version.split()[0]}"
    )


def risk_text(risk: RiskEngine, *, stopped_today: bool) -> str:
    s, limits = risk.state, risk.limits
    loss_limit = limits.daily_loss_limit_inr(risk.capital_inr)
    lines = [
        f"Risk for {s.day or 'today'}",
        f"Kill switch: HALTED - {s.halt_reason} (new entries stopped, positions flattened; "
        "/clearhalt to clear)"
        if s.halted
        else "Kill switch: ok",
        f"Entries blocked: YES - {s.block_reason} (positions left alone; check the broker, "
        "then /clearhalt)"
        if s.entries_blocked
        else "Entries blocked: no",
        "Stopped by /stop today: yes" if stopped_today else "Stopped by /stop today: no",
        f"Trades today: {s.trades_today} of {limits.max_trades_per_day}",
        f"Open positions: {s.open_positions} of {limits.max_open_positions}",
        f"Realized P&L: {rs(s.realized_pnl_inr, sign=True)} (daily loss limit "
        f"{rs(loss_limit)} = {limits.daily_loss_limit_pct:.1%} of {rs(risk.capital_inr)})",
        f"Loss streak: {s.loss_streak} (halts at {limits.loss_streak_halt})",
        f"Per trade: max {limits.max_lots_per_trade} lot(s), option spread at most "
        f"{limits.max_option_spread_pct:.1%}, data at most {limits.max_data_staleness_sec}s old",
        f"Orders: {limits.product}, entry limit +{limits.entry_price_protection_pct:.1%} "
        f"for {limits.entry_fill_timeout_sec:.0f}s then cancel, square-off "
        f"{limits.square_off_time}",
    ]
    return "\n".join(lines)


# ---- limits ---------------------------------------------------------------------------------
def limits_text(controls: LimitControls) -> str:
    """The four adjustable limits, what each means in rupees and where each value comes from."""
    rows = []
    for key, knob in KNOBS.items():
        value = controls.get(key)
        extra = {
            "risk_pct": f" = {rs(controls.risk_budget_inr())} per trade",
            "daily_loss_pct": f" = {rs(controls.daily_loss_limit_inr())} a day",
        }.get(key, "")
        source = (
            "set by Telegram"
            if controls.is_set_by_operator(key)
            else "default"
        )
        rows.append(
            f"{knob.label}: {format_value(knob, value)}{extra} "
            f"({source}, change with {knob.command})"
        )
    return (
        "Limits in force (a change applies from the next entry)\n"
        + "\n".join(rows)
        + "\nDefault = the config files and GitHub variables. /limits reset puts every value back."
    )


# ---- funds -------------------------------------------------------------------------------
def funds_text(
    *,
    capital_inr: float,
    daily_loss_limit_inr: float,
    mode_label: str,
    margin: float | None,
    margin_note: str,
    held: dict[str, int] | None,
    held_label: str,
) -> str:
    lines = [
        f"Mode: {mode_label}",
        f"Capital the bot sizes against: {rs(capital_inr)} "
        f"(daily loss limit {rs(daily_loss_limit_inr)})",
        f"Available option margin: {rs(margin)}" if margin is not None else margin_note,
    ]
    if held is not None:
        open_ = {k: v for k, v in held.items() if v}
        lines.append(
            f"{held_label}: " + (", ".join(f"{k} {v}" for k, v in sorted(open_.items())) or "none")
        )
    return "\n".join(lines)


# ---- today's trading ---------------------------------------------------------------------
def progress_text(positions: Sequence[Position]) -> str:
    if not positions:
        return "No open positions."
    out = [f"{len(positions)} open:"]
    for p in positions:
        slip = ""
        if p.planned_entry > 0:
            slip = f" (planned {p.planned_entry:.2f})"
        stop_note = " (moved to entry after T1)" if p.stop_at_entry else ""
        t1 = "done" if p.t1_done else "pending"
        t2_note = (
            f" (trigger {p.t2_hit_level:.1f})"
            if abs(p.t2_hit_level - p.t2) >= 1e-6
            else ""
        )
        out.append(
            f"- {p.direction.value} {p.setup} {p.trading_symbol} [{p.order_mode}] "
            f"{p.remaining_lots}/{p.lots} lot(s)\n"
            f"  entry premium {p.entry_ltp:.2f}{slip}, futures entry {p.futures_entry:.1f}\n"
            f"  futures stop {p.active_stop:.1f}{stop_note}, T1 {p.t1:.1f} ({t1}), "
            f"T2 {p.t2:.1f}{t2_note}\n"
            f"  broker net {p.active_option_stop:.2f}, T1 {p.t1_option:.2f}, "
            f"T2 {p.t2_option:.2f}"
        )
        if p.order_mode != "paper":
            if p.stop_order_id:
                out.append(
                    f"  broker stop: order {p.stop_order_id}, trigger {p.stop_trigger:.2f}, "
                    f"qty {p.stop_quantity}"
                )
            else:
                out.append("  broker stop: NONE (try /rearm)")
            if p.unbooked_qty:
                out.append(f"  sold but not yet booked: {p.unbooked_qty} unit(s)")
    return "\n".join(out)


def orders_text(store: TradeStore, day: date) -> str:
    rows = store.orders_for_day(day)
    positions = [p for p in store.positions_for_day(day) if p.order_mode != "paper"]
    if not rows and not positions:
        return f"No orders on {day}."
    out = [f"Orders on {day}: {len(rows)}"]
    shown = rows[-_MAX_ORDERS_SHOWN:]
    if len(rows) > len(shown):
        out.append(f"(last {len(shown)} shown)")
    for r in shown:
        price = f"@{r['price']:.2f}" if r["price"] else "at market"
        trigger = f" trg {r['trigger_price']:.2f}" if r["trigger_price"] else ""
        filled = (
            f" filled {r['filled_qty']}@{r['avg_price']:.2f}" if r["filled_qty"] else ""
        )
        reason = f" ({r['reject_reason']})" if r["reject_reason"] else ""
        out.append(
            f"{str(r['created_at'])[11:19]} {r['leg']} {r['side']} {r['quantity']} "
            f"{r['trading_symbol']} {r['order_type']} {price}{trigger} -> {r['status']}"
            f"{filled}{reason} [{r['mode']}]"
        )
    for p in positions:
        if p.planned_entry > 0 and p.entry_ltp > 0:
            diff = p.entry_ltp - p.planned_entry
            out.append(
                f"Entry vs plan {p.trading_symbol}: planned {p.planned_entry:.2f}, filled "
                f"{p.entry_ltp:.2f} ({diff / p.planned_entry:+.1%})"
            )
    return "\n".join(out)


def signals_text(store: TradeStore, day: date) -> str:
    rows = store.signals_for_day(day)
    if not rows:
        return f"No signals recorded on {day}."
    counts: dict[str, int] = {}
    for r in rows:
        counts[str(r["status"])] = counts.get(str(r["status"]), 0) + 1
    out = [
        f"Signals on {day}: {len(rows)} ("
        + ", ".join(f"{n} {s}" for s, n in sorted(counts.items()))
        + ")"
    ]
    for r in rows:
        detail = ""
        if r["stage"] or r["reason"]:
            detail = f" [{r['stage']}] {r['reason']}".rstrip()
        out.append(
            f"{str(r['candle_start'])[11:16]} {r['setup']} {r['level_name']} "
            f"{r['direction']} @{r['entry_price']:.1f}: {r['status']}{detail}"
        )
    return "\n".join(out)


def levels_text(
    symbol: str | None,
    levels: SessionLevels | None,
    orh: float | None,
    orl: float | None,
    last_close: float | None,
) -> str:
    if levels is None:
        return (
            "Today's levels are not loaded yet. The session loads them when it starts "
            "(about 09:00 IST)."
        )
    pivots = levels.pivots()
    out = [
        f"{symbol or 'Futures'}: pivots from {levels.day} ({levels.describe()})",
        "  ".join(f"{name} {value:.1f}" for name, value in reversed(pivots.ordered())),
    ]
    if orh is not None and orl is not None:
        out.append(f"Opening range: high {orh:.1f}, low {orl:.1f} (width {orh - orl:.1f})")
    else:
        out.append("Opening range: not complete yet")
    if last_close is not None:
        out.append(f"Last candle close: {last_close:.1f}")
    return "\n".join(out)


def parse_pnl_period(arg: str) -> str | None:
    word = arg.strip().lower() or "day"
    aliases = {"today": "day", "d": "day", "w": "week", "m": "month"}
    word = aliases.get(word, word)
    return word if word in PNL_PERIODS else None


def pnl_text(store: TradeStore, period: str, today: date) -> str:
    if period == "day":
        first = today
    elif period == "week":
        first = today - timedelta(days=6)
    else:
        first = today.replace(day=1)
    days = [d for d in store.trade_days() if first <= d <= today]
    positions: list[Position] = []
    lines: list[str] = []
    for day in days:
        day_positions = store.positions_for_day(day)
        if not day_positions:
            continue
        positions += day_positions
        s = summarize(day, day_positions)
        lines.append(
            f"{day}: {s.trades} trade(s), {s.wins}W/{s.losses}L, "
            f"net {rs(s.net_option_pnl, sign=True)}"
            + (f", {s.open_trades} open" if s.open_trades else "")
        )
    title = {"day": f"P&L for {today}", "week": f"P&L {first} to {today}"}.get(
        period, f"P&L {first} to {today} (month)"
    )
    if not positions:
        return f"{title}\nNo trades."
    total = summarize(today, positions)
    modes: dict[str, int] = {}
    for p in positions:
        modes[p.order_mode] = modes.get(p.order_mode, 0) + 1
    rate = "n/a" if total.win_rate is None else f"{total.win_rate:.0%}"
    out = [title, *lines] if period != "day" else [title]
    out.append(
        f"Total: {total.trades} trade(s), {total.wins} win / {total.losses} loss ({rate}), "
        f"gross {rs(total.gross_option_pnl, sign=True)} - charges {rs(total.charges)} = "
        f"net {rs(total.net_option_pnl, sign=True)}, futures {total.futures_points:+.1f} pts"
    )
    out.append("Modes: " + ", ".join(f"{m} x{n}" for m, n in sorted(modes.items())))
    return "\n".join(out)


# ---- logs and matching -------------------------------------------------------------------
def parse_log_lines(arg: str) -> int:
    word = arg.strip()
    if not word.isdigit():
        return _LOG_DEFAULT_LINES
    return max(1, min(int(word), _LOG_MAX_LINES))


def log_tail(data_dir: Path, day: date, lines: int) -> str:
    path = data_dir / "logs" / f"orb-bot-{day.isoformat()}.log"
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            start = max(0, size - _LOG_TAIL_BYTES)
            fh.seek(start)
            raw = fh.read()
    except OSError:
        return f"No log for {day} yet."
    text = raw.decode("utf-8", errors="replace").splitlines()
    if start > 0 and text:
        text = text[1:]                      # the first line may be cut in half
    tail = [redact(line)[:_LOG_LINE_MAX] for line in text[-lines:] if line.strip()]
    if not tail:
        return f"The log for {day} is empty."
    body = "\n".join(tail)
    if len(body) > _LOG_TOTAL_MAX:
        body = "...\n" + body[-_LOG_TOTAL_MAX:]
    return f"Last {len(tail)} log line(s) of {day}:\n{body}"


def match_positions(positions: Iterable[Position], text: str) -> list[Position]:
    """Open positions the operator means by `text`: an exact trading symbol, else a part of one
    (for example 24500CE). Case does not matter."""
    needle = text.strip().upper()
    if not needle:
        return []
    items = list(positions)
    exact = [p for p in items if p.trading_symbol.upper() == needle]
    if exact:
        return exact
    return [p for p in items if needle in p.trading_symbol.upper()]

