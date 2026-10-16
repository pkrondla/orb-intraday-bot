"""Compare what the live paper run did on a day with a replay of the same day's final candles.

A live run sees candles as they are published and polls prices between them; a replay sees the
finished day. They should agree on which signals fired and be close on exits. A difference is
information: a missed candle, a data revision, a stale signal, an option-chain failure, or a bug.
Replay option prices are synthetic, so only futures points are compared.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from orb_intraday_bot.analysis.backtest import DayResult, SignalRecord
from orb_intraday_bot.paper.models import Position

POINTS_TOLERANCE = 5.0          # futures points per unit before an exit counts as different


@dataclass(frozen=True, slots=True)
class Match:
    signal: SignalRecord
    live_points: float | None
    replay_points: float | None

    @property
    def differs(self) -> bool:
        if self.live_points is None or self.replay_points is None:
            return False
        return abs(self.live_points - self.replay_points) > POINTS_TOLERANCE


@dataclass(slots=True)
class Reconciliation:
    day: date
    matched: list[Match] = field(default_factory=list)
    only_replay: list[tuple[SignalRecord, str]] = field(default_factory=list)   # (signal, live)
    only_live: list[tuple[SignalRecord, str]] = field(default_factory=list)     # (signal, replay)

    @property
    def clean(self) -> bool:
        return not self.only_replay and not self.only_live and not any(
            m.differs for m in self.matched
        )


def _label(rows: dict[str, SignalRecord], signal_id: str) -> str:
    row = rows.get(signal_id)
    if row is None:
        return "no such signal"
    detail = f"{row.stage}: {row.reason}" if row.reason else row.stage
    return f"{row.status} ({detail})" if detail else row.status


def reconcile(
    day: date,
    live_signals: list[SignalRecord],
    live_positions: list[Position],
    replay: DayResult,
) -> Reconciliation:
    live_by_id = {s.signal_id: s for s in live_signals}
    replay_by_id = {s.signal_id: s for s in replay.signals}
    live_taken = {s.signal_id for s in live_signals if s.status == "accepted"}
    replay_taken = {s.signal_id for s in replay.signals if s.status == "accepted"}
    live_pos = {p.position_id: p for p in live_positions}
    replay_pos = {p.position_id: p for p in replay.positions}

    out = Reconciliation(day)
    for signal_id in sorted(live_taken & replay_taken, key=lambda i: replay_by_id[i].time):
        lp, rp = live_pos.get(signal_id), replay_pos.get(signal_id)
        out.matched.append(
            Match(
                replay_by_id[signal_id],
                None if lp is None or lp.is_open else lp.futures_points,
                None if rp is None or rp.is_open else rp.futures_points,
            )
        )
    for signal_id in sorted(replay_taken - live_taken, key=lambda i: replay_by_id[i].time):
        out.only_replay.append((replay_by_id[signal_id], _label(live_by_id, signal_id)))
    for signal_id in sorted(live_taken - replay_taken, key=lambda i: live_by_id[i].time):
        out.only_live.append((live_by_id[signal_id], _label(replay_by_id, signal_id)))
    return out


def _name(s: SignalRecord) -> str:
    return f"{s.time} {s.setup} {s.level} {s.direction}"


def format_reconciliation(rec: Reconciliation) -> str:
    lines = [
        f"{rec.day}: {len(rec.matched)} trade(s) in both, {len(rec.only_replay)} only in the "
        f"replay, {len(rec.only_live)} only live"
    ]
    for m in rec.matched:
        live = "open" if m.live_points is None else f"{m.live_points:+.1f}"
        replay = "open" if m.replay_points is None else f"{m.replay_points:+.1f}"
        flag = "  <-- exits differ" if m.differs else ""
        lines.append(f"  both   {_name(m.signal)}: live {live} pts, replay {replay} pts{flag}")
    for signal, live in rec.only_replay:
        lines.append(f"  REPLAY ONLY {_name(signal)}: live run had it as {live}")
    for signal, replay in rec.only_live:
        lines.append(f"  LIVE ONLY   {_name(signal)}: replay had it as {replay}")
    verdict = "the live run and the replay agree" if rec.clean else "differences found"
    lines.append(f"  => {verdict}")
    return "\n".join(lines)
