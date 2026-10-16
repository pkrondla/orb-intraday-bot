"""Order intents, paper/shadow venues, order manager, schema migrations."""

from __future__ import annotations

from datetime import datetime

import pytest

from orb_intraday_bot.core.clock import IST, FixedClock
from orb_intraday_bot.execution.models import (
    OrderIntent,
    OrderLeg,
    OrderStatus,
    can_transition,
)
from orb_intraday_bot.execution.order_manager import OrderManager
from orb_intraday_bot.execution.paper_venue import PaperVenue
from orb_intraday_bot.execution.shadow_venue import ShadowVenue
from orb_intraday_bot.storage.migrations import SCHEMA_VERSION, apply_migrations, current_version
from orb_intraday_bot.storage.store import TradeStore


def _intent(qty: int = 75, price: float | None = 100.0) -> OrderIntent:
    return OrderIntent(
        signal_id="sig-abc12345",
        leg=OrderLeg.ENTRY,
        trading_symbol="NIFTY26OCT25000CE",
        side="BUY",
        quantity=qty,
        order_type="LIMIT",
        price=price,
    )


def test_reference_id_is_orb_prefixed_and_short() -> None:
    ref = _intent().reference_id
    assert ref.startswith("ORB-")
    assert len(ref) <= 20


def test_order_transitions() -> None:
    assert can_transition(OrderStatus.NEW, OrderStatus.SENT)
    assert can_transition(OrderStatus.SENT, OrderStatus.FILLED)
    assert not can_transition(OrderStatus.FILLED, OrderStatus.CANCELLED)


def test_paper_venue_fills_immediately() -> None:
    clock = FixedClock(datetime(2026, 10, 16, 10, 0, tzinfo=IST))
    venue = PaperVenue(clock=clock)
    record = venue.submit(_intent())
    assert record.status is OrderStatus.FILLED
    assert record.filled_qty == 75
    assert record.avg_price == 100.0
    assert record.fills[0].broker_order_id.startswith("PAPER-")


def test_paper_venue_rejection_and_idempotent_manager() -> None:
    intent = _intent()
    venue = PaperVenue(reject_keys={intent.idempotency_key})
    mgr = OrderManager(venue)
    first = mgr.submit(intent)
    second = mgr.submit(intent)
    assert first is second
    assert first.status is OrderStatus.REJECTED


def test_shadow_venue_logs_would_have_filled() -> None:
    quotes = {"NIFTY26OCT25000CE": 12.5}
    venue = ShadowVenue(quote=quotes.get)
    intent = _intent(price=None)
    record = venue.submit(intent)
    assert record.status is OrderStatus.FILLED
    assert venue.log[0].would_fill_price == 12.5


def test_migrations_create_orders_and_events(tmp_path) -> None:  # noqa: ANN001
    store = TradeStore(tmp_path / "t.sqlite")
    assert current_version(store._conn) == SCHEMA_VERSION  # noqa: SLF001
    intent = _intent()
    venue = PaperVenue()
    record = venue.submit(intent)
    store.save_order(record)
    store.append_event("OrderFilled", {"key": intent.idempotency_key}, signal_id=intent.signal_id)
    rows = store.orders_for_signal(intent.signal_id)
    assert len(rows) == 1
    assert rows[0]["status"] == "FILLED"
    store.close()


def test_legacy_db_migrates_from_v1(tmp_path) -> None:  # noqa: ANN001
    import sqlite3

    path = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE signals (signal_id TEXT PRIMARY KEY, trade_date TEXT, "
        "candle_start TEXT, setup TEXT, direction TEXT, level_name TEXT, "
        "level_value REAL, entry_price REAL, sign_id TEXT, status TEXT, "
        "stage TEXT, reason TEXT, note TEXT);"
        "CREATE TABLE positions (position_id TEXT PRIMARY KEY, trade_date TEXT, "
        "status TEXT, state TEXT, updated_at TEXT);"
        "CREATE TABLE fills (position_id TEXT, seq INTEGER, fill_time TEXT, "
        "reason TEXT, lots INTEGER, quantity INTEGER, fut_price REAL, "
        "opt_price REAL, source TEXT, gross REAL, fut_points REAL, charges REAL, "
        "PRIMARY KEY (position_id, seq));"
    )
    conn.commit()
    assert current_version(conn) == 1
    assert apply_migrations(conn) == SCHEMA_VERSION
    conn.close()
    # Re-open via TradeStore
    store = TradeStore(path)
    assert current_version(store._conn) == SCHEMA_VERSION  # noqa: SLF001
    store.close()


def test_illegal_transition_raises() -> None:
    from orb_intraday_bot.execution.models import OrderRecord

    record = OrderRecord(intent=_intent(), status=OrderStatus.FILLED)
    with pytest.raises(ValueError):
        record.transition(OrderStatus.CANCELLED)
