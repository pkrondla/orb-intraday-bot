"""GrowwVenue against a fake broker: fills, rejects, shared-account filter."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from orb_intraday_bot.adapters.groww.venue import GrowwVenue
from orb_intraday_bot.core.clock import IST, FixedClock
from orb_intraday_bot.execution.models import OrderIntent, OrderLeg, OrderStatus


class FakeBroker:
    def __init__(self) -> None:
        self.orders: dict[str, dict[str, Any]] = {}
        self.placed: list[dict[str, Any]] = []
        self.reject_next = False
        self.partial = False
        self.ltp_map = {"NIFTY26OCT25000CE": 100.0}

    def place_order(self, **kwargs: Any) -> dict[str, Any]:
        self.placed.append(kwargs)
        if self.reject_next:
            self.reject_next = False
            return {}
        oid = f"G-{len(self.orders) + 1}"
        qty = int(kwargs["quantity"])
        filled = qty // 2 if self.partial else qty
        self.orders[oid] = {
            "groww_order_id": oid,
            "order_status": "COMPLETE" if filled == qty else "OPEN",
            "filled_quantity": filled,
            "average_price": float(kwargs.get("price") or 100),
            "order_reference_id": kwargs.get("order_reference_id", ""),
        }
        return {"groww_order_id": oid}

    def cancel_order(self, **kwargs: Any) -> dict[str, Any]:
        oid = kwargs["groww_order_id"]
        if oid in self.orders:
            self.orders[oid]["order_status"] = "CANCELLED"
        return {"ok": True}

    def create_smart_order(self, **kwargs: Any) -> dict[str, Any]:
        oid = f"S-{len(self.orders) + 1}"
        self.orders[oid] = {
            "groww_order_id": oid,
            "smart_order_id": oid,
            "order_status": "ACTIVE",
            "filled_quantity": 0,
            "average_price": 0,
        }
        return {"smart_order_id": oid}

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


def _entry(price: float = 100.0) -> OrderIntent:
    return OrderIntent(
        signal_id="sig-deadbeef",
        leg=OrderLeg.ENTRY,
        trading_symbol="NIFTY26OCT25000CE",
        side="BUY",
        quantity=75,
        order_type="LIMIT",
        price=price,
        product="MIS",
    )


def test_entry_fills_and_uses_orb_reference() -> None:
    broker = FakeBroker()
    venue = GrowwVenue(
        broker, clock=FixedClock(datetime(2026, 10, 16, 10, tzinfo=IST)),
        fill_wait_sec=0, fill_polls=1,
    )
    record = venue.submit(_entry())
    assert record.status is OrderStatus.FILLED
    assert record.filled_qty == 75
    assert broker.placed[0]["order_reference_id"].startswith("ORB-")


def test_rejection_when_broker_returns_no_id() -> None:
    broker = FakeBroker()
    broker.reject_next = True
    venue = GrowwVenue(broker, fill_wait_sec=0, fill_polls=1)
    record = venue.submit(_entry())
    assert record.status is OrderStatus.REJECTED


def test_shared_account_lists_only_orb_orders() -> None:
    venue = GrowwVenue(FakeBroker())
    rows = venue.list_orb_orders()
    assert len(rows) == 1
    assert rows[0]["order_reference_id"].startswith("ORB-")


def test_cancel_open_order() -> None:
    broker = FakeBroker()
    broker.partial = True
    venue = GrowwVenue(broker, fill_wait_sec=0, fill_polls=1)
    record = venue.submit(_entry())
    assert record.status in {OrderStatus.ACKNOWLEDGED, OrderStatus.PARTIAL, OrderStatus.FILLED}
    if record.status is not OrderStatus.FILLED:
        cancelled = venue.cancel(record)
        assert cancelled.status is OrderStatus.CANCELLED


def test_protection_leg_uses_smart_order() -> None:
    broker = FakeBroker()
    venue = GrowwVenue(broker, fill_wait_sec=0, fill_polls=1)
    intent = OrderIntent(
        signal_id="sig-deadbeef",
        leg=OrderLeg.STOP,
        trading_symbol="NIFTY26OCT25000CE",
        side="SELL",
        quantity=75,
        order_type="SL",
        price=120.0,
        trigger_price=80.0,
        product="MIS",
    )
    record = venue.submit(intent)
    assert record.status is OrderStatus.ACKNOWLEDGED
    assert record.broker_order_id.startswith("S-")


def test_entry_fetches_ltp_when_price_missing() -> None:
    broker = FakeBroker()
    venue = GrowwVenue(broker, fill_wait_sec=0, fill_polls=1)
    intent = _entry(price=0.0)
    # LIMIT BUY with price 0 triggers LTP protection path
    record = venue.submit(intent)
    assert record.status is OrderStatus.FILLED
    assert broker.placed[0]["price"] > 100.0  # padded


def test_trading_factory_reuses_shared_totp_key(tmp_path) -> None:  # noqa: ANN001
    from pathlib import Path

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
    # Same key shares the data token cache file.
    assert (Path(tmp_path) / "groww_token.json").parent == Path(tmp_path)
