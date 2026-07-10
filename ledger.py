"""Durable local state for Aegis orders, positions, and Sheet synchronization."""

from __future__ import annotations

import csv
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


FINAL_ORDER_STATUSES = {"filled", "canceled", "cancelled", "rejected", "expired"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Ledger:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    client_order_id TEXT NOT NULL UNIQUE,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    status TEXT NOT NULL,
                    submitted_qty REAL,
                    submitted_notional REAL,
                    filled_qty REAL,
                    filled_avg_price REAL,
                    submitted_at TEXT,
                    completed_at TEXT,
                    signal_price REAL,
                    entry_atr REAL,
                    rsi REAL,
                    lower_band REAL,
                    realized_vol REAL,
                    market_direction TEXT,
                    reason TEXT,
                    latency_ms REAL,
                    logged_event_id TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_orders_pending
                    ON orders(status, symbol, side);
                CREATE TABLE IF NOT EXISTS positions (
                    symbol TEXT PRIMARY KEY,
                    entry_order_id TEXT,
                    entry_price REAL NOT NULL,
                    entry_qty REAL NOT NULL,
                    entry_atr REAL NOT NULL,
                    stop_price REAL NOT NULL,
                    target_price REAL NOT NULL,
                    opened_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trade_events (
                    event_id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    row_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sheet_queue (
                    event_id TEXT PRIMARY KEY,
                    row_json TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    synced_at TEXT,
                    last_error TEXT
                );
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

    def get_metadata(self, key: str, default: str | None = None) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else default

    def set_metadata(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO metadata(key,value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def record_submitted_order(self, order: dict[str, Any]) -> None:
        values = {
            "order_id": str(order["order_id"]),
            "client_order_id": str(order["client_order_id"]),
            "symbol": str(order["symbol"]).upper(),
            "side": str(order["side"]).lower(),
            "status": str(order.get("status", "new")).lower(),
            "submitted_qty": order.get("submitted_qty"),
            "submitted_notional": order.get("submitted_notional"),
            "filled_qty": order.get("filled_qty"),
            "filled_avg_price": order.get("filled_avg_price"),
            "submitted_at": order.get("submitted_at") or utc_now(),
            "completed_at": order.get("completed_at"),
            "signal_price": order.get("signal_price"),
            "entry_atr": order.get("entry_atr"),
            "rsi": order.get("rsi"),
            "lower_band": order.get("lower_band"),
            "realized_vol": order.get("realized_vol"),
            "market_direction": order.get("market_direction"),
            "reason": order.get("reason"),
            "latency_ms": order.get("latency_ms"),
            "logged_event_id": None,
            "updated_at": utc_now(),
        }
        columns = ", ".join(values)
        placeholders = ", ".join(f":{key}" for key in values)
        updates = ", ".join(
            f"{key}=excluded.{key}" for key in values if key != "order_id"
        )
        with self.connect() as conn:
            conn.execute(
                f"INSERT INTO orders ({columns}) VALUES ({placeholders}) "
                f"ON CONFLICT(order_id) DO UPDATE SET {updates}",
                values,
            )

    def update_order(self, order_id: str, **fields: Any) -> None:
        allowed = {
            "status", "filled_qty", "filled_avg_price", "submitted_at",
            "completed_at", "logged_event_id",
        }
        clean = {key: value for key, value in fields.items() if key in allowed}
        if not clean:
            return
        clean["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = :{key}" for key in clean)
        clean["order_id"] = str(order_id)
        with self.connect() as conn:
            conn.execute(f"UPDATE orders SET {assignments} WHERE order_id = :order_id", clean)

    def get_order(self, order_id: str):
        with self.connect() as conn:
            return conn.execute("SELECT * FROM orders WHERE order_id = ?", (str(order_id),)).fetchone()

    def pending_orders(self) -> list[sqlite3.Row]:
        marks = ",".join("?" for _ in FINAL_ORDER_STATUSES)
        with self.connect() as conn:
            return conn.execute(
                f"SELECT * FROM orders WHERE lower(status) NOT IN ({marks}) ORDER BY submitted_at",
                tuple(FINAL_ORDER_STATUSES),
            ).fetchall()

    def has_open_order(self, symbol: str, side: str) -> bool:
        marks = ",".join("?" for _ in FINAL_ORDER_STATUSES)
        params = [symbol.upper(), side.lower(), *FINAL_ORDER_STATUSES]
        with self.connect() as conn:
            row = conn.execute(
                f"SELECT 1 FROM orders WHERE symbol=? AND side=? "
                f"AND lower(status) NOT IN ({marks}) LIMIT 1",
                params,
            ).fetchone()
        return row is not None

    def save_position(
        self, symbol: str, entry_order_id: str | None, entry_price: float,
        entry_qty: float, entry_atr: float, opened_at: str | None = None,
    ) -> None:
        stop_price = entry_price - (2.0 * entry_atr)
        target_price = entry_price + (3.0 * entry_atr)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO positions VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    entry_order_id=excluded.entry_order_id,
                    entry_price=excluded.entry_price,
                    entry_qty=excluded.entry_qty,
                    entry_atr=excluded.entry_atr,
                    stop_price=excluded.stop_price,
                    target_price=excluded.target_price,
                    opened_at=excluded.opened_at
                """,
                (
                    symbol.upper(), entry_order_id, entry_price, entry_qty, entry_atr,
                    stop_price, target_price, opened_at or utc_now(),
                ),
            )

    def get_position(self, symbol: str):
        with self.connect() as conn:
            return conn.execute("SELECT * FROM positions WHERE symbol=?", (symbol.upper(),)).fetchone()

    def close_position(self, symbol: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM positions WHERE symbol=?", (symbol.upper(),))

    def add_trade_event(self, order_id: str, row: list[Any]) -> str:
        existing = self.get_order(order_id)
        if existing and existing["logged_event_id"]:
            return str(existing["logged_event_id"])
        event_id = uuid.uuid4().hex
        row[-1] = event_id
        payload = json.dumps(row, default=str)
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO trade_events VALUES (?, ?, ?, ?)",
                (event_id, str(order_id), utc_now(), payload),
            )
            conn.execute(
                "INSERT INTO sheet_queue(event_id, row_json) VALUES (?, ?)",
                (event_id, payload),
            )
            conn.execute(
                "UPDATE orders SET logged_event_id=?, updated_at=? WHERE order_id=?",
                (event_id, utc_now(), str(order_id)),
            )
        return event_id

    def unsynced_rows(self, limit: int = 100) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM sheet_queue WHERE synced_at IS NULL ORDER BY rowid LIMIT ?",
                (limit,),
            ).fetchall()

    def mark_synced(self, event_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE sheet_queue SET synced_at=?, last_error=NULL WHERE event_id=?",
                (utc_now(), event_id),
            )

    def mark_sync_failed(self, event_id: str, error: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE sheet_queue SET attempts=attempts+1, last_error=? WHERE event_id=?",
                (error[:1000], event_id),
            )

    def count_buys_on(self, date_prefix: str) -> int:
        with self.connect() as conn:
            return int(conn.execute(
                "SELECT COUNT(*) FROM orders WHERE side='buy' AND status='filled' "
                "AND completed_at LIKE ?",
                (f"{date_prefix}%",),
            ).fetchone()[0])

    def bootstrap_position_from_csv(self, symbol: str, csv_path: str | Path) -> bool:
        """Load the newest unmatched BUY's frozen exits for a pre-existing position."""
        path = Path(csv_path)
        if not path.exists():
            return False
        latest: list[str] | None = None
        with path.open(newline="") as handle:
            for row in csv.reader(handle):
                if len(row) < 8 or row[1].strip().upper() != symbol.upper():
                    continue
                if row[2].strip().upper() == "SELL":
                    latest = None
                elif row[2].strip().upper() == "BUY":
                    latest = row
        if not latest:
            return False
        try:
            entry_price, atr, qty = float(latest[3]), float(latest[4]), float(latest[7])
        except (ValueError, TypeError):
            return False
        self.save_position(symbol, None, entry_price, qty, atr, latest[0])
        return True
