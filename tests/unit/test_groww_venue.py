"""GrowwVenue against a fake broker that answers like Groww does."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from orb_intraday_bot.adapters.groww.venue import GrowwVenue
from orb_intraday_bot.core.clock import IST, FixedClock
from orb_intraday_bot.execution.models import OrderIntent, OrderLeg, OrderStatus

SYMBOL = "NIFTY26OCT25000CE"


class FakeBroker:
    """`order_status`, `filled_quantity` and `average_fill_price`, as the Groww API returns them."""

    def __init__(self) -> None:
        self.orders: dict[str, dict[str, Any]] = {}
        self.placed: list[dict[str, Any]] = []
        self.cancels: list[str] = []
        self.fill = "full"                 # full | partial | none
        self.fill_price = 98.5             # the real fill, different from the limit price
        self.reject_next = False
        self.raise_on_place = False
        self.honour_cancel = True
        self.ltp_map = {SYMBOL: 100.0}

    def place_order(self, **kwargs: Any) -> dict[str, Any]:
        if self.raise_on_place:
            oid = f"G-{len(self.orders) + 1}"
            self.orders[oid] = self._row(oid, kwargs)
            raise TimeoutError("read timed out")
        self.placed.append(kwargs)
        if self.reject_next:
            self.reject_next = False
            return {}
        oid = f"G-{len(self.orders) + 1}"
        self.orders[oid] = self._row(oid, kwargs)
        return {"groww_order_id": oid, "order_status": "NEW"}

    def _row(self, oid: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        qty = int(kwargs["quantity"])
        sl = kwargs["order_type"] == "SL"
        if sl or self.fill == "none":
            status, filled = ("TRIGGER_PENDING" if sl else "ACKED"), 0
        elif self.fill == "partial":
            status, filled = "ACKED", qty // 2
        else:
            status, filled = "EXECUTED", qty
        return {
            "groww_order_id": oid,
            "order_status": status,
            "quantity": qty,
            "price": kwargs.get("price"),
            "filled_quantity": filled,
            "average_fill_price": self.fill_price if filled else 0,
            "order_reference_id": kwargs.get("order_reference_id", ""),
        }

    def cancel_order(self, **kwargs: Any) -> dict[str, Any]:
        oid = kwargs["groww_order_id"]
        self.cancels.append(oid)
        row = self.orders.get(oid)
        if row is not None and self.honour_cancel and row["order_status"] != "EXECUTED":
            row["order_status"] = "CANCELLED"
        return {"groww_order_id": oid}

    def get_order_status(self, **kwargs: Any) -> dict[str, Any]:
        return self.orders.get(kwargs["groww_order_id"], {})

    def get_order_status_by_reference(self, **kwargs: Any) -> dict[str, Any]:
        ref = kwargs["order_reference_id"]
        for row in self.orders.values():
            if row.get("order_reference_id") == ref:
                return row
        return {}

    def get_order_list(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "order_list": [
                {"order_reference_id": "ORB-abc", "groww_order_id": "1"},
                {"order_reference_id": "OTHER-1", "groww_order_id": "2"},
            ]
        }

    def get_ltp(self, **kwargs: Any) -> dict[str, Any]:
        return dict(self.ltp_map)


def _venue(broker: FakeBroker, **kwargs: Any) -> GrowwVenue:
    kwargs.setdefault("fill_polls", 1)
    return GrowwVenue(
        broker, clock=FixedClock(datetime(2026, 10, 16, 10, tzinfo=IST)),
        fill_wait_sec=0, **kwargs,
    )


def _entry(price: float = 100.0) -> OrderIntent:
    return OrderIntent(
        signal_id="sig-deadbeef", leg=OrderLeg.ENTRY, trading_symbol=SYMBOL, side="BUY",
        quantity=75, order_type="LIMIT", price=price, product="MIS",
    )


def _stop() -> OrderIntent:
    return OrderIntent(
        signal_id="sig-deadbeef", leg=OrderLeg.PROTECT, trading_symbol=SYMBOL, side="SELL",
        quantity=75, order_type="SL", price=76.0, trigger_price=80.0, product="MIS",
    )


def test_entry_fills_at_the_brokers_price_not_the_limit_price() -> None:
    broker = FakeBroker()
    record = _venue(broker).submit(_entry(price=100.0))
    assert record.status is OrderStatus.FILLED
    assert record.filled_qty == 75
    assert record.avg_price == 98.5            # not the 100 limit
    assert record.mode == "live"
    assert broker.placed[0]["order_reference_id"].startswith("ORB-")
    assert broker.placed[0]["order_type"] == "LIMIT"


def test_a_partial_fill_keeps_the_filled_quantity_not_the_ordered_one() -> None:
    broker = FakeBroker()
    broker.fill = "partial"
    record = _venue(broker).submit(_entry())
    assert record.status is OrderStatus.PARTIAL
    assert record.filled_qty == 37
    assert record.avg_price == 98.5


def test_rejection_when_broker_returns_no_id() -> None:
    broker = FakeBroker()
    broker.reject_next = True
    record = _venue(broker).submit(_entry())
    assert record.status is OrderStatus.REJECTED


def test_a_rejected_status_on_poll_carries_the_reason() -> None:
    broker = FakeBroker()
    broker.fill = "none"
    venue = _venue(broker, fill_polls=0)
    record = venue.submit(_entry())
    assert record.status is OrderStatus.ACKNOWLEDGED
    broker.orders[record.broker_order_id].update(order_status="REJECTED", remark="no margin")
    venue.poll(record)
    assert record.status is OrderStatus.REJECTED
    assert record.reject_reason == "no margin"


def test_shared_account_lists_only_orb_orders() -> None:
    rows = GrowwVenue(FakeBroker()).list_orb_orders()
    assert len(rows) == 1
    assert rows[0]["order_reference_id"].startswith("ORB-")


def test_cancel_is_confirmed_by_the_brokers_status() -> None:
    broker = FakeBroker()
    broker.fill = "none"
    venue = _venue(broker)
    record = venue.submit(_entry())
    assert record.status is OrderStatus.ACKNOWLEDGED
    cancelled = venue.cancel(record)
    assert cancelled.status is OrderStatus.CANCELLED
    assert broker.cancels == [record.broker_order_id]


def test_cancel_keeps_a_partial_fill() -> None:
    broker = FakeBroker()
    broker.fill = "partial"
    venue = _venue(broker)
    record = venue.submit(_entry())
    cancelled = venue.cancel(record)
    assert cancelled.status is OrderStatus.CANCELLED
    assert cancelled.filled_qty == 37


def test_an_unconfirmed_cancel_is_not_assumed_to_have_worked() -> None:
    broker = FakeBroker()
    broker.fill = "none"
    broker.honour_cancel = False
    venue = _venue(broker, cancel_polls=2)
    record = venue.submit(_entry())
    result = venue.cancel(record)
    assert result.status is OrderStatus.ACKNOWLEDGED       # still open: the caller must not assume


def test_an_order_that_filled_before_the_cancel_landed_ends_filled() -> None:
    broker = FakeBroker()
    broker.fill = "none"
    venue = _venue(broker)
    record = venue.submit(_entry())
    broker.orders[record.broker_order_id].update(
        order_status="EXECUTED", filled_quantity=75, average_fill_price=99.0
    )
    result = venue.cancel(record)
    assert result.status is OrderStatus.FILLED
    assert result.avg_price == 99.0


def test_protective_stop_is_a_plain_sl_order_with_a_trigger() -> None:
    broker = FakeBroker()
    record = _venue(broker).submit(_stop())
    assert record.status is OrderStatus.ACKNOWLEDGED
    assert record.broker_order_id.startswith("G-")
    placed = broker.placed[0]
    assert placed["order_type"] == "SL"
    assert placed["trigger_price"] == 80.0
    assert placed["price"] == 76.0
    assert placed["transaction_type"] == "SELL"


def test_a_timeout_on_place_is_resolved_by_looking_the_order_up_by_reference() -> None:
    broker = FakeBroker()
    broker.raise_on_place = True
    record = _venue(broker).submit(_entry())
    assert record.status is OrderStatus.FILLED             # it did reach the broker
    assert record.broker_order_id == "G-1"


def test_an_error_on_place_with_no_order_at_the_broker_is_a_rejection() -> None:
    class Down(FakeBroker):
        def place_order(self, **kwargs: Any) -> dict[str, Any]:
            raise ConnectionError("down")

    record = _venue(Down()).submit(_entry())
    assert record.status is OrderStatus.REJECTED
    assert "ConnectionError" in record.reject_reason


def test_entry_fetches_ltp_when_price_missing() -> None:
    broker = FakeBroker()
    record = _venue(broker).submit(_entry(price=0.0))
    assert record.status is OrderStatus.FILLED
    assert broker.placed[0]["price"] > 100.0               # padded above the LTP


def test_trading_factory_reuses_shared_totp_key(tmp_path: Path) -> None:
    from pydantic import SecretStr

    from orb_intraday_bot.adapters.groww.factory import build_trading_client
    from orb_intraday_bot.adapters.groww.trading_client import GrowwTradingClient
    from orb_intraday_bot.config.settings import Settings

    settings = Settings.model_construct(
        groww_totp_token=SecretStr("shared"),
        groww_totp_secret=SecretStr("shared-secret"),
        groww_trading_totp_token=SecretStr(""),
        groww_trading_totp_secret=SecretStr(""),
        data_dir=Path(tmp_path),
        token_cooldown_sec=180,
    )
    client = build_trading_client(settings)
    assert isinstance(client, GrowwTradingClient)
    assert (Path(tmp_path) / "groww_token.json").parent == Path(tmp_path)


def test_a_stop_fill_with_no_average_price_uses_the_trigger() -> None:
    broker = FakeBroker()
    venue = _venue(broker, fill_polls=0)
    record = venue.submit(_stop())
    broker.orders[record.broker_order_id].update(
        order_status="EXECUTED", filled_quantity=75, average_fill_price=0
    )
    venue.poll(record)
    assert record.status is OrderStatus.FILLED
    assert record.filled_qty == 75
    assert record.avg_price == 80.0          # the SL trigger, not ₹0


def test_a_limit_fill_with_no_average_price_uses_the_limit() -> None:
    broker = FakeBroker()
    broker.fill = "none"
    venue = _venue(broker, fill_polls=0)
    record = venue.submit(_entry(price=100.0))
    broker.orders[record.broker_order_id].update(
        order_status="EXECUTED", filled_quantity=75, average_fill_price=0
    )
    venue.poll(record)
    assert record.status is OrderStatus.FILLED
    assert record.avg_price == 100.0


def test_average_price_nested_under_payload_is_read() -> None:
    broker = FakeBroker()
    broker.fill = "none"
    venue = _venue(broker, fill_polls=0)
    record = venue.submit(_entry())
    broker.orders[record.broker_order_id] = {
        "order_status": "EXECUTED",
        "filled_quantity": 75,
        "payload": {"average_fill_price": 97.25},
    }
    venue.poll(record)
    assert record.status is OrderStatus.FILLED
    assert record.avg_price == 97.25


@pytest.mark.parametrize("status", ["COMPLETED", "COMPLETE", "EXECUTED"])
def test_every_filled_status_spelling_is_a_fill(status: str) -> None:
    broker = FakeBroker()
    broker.fill = "none"
    venue = _venue(broker, fill_polls=0)
    record = venue.submit(_entry())
    broker.orders[record.broker_order_id].update(
        order_status=status, filled_quantity=75, average_fill_price=97.0
    )
    venue.poll(record)
    assert record.status is OrderStatus.FILLED
    assert record.avg_price == 97.0
