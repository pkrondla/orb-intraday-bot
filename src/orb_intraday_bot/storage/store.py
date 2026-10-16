"""SQLite storage for signals, paper positions and fills.

Idempotent by design: a signal id is a primary key, so replaying a candle (a restart, a catch-up)
never records the same signal or opens the same trade twice. Positions are saved as JSON state
after every change, so open trades come back after a restart.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

from orb_intraday_bot.core.clock import IST
from orb_intraday_bot.core.signals import SetupSignal
from orb_intraday_bot.execution.models import OrderIntent, OrderLeg, OrderRecord, OrderStatus
from orb_intraday_bot.paper.models import Fill, Position
from orb_intraday_bot.paper.report import DaySummary, summarize
from orb_intraday_bot.storage.migrations import apply_migrations


class TradeStore:
    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        self._conn.row_factory = sqlite3.Row
        if self._path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        apply_migrations(self._conn)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._conn:      # commits on success, rolls back on error
            yield self._conn

    # -- signals -------------------------------------------------------------------------
    def record_signal(
        self, signal: SetupSignal, status: str, stage: str = "", reason: str = ""
    ) -> bool:
        """Insert a signal once. Returns False if it was already recorded (a replay)."""
        with self._tx() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO signals (signal_id, trade_date, candle_start, setup,"
                " direction, level_name, level_value, entry_price, sign_id, status, stage,"
                " reason, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    signal.signal_id,
                    signal.candle_start.date().isoformat(),
                    signal.candle_start.isoformat(),
                    signal.setup.value,
                    signal.direction.value,
                    signal.level_name,
                    signal.level_value,
                    signal.entry_price,
                    signal.sign_id,
                    status,
                    stage,
                    reason,
                    signal.note,
                ),
            )
            return cur.rowcount == 1

    def set_signal_status(
        self, signal_id: str, status: str, stage: str = "", reason: str = ""
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE signals SET status=?, stage=?, reason=? WHERE signal_id=?",
                (status, stage, reason, signal_id),
            )

    def signal_status(self, signal_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT status FROM signals WHERE signal_id=?", (signal_id,)
        ).fetchone()
        return None if row is None else str(row["status"])

    def signals_for_day(self, day: date) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM signals WHERE trade_date=? ORDER BY candle_start, signal_id",
                (day.isoformat(),),
            )
        )

    # -- positions and fills -------------------------------------------------------------
    def has_position(self, position_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM positions WHERE position_id=?", (position_id,)
        ).fetchone()
        return row is not None

    def save_position(self, position: Position, now: datetime) -> None:
        """Upsert the position state and any fills not yet in the fills table."""
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO positions (position_id, trade_date, status, state, updated_at)"
                " VALUES (?,?,?,?,?) ON CONFLICT(position_id) DO UPDATE SET"
                " status=excluded.status, state=excluded.state, updated_at=excluded.updated_at",
                (
                    position.position_id,
                    position.opened_at.date().isoformat(),
                    position.status.value,
                    json.dumps(position.to_dict()),
                    now.isoformat(),
                ),
            )
            for seq, fill in enumerate(position.fills):
                _insert_fill(conn, position.position_id, seq, fill)

    def load_position(self, position_id: str) -> Position | None:
        row = self._conn.execute(
            "SELECT state FROM positions WHERE position_id=?", (position_id,)
        ).fetchone()
        return None if row is None else _load(row["state"])

    def open_positions(self) -> list[Position]:
        rows = self._conn.execute(
            "SELECT state FROM positions WHERE status='OPEN' ORDER BY trade_date, position_id"
        )
        return [_load(r["state"]) for r in rows]

    def positions_for_day(self, day: date) -> list[Position]:
        rows = self._conn.execute(
            "SELECT state FROM positions WHERE trade_date=? ORDER BY position_id",
            (day.isoformat(),),
        )
        return sorted((_load(r["state"]) for r in rows), key=lambda p: p.opened_at)

    def trade_days(self) -> list[date]:
        """Every day with a recorded signal or position, oldest first."""
        rows = self._conn.execute(
            "SELECT trade_date FROM signals UNION SELECT trade_date FROM positions "
            "ORDER BY trade_date"
        )
        return [date.fromisoformat(r["trade_date"]) for r in rows]

    def fills_for_position(self, position_id: str) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM fills WHERE position_id=? ORDER BY seq", (position_id,)
            )
        )

    # -- reporting -----------------------------------------------------------------------
    def day_summary(self, day: date) -> DaySummary:
        return summarize(day, self.positions_for_day(day))

    # -- orders and event log ------------------------------------------------------------
    def save_order(self, record: OrderRecord) -> None:
        intent = record.intent
        created = (record.created_at or datetime.now(IST)).isoformat()
        updated = (record.updated_at or datetime.now(IST)).isoformat()
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO orders (idempotency_key, signal_id, leg, trading_symbol, side,"
                " quantity, order_type, price, trigger_price, product, status, broker_order_id,"
                " filled_qty, avg_price, reject_reason, reference_id, created_at, updated_at,"
                " mode)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(idempotency_key) DO UPDATE SET"
                " status=excluded.status, broker_order_id=excluded.broker_order_id,"
                " filled_qty=excluded.filled_qty, avg_price=excluded.avg_price,"
                " reject_reason=excluded.reject_reason, updated_at=excluded.updated_at,"
                " mode=excluded.mode",
                (
                    intent.idempotency_key, intent.signal_id, intent.leg.value,
                    intent.trading_symbol, intent.side, intent.quantity, intent.order_type,
                    intent.price, intent.trigger_price, intent.product, record.status.value,
                    record.broker_order_id, record.filled_qty, record.avg_price,
                    record.reject_reason, intent.reference_id, created, updated, record.mode,
                ),
            )
            for seq, fill in enumerate(record.fills):
                conn.execute(
                    "INSERT OR IGNORE INTO order_fills (idempotency_key, seq, fill_time,"
                    " quantity, price, broker_order_id) VALUES (?,?,?,?,?,?)",
                    (
                        intent.idempotency_key, seq, fill.time.isoformat(),
                        fill.quantity, fill.price, fill.broker_order_id,
                    ),
                )

    def append_event(
        self,
        event_type: str,
        payload: dict[str, object],
        *,
        signal_id: str = "",
        at: datetime | None = None,
        mode: str = "",
    ) -> None:
        when = (at or datetime.now(IST)).isoformat()
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO events (event_time, event_type, signal_id, payload, mode)"
                " VALUES (?,?,?,?,?)",
                (when, event_type, signal_id, json.dumps(payload), mode),
            )

    def orders_for_signal(self, signal_id: str) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM orders WHERE signal_id=? ORDER BY created_at", (signal_id,)
            )
        )

    def orphan_entries(self, day: date, mode: str) -> list[str]:
        """Signals whose entry order filled but that have no position on record.

        That only happens if the process died between the fill and saving the position. The
        shares are held at the broker with nothing tracking them, so it must be looked at.
        """
        rows = self._conn.execute(
            "SELECT o.signal_id FROM orders o"
            " LEFT JOIN positions p ON p.position_id = o.signal_id"
            " WHERE o.leg='ENTRY' AND o.status='FILLED' AND o.mode=?"
            " AND substr(o.created_at,1,10)=? AND p.position_id IS NULL",
            (mode, day.isoformat()),
        )
        return [str(r["signal_id"]) for r in rows]

    def attempts_used(self, signal_id: str, leg: str) -> int:
        """How many orders this signal already has for `leg`: the next attempt number is +1."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM orders WHERE signal_id=? AND leg=?", (signal_id, leg)
        ).fetchone()
        return int(row["n"])

    def load_order(self, idempotency_key: str) -> OrderRecord | None:
        """Rebuild an order from its row (after a restart), so it can be polled or cancelled."""
        row = self._conn.execute(
            "SELECT * FROM orders WHERE idempotency_key=?", (idempotency_key,)
        ).fetchone()
        if row is None:
            return None
        signal_id, leg, attempt = str(row["idempotency_key"]).rsplit(":", 2)
        intent = OrderIntent(
            signal_id=signal_id,
            leg=OrderLeg(leg),
            trading_symbol=row["trading_symbol"],
            side=row["side"],
            quantity=int(row["quantity"]),
            order_type=row["order_type"],
            price=row["price"],
            trigger_price=row["trigger_price"],
            product=row["product"],
            attempt=int(attempt),
        )
        record = OrderRecord(
            intent=intent,
            status=OrderStatus(row["status"]),
            broker_order_id=row["broker_order_id"],
            filled_qty=int(row["filled_qty"]),
            avg_price=float(row["avg_price"]),
            reject_reason=row["reject_reason"],
            mode=row["mode"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
        return record


def _load(state: str) -> Position:
    position = Position.from_dict(json.loads(state))
    position.opened_at = position.opened_at.astimezone(IST)
    if position.closed_at is not None:
        position.closed_at = position.closed_at.astimezone(IST)
    if position.last_candle is not None:
        position.last_candle = position.last_candle.astimezone(IST)
    position.fills = [_localize(f) for f in position.fills]
    return position


def _localize(fill: Fill) -> Fill:
    return Fill(
        time=fill.time.astimezone(IST),
        reason=fill.reason,
        lots=fill.lots,
        quantity=fill.quantity,
        futures_price=fill.futures_price,
        option_price=fill.option_price,
        price_source=fill.price_source,
        option_gross=fill.option_gross,
        futures_points=fill.futures_points,
        charges=fill.charges,
    )


def _insert_fill(conn: sqlite3.Connection, position_id: str, seq: int, fill: Fill) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO fills (position_id, seq, fill_time, reason, lots, quantity,"
        " fut_price, opt_price, source, gross, fut_points, charges)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            position_id,
            seq,
            fill.time.isoformat(),
            fill.reason.value,
            fill.lots,
            fill.quantity,
            fill.futures_price,
            fill.option_price,
            fill.price_source,
            fill.option_gross,
            fill.futures_points,
            fill.charges,
        ),
    )
