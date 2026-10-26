"""Groww execution venue: the real broker order step.

Every order is a plain `place_order` call (LIMIT, MARKET or SL), tagged with an `ORB-` reference
id so the bot only ever reconciles its own orders on a shared account. The protective stop is an
ordinary stop-loss-limit sell that stays at the exchange; the bot manages every other exit itself.

Response fields follow the Groww API: `order_status` (NEW, ACKED, TRIGGER_PENDING, APPROVED,
EXECUTED, COMPLETED, REJECTED, FAILED, CANCELLED, CANCELLATION_REQUESTED, ...),
`filled_quantity` and `average_fill_price`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Protocol

from orb_intraday_bot.core.clock import IST, Clock, SystemClock
from orb_intraday_bot.execution.models import (
    FillReport,
    OrderIntent,
    OrderLeg,
    OrderRecord,
    OrderStatus,
)

log = logging.getLogger("orb.groww.venue")

_TERMINAL = {OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.CANCELLED}
_FILLED_STATUSES = {"COMPLETE", "COMPLETED", "FILLED", "EXECUTED"}
_REJECTED_STATUSES = {"REJECTED", "FAILED"}
_CANCELLED_STATUSES = {"CANCELLED", "CANCELED"}


class TradingApi(Protocol):
    """Minimal surface the venue needs. GrowwTradingClient and fakes implement this."""

    def place_order(self, **kwargs: Any) -> dict[str, Any]: ...
    def cancel_order(self, **kwargs: Any) -> dict[str, Any]: ...
    def get_order_status(self, **kwargs: Any) -> dict[str, Any]: ...
    def get_order_status_by_reference(self, **kwargs: Any) -> dict[str, Any]: ...
    def get_order_list(self, **kwargs: Any) -> Any: ...
    def get_ltp(self, **kwargs: Any) -> dict[str, Any]: ...


def _as_dict(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    return {}


def _extract_order_id(payload: dict[str, Any]) -> str:
    for key in ("groww_order_id", "order_id", "id"):
        if payload.get(key):
            return str(payload[key])
    nested = payload.get("payload") or payload.get("data") or {}
    if isinstance(nested, dict):
        for key in ("groww_order_id", "order_id", "id"):
            if nested.get(key):
                return str(nested[key])
    return ""


def _extract_status(payload: dict[str, Any]) -> str:
    for key in ("order_status", "status"):
        if payload.get(key):
            return str(payload[key]).upper()
    return ""


def _extract_avg_price(payload: dict[str, Any]) -> float:
    # `price` is deliberately not read: it is the order's limit price, not what it filled at.
    blobs: list[dict[str, Any]] = [payload]
    for key in ("payload", "data", "order"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            blobs.append(nested)
    for blob in blobs:
        for key in ("average_fill_price", "average_price", "avg_price", "filled_price"):
            try:
                value = float(blob.get(key) or 0)
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
    return 0.0


def _fallback_fill_price(record: OrderRecord) -> float:
    """When Groww reports a fill with no average price (today's PE stop was `65@0.00`)."""
    intent = record.intent
    if intent.trigger_price and intent.trigger_price > 0:
        return float(intent.trigger_price)
    if intent.price and intent.price > 0:
        return float(intent.price)
    return 0.0


def _extract_filled_qty(payload: dict[str, Any]) -> int:
    # `quantity` is deliberately not read: it is the ordered quantity, not what was filled.
    for key in ("filled_quantity", "filled_qty"):
        try:
            value = int(payload.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0


class GrowwVenue:
    def __init__(
        self,
        client: TradingApi,
        clock: Clock | None = None,
        sleep: Callable[[float], None] | None = None,
        *,
        product: str = "MIS",
        entry_price_protection_pct: float = 0.02,
        fill_wait_sec: float = 2.0,
        fill_polls: int = 5,
        cancel_polls: int = 3,
    ) -> None:
        self._client = client
        self._product = product
        self._entry_price_protection_pct = entry_price_protection_pct
        self._clock = clock or SystemClock()
        self._sleep = sleep or (lambda _: None)
        self._fill_wait_sec = fill_wait_sec
        self._fill_polls = fill_polls
        self._cancel_polls = cancel_polls
        self._orders: dict[str, OrderRecord] = {}

    def submit(self, intent: OrderIntent) -> OrderRecord:
        now = self._clock.now().astimezone(IST)
        record = OrderRecord(intent=intent, created_at=now, updated_at=now, mode="live")
        record.transition(OrderStatus.SENT, at=now)
        try:
            payload = self._place(intent)
        except Exception as exc:
            # A timeout does not prove the order never reached the broker. Look it up by its
            # reference before calling it rejected, or a live order could go untracked.
            payload = self._lookup_by_reference(intent)
            if not _extract_order_id(payload):
                record.transition(OrderStatus.REJECTED, at=now)
                record.reject_reason = f"{type(exc).__name__}: {exc}"
                self._orders[intent.idempotency_key] = record
                return record
            log.warning("order call failed but the order exists at the broker: %s", exc)

        order_id = _extract_order_id(_as_dict(payload))
        record.broker_order_id = order_id
        record.transition(OrderStatus.ACKNOWLEDGED, at=now)
        if not order_id:
            record.transition(OrderStatus.REJECTED, at=now)
            record.reject_reason = f"no order id in response: {payload!r}"[:300]
            self._orders[intent.idempotency_key] = record
            return record

        if intent.leg is not OrderLeg.PROTECT:
            self._await_fill(record)
        self._orders[intent.idempotency_key] = record
        return record

    def cancel(self, record: OrderRecord) -> OrderRecord:
        """Ask the broker to cancel, then confirm the outcome from the order's own status.

        The order is only marked cancelled when the broker says so. If it filled before the cancel
        landed, the record ends FILLED. If the outcome is still unknown after a few polls the
        record stays open, and the caller must not assume either way.
        """
        if record.status in _TERMINAL:
            return record
        if record.broker_order_id:
            try:
                self._client.cancel_order(groww_order_id=record.broker_order_id, segment="FNO")
            except Exception as exc:
                log.warning("cancel request failed: %s: %s", type(exc).__name__, exc)
        for attempt in range(max(self._cancel_polls, 1)):
            self.poll(record)
            if record.status in _TERMINAL:
                break
            if attempt < self._cancel_polls - 1:
                self._sleep(self._fill_wait_sec)
        self._orders[record.intent.idempotency_key] = record
        return record

    def poll(self, record: OrderRecord) -> OrderRecord:
        if record.status in _TERMINAL:
            return record
        if not record.broker_order_id and not record.intent.reference_id:
            return record
        try:
            if record.broker_order_id:
                payload = _as_dict(
                    self._client.get_order_status(
                        segment="FNO", groww_order_id=record.broker_order_id
                    )
                )
            else:
                payload = _as_dict(
                    self._client.get_order_status_by_reference(
                        segment="FNO", order_reference_id=record.intent.reference_id
                    )
                )
        except Exception as exc:
            log.warning("poll failed: %s", type(exc).__name__)
            return record
        self._apply_status(record, payload)
        self._orders[record.intent.idempotency_key] = record
        return record

    def recent_fills(self, record: OrderRecord) -> list[FillReport]:
        return list(record.fills)

    def list_orb_orders(self) -> list[dict[str, Any]]:
        """Orders on the shared account that belong to this bot (ORB- reference prefix)."""
        try:
            raw = self._client.get_order_list(segment="FNO")
        except Exception as exc:
            log.warning("get_order_list failed: %s", type(exc).__name__)
            return []
        rows: list[Any]
        if isinstance(raw, dict):
            rows = list(raw.get("order_list") or raw.get("orders") or raw.get("data") or [])
        elif isinstance(raw, list):
            rows = raw
        else:
            rows = []
        out: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            ref = str(
                row.get("order_reference_id")
                or row.get("reference_id")
                or row.get("client_order_id")
                or ""
            )
            if ref.startswith("ORB-"):
                out.append(row)
        return out

    def _lookup_by_reference(self, intent: OrderIntent) -> dict[str, Any]:
        try:
            return _as_dict(
                self._client.get_order_status_by_reference(
                    segment="FNO", order_reference_id=intent.reference_id
                )
            )
        except Exception:
            return {}

    def _place(self, intent: OrderIntent) -> dict[str, Any]:
        price = intent.price or 0.0
        if intent.order_type == "LIMIT" and intent.side == "BUY" and price <= 0:
            # Protect: fetch LTP and pad.
            ltp = self._ltp(intent.trading_symbol)
            price = round(ltp * (1 + self._entry_price_protection_pct), 2)
        kwargs: dict[str, Any] = {
            "validity": "DAY",
            "exchange": "NSE",
            "order_type": intent.order_type,
            "product": intent.product or self._product,
            "quantity": intent.quantity,
            "segment": "FNO",
            "trading_symbol": intent.trading_symbol,
            "transaction_type": intent.side,
            "price": float(price or 0.0),
            "order_reference_id": intent.reference_id,
        }
        if intent.trigger_price is not None:
            kwargs["trigger_price"] = float(intent.trigger_price)
        return _as_dict(self._client.place_order(**kwargs))

    def _await_fill(self, record: OrderRecord) -> None:
        for _ in range(self._fill_polls):
            self.poll(record)
            if record.status in _TERMINAL:
                return
            self._sleep(self._fill_wait_sec)
        # Leave as ACKNOWLEDGED / PARTIAL if not filled yet.

    def _apply_status(self, record: OrderRecord, payload: dict[str, Any]) -> None:
        now = self._clock.now().astimezone(IST)
        status = _extract_status(payload)
        filled = _extract_filled_qty(payload)
        broker_avg = _extract_avg_price(payload)
        avg = broker_avg if broker_avg > 0 else _fallback_fill_price(record)
        if (
            broker_avg <= 0
            and avg > 0
            and (filled > 0 or status in _FILLED_STATUSES)
        ):
            log.warning(
                "broker fill for %s has no average price; using %.2f (%s)",
                record.intent.trading_symbol, avg,
                "trigger" if record.intent.trigger_price else "limit",
            )
        if filled > record.filled_qty and avg > 0:
            qty = filled - record.filled_qty
            record.fills.append(
                FillReport(
                    intent_key=record.intent.idempotency_key,
                    quantity=qty,
                    price=avg,
                    time=now,
                    broker_order_id=record.broker_order_id,
                    raw=payload,
                )
            )
            record.filled_qty = filled
            record.avg_price = avg
        if status in _FILLED_STATUSES or (
            filled >= record.intent.quantity > 0 and record.filled_qty >= record.intent.quantity
        ):
            if record.status is not OrderStatus.FILLED:
                if record.filled_qty <= 0:
                    record.filled_qty = record.intent.quantity
                    if avg > 0:
                        record.avg_price = avg
                        record.fills.append(
                            FillReport(
                                intent_key=record.intent.idempotency_key,
                                quantity=record.intent.quantity,
                                price=avg,
                                time=now,
                                broker_order_id=record.broker_order_id,
                                raw=payload,
                            )
                        )
                record.transition(OrderStatus.FILLED, at=now)
        elif status in _REJECTED_STATUSES:
            if record.status is not OrderStatus.REJECTED:
                record.transition(OrderStatus.REJECTED, at=now)
                record.reject_reason = (
                    str(payload.get("remark") or status)[:200] if payload else status
                )
        elif status in _CANCELLED_STATUSES and record.status not in {
            OrderStatus.CANCELLED,
            OrderStatus.FILLED,
        }:
            record.transition(OrderStatus.CANCELLED, at=now)
        elif (
            0 < filled < record.intent.quantity
            and record.status is OrderStatus.ACKNOWLEDGED
        ):
            record.transition(OrderStatus.PARTIAL, at=now)

    def _ltp(self, symbol: str) -> float:
        raw = self._client.get_ltp(trading_symbols=[symbol]) or {}
        if isinstance(raw, dict):
            # Groww shapes vary: {symbol: price} or nested.
            if symbol in raw:
                return float(raw[symbol])
            data = raw.get("ltp") or raw.get("data") or raw
            if isinstance(data, dict) and symbol in data:
                val = data[symbol]
                if isinstance(val, dict):
                    return float(val.get("ltp") or val.get("last_price") or 0)
                return float(val or 0)
        raise RuntimeError(f"no LTP for {symbol}")
