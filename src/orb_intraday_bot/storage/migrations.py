"""Versioned SQLite schema migrations.

Each migration is idempotent. The current schema version is stored in `schema_meta`.
"""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 3

_MIGRATIONS: dict[int, str] = {
    1: """
        CREATE TABLE IF NOT EXISTS signals (
            signal_id    TEXT PRIMARY KEY,
            trade_date   TEXT NOT NULL,
            candle_start TEXT NOT NULL,
            setup        TEXT NOT NULL,
            direction    TEXT NOT NULL,
            level_name   TEXT NOT NULL,
            level_value  REAL NOT NULL,
            entry_price  REAL NOT NULL,
            sign_id      TEXT NOT NULL,
            status       TEXT NOT NULL,
            stage        TEXT NOT NULL DEFAULT '',
            reason       TEXT NOT NULL DEFAULT '',
            note         TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS signals_day ON signals (trade_date);

        CREATE TABLE IF NOT EXISTS positions (
            position_id TEXT PRIMARY KEY,
            trade_date  TEXT NOT NULL,
            status      TEXT NOT NULL,
            state       TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS positions_day ON positions (trade_date, status);

        CREATE TABLE IF NOT EXISTS fills (
            position_id TEXT NOT NULL,
            seq         INTEGER NOT NULL,
            fill_time   TEXT NOT NULL,
            reason      TEXT NOT NULL,
            lots        INTEGER NOT NULL,
            quantity    INTEGER NOT NULL,
            fut_price   REAL NOT NULL,
            opt_price   REAL NOT NULL,
            source      TEXT NOT NULL,
            gross       REAL NOT NULL,
            fut_points  REAL NOT NULL,
            charges     REAL NOT NULL,
            PRIMARY KEY (position_id, seq)
        );

        CREATE TABLE IF NOT EXISTS schema_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """,
    2: """
        CREATE TABLE IF NOT EXISTS orders (
            idempotency_key TEXT PRIMARY KEY,
            signal_id       TEXT NOT NULL,
            leg             TEXT NOT NULL,
            trading_symbol  TEXT NOT NULL,
            side            TEXT NOT NULL,
            quantity        INTEGER NOT NULL,
            order_type      TEXT NOT NULL,
            price           REAL,
            trigger_price   REAL,
            product         TEXT NOT NULL,
            status          TEXT NOT NULL,
            broker_order_id TEXT NOT NULL DEFAULT '',
            filled_qty      INTEGER NOT NULL DEFAULT 0,
            avg_price       REAL NOT NULL DEFAULT 0,
            reject_reason   TEXT NOT NULL DEFAULT '',
            reference_id    TEXT NOT NULL DEFAULT '',
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS orders_signal ON orders (signal_id);
        CREATE INDEX IF NOT EXISTS orders_status ON orders (status);
        CREATE INDEX IF NOT EXISTS orders_reference ON orders (reference_id);

        CREATE TABLE IF NOT EXISTS order_fills (
            idempotency_key TEXT NOT NULL,
            seq             INTEGER NOT NULL,
            fill_time       TEXT NOT NULL,
            quantity        INTEGER NOT NULL,
            price           REAL NOT NULL,
            broker_order_id TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (idempotency_key, seq)
        );

        CREATE TABLE IF NOT EXISTS events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            event_time TEXT NOT NULL,
            event_type TEXT NOT NULL,
            signal_id  TEXT NOT NULL DEFAULT '',
            payload    TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS events_time ON events (event_time);
        CREATE INDEX IF NOT EXISTS events_type ON events (event_type);
        """,
    3: """
        ALTER TABLE orders ADD COLUMN mode TEXT NOT NULL DEFAULT 'paper';
        ALTER TABLE events ADD COLUMN mode TEXT NOT NULL DEFAULT '';
        CREATE INDEX IF NOT EXISTS orders_mode ON orders (mode);
        """,
}


def current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'"
    ).fetchone()
    if row is None:
        # Legacy DB from before migrations: signals table exists without schema_meta.
        legacy = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='signals'"
        ).fetchone()
        return 1 if legacy is not None else 0
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    return int(row[0]) if row else 0


def apply_migrations(conn: sqlite3.Connection) -> int:
    """Apply pending migrations. Returns the schema version after applying."""
    version = current_version(conn)
    # Legacy DBs (signals table, no schema_meta) report version 1; ensure the meta table exists.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    for target in range(version + 1, SCHEMA_VERSION + 1):
        sql = _MIGRATIONS.get(target)
        if not sql:
            raise RuntimeError(f"missing migration for schema version {target}")
        conn.executescript(sql)
        conn.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(target),),
        )
        conn.commit()
        version = target
    # Stamp the version even when no migrations ran (fresh DB after v1+v2, or already current).
    if version >= 1:
        conn.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(version),),
        )
        conn.commit()
    return version
