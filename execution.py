"""Alpaca order submission and reconciliation helpers."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest

from ledger import FINAL_ORDER_STATUSES, Ledger


def enum_value(value: Any) -> str:
    return str(getattr(value, "value", value)).lower()


def iso_value(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value else None)


def order_snapshot(order: Any) -> dict[str, Any]:
    return {
        "order_id": str(order.id),
        "client_order_id": str(order.client_order_id),
        "symbol": str(order.symbol).upper(),
        "side": enum_value(order.side),
        "status": enum_value(order.status),
        "submitted_qty": float(order.qty) if getattr(order, "qty", None) else None,
        "submitted_notional": float(order.notional) if getattr(order, "notional", None) else None,
        "filled_qty": float(order.filled_qty) if getattr(order, "filled_qty", None) else None,
        "filled_avg_price": (
            float(order.filled_avg_price) if getattr(order, "filled_avg_price", None) else None
        ),
        "submitted_at": iso_value(getattr(order, "submitted_at", None)),
        "completed_at": iso_value(
            getattr(order, "filled_at", None)
            or getattr(order, "canceled_at", None)
            or getattr(order, "expired_at", None)
        ),
    }


def client_order_id(symbol: str, side: str, now: datetime | None = None) -> str:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%S%fZ")
    return f"aegis-{side.lower()}-{symbol.lower()}-{stamp}"[:48]


def broker_has_open_order(trading_client: Any, symbol: str, side: str) -> bool:
    try:
        orders = trading_client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500))
    except Exception as exc:
        logging.error("Cannot verify broker open orders for %s %s: %s", symbol, side, exc)
        return True  # safe outcome: do not submit a possible duplicate
    return any(
        str(order.symbol).upper() == symbol.upper() and enum_value(order.side) == side.lower()
        for order in orders
    )


def submit_order(
    trading_client: Any, ledger: Ledger, *, symbol: str, side: str,
    signal_price: float, entry_atr: float | None, rsi: float | None,
    lower_band: float | None, realized_vol: float | None,
    market_direction: str, reason: str, qty: float | None = None,
    notional: float | None = None, order_role: str = "strategy",
) -> Any | None:
    if ledger.has_open_order(symbol, side) or broker_has_open_order(trading_client, symbol, side):
        logging.warning("Duplicate %s order blocked for %s", side.upper(), symbol)
        return None
    cid = client_order_id(symbol, side)
    request = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        notional=notional,
        side=OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
        client_order_id=cid,
    )
    started = time.monotonic()
    response = trading_client.submit_order(order_data=request)
    latency_ms = round((time.monotonic() - started) * 1000, 2)
    data = order_snapshot(response)
    data.update({
        "signal_price": signal_price,
        "entry_atr": entry_atr,
        "rsi": rsi,
        "lower_band": lower_band,
        "realized_vol": realized_vol,
        "market_direction": market_direction,
        "reason": reason,
        "order_role": order_role,
        "latency_ms": latency_ms,
    })
    ledger.record_submitted_order(data)
    return response


def refresh_order(trading_client: Any, ledger: Ledger, order_id: str):
    order = trading_client.get_order_by_id(str(order_id))
    data = order_snapshot(order)
    ledger.update_order(
        str(order_id), status=data["status"], filled_qty=data["filled_qty"],
        filled_avg_price=data["filled_avg_price"], submitted_at=data["submitted_at"],
        completed_at=data["completed_at"],
    )
    return ledger.get_order(str(order_id))


def wait_for_final_order(
    trading_client: Any, ledger: Ledger, order_id: str,
    timeout_seconds: float = 30, poll_seconds: float = 1,
):
    deadline = time.monotonic() + timeout_seconds
    current = ledger.get_order(order_id)
    while time.monotonic() < deadline:
        current = refresh_order(trading_client, ledger, order_id)
        if current and current["status"] in FINAL_ORDER_STATUSES:
            return current
        time.sleep(poll_seconds)
    logging.warning("Order %s still pending after %.1f seconds; it will be reconciled later", order_id, timeout_seconds)
    return current


def reconcile_pending_orders(
    trading_client: Any, ledger: Ledger, on_final: Callable[[Any], None],
) -> None:
    for stored in ledger.pending_orders():
        try:
            refreshed = refresh_order(trading_client, ledger, stored["order_id"])
            if refreshed and refreshed["status"] in FINAL_ORDER_STATUSES:
                on_final(refreshed)
        except Exception as exc:
            logging.error("Failed to reconcile order %s: %s", stored["order_id"], exc)
