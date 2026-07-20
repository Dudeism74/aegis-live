"""Durable local state for Aegis orders, positions, and Sheet synchronization."""

from __future__ import annotations

import csv
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


FINAL_ORDER_STATUSES = {"filled", "canceled", "cancelled", "rejected", "expired"}
REDDIT_SHEET_HEADERS = [
    "Signal ID", "Observed At UTC", "Ticker", "Subreddits", "Sentiment Score",
    "Mention Count", "Weighted Mentions", "Baseline Mentions", "Mention Spike Ratio",
    "Positive Reddit Signal", "Price at Signal", "Technical Check Run", "Technical Pass",
    "Above KAMA", "RSI Oversold", "Intraday Bounce", "RSI 7", "RSI Lower Band",
    "KAMA", "ATR 14", "Market Direction", "SPY Realized Volatility", "Sample Posts",
    "Price 1h", "Return 1h", "1h Observed At UTC", "Price 24h", "Return 24h",
    "24h Observed At UTC", "Price 120h", "Return 120h", "120h Observed At UTC",
    "Last Updated UTC",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _yes_no(value: Any) -> str:
    if value is None:
        return ""
    return "YES" if bool(value) else "NO"


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
                CREATE TABLE IF NOT EXISTS reddit_observations (
                    observed_at TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    sentiment_score REAL NOT NULL,
                    mention_count INTEGER NOT NULL,
                    weighted_mentions REAL NOT NULL,
                    baseline_mentions REAL NOT NULL,
                    spike_ratio REAL NOT NULL,
                    PRIMARY KEY(observed_at, symbol)
                );
                CREATE INDEX IF NOT EXISTS idx_reddit_observations_symbol_time
                    ON reddit_observations(symbol, observed_at DESC);
                CREATE TABLE IF NOT EXISTS reddit_signals (
                    signal_id TEXT PRIMARY KEY,
                    observed_at TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    subreddits TEXT NOT NULL,
                    sentiment_score REAL NOT NULL,
                    mention_count INTEGER NOT NULL,
                    weighted_mentions REAL NOT NULL,
                    baseline_mentions REAL NOT NULL,
                    baseline_samples INTEGER NOT NULL,
                    spike_ratio REAL NOT NULL,
                    price_at_signal REAL NOT NULL,
                    technical_checked INTEGER NOT NULL,
                    technical_pass INTEGER,
                    above_kama INTEGER,
                    rsi_oversold INTEGER,
                    intraday_bounce INTEGER,
                    rsi REAL,
                    lower_band REAL,
                    kama REAL,
                    atr REAL,
                    market_direction TEXT,
                    realized_vol REAL,
                    sample_posts_json TEXT NOT NULL,
                    price_1h REAL,
                    return_1h REAL,
                    observed_1h_at TEXT,
                    price_24h REAL,
                    return_24h REAL,
                    observed_24h_at TEXT,
                    price_120h REAL,
                    return_120h REAL,
                    observed_120h_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_reddit_signals_symbol_time
                    ON reddit_signals(symbol, observed_at DESC);
                CREATE TABLE IF NOT EXISTS reddit_sheet_queue (
                    queue_id TEXT PRIMARY KEY,
                    signal_id TEXT NOT NULL,
                    row_json TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    synced_at TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_reddit_sheet_queue_unsynced
                    ON reddit_sheet_queue(synced_at, created_at);
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

    def record_reddit_observation(
        self, *, observed_at: str, symbol: str, sentiment_score: float,
        mention_count: int, weighted_mentions: float, baseline_mentions: float,
        spike_ratio: float,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO reddit_observations
                (observed_at, symbol, sentiment_score, mention_count, weighted_mentions,
                 baseline_mentions, spike_ratio)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observed_at, symbol.upper(), float(sentiment_score), int(mention_count),
                    float(weighted_mentions), float(baseline_mentions), float(spike_ratio),
                ),
            )

    def reddit_baseline_mentions(self, symbol: str, limit: int = 12) -> tuple[float, int]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT mention_count FROM reddit_observations WHERE symbol=? "
                "ORDER BY observed_at DESC LIMIT ?",
                (symbol.upper(), max(1, int(limit))),
            ).fetchall()
        if not rows:
            return 0.0, 0
        return sum(float(row[0]) for row in rows) / len(rows), len(rows)

    def has_recent_reddit_signal(self, symbol: str, observed_at: str, cooldown_minutes: int) -> bool:
        try:
            cutoff = datetime.fromisoformat(observed_at.replace("Z", "+00:00")) - timedelta(
                minutes=max(0, int(cooldown_minutes))
            )
        except ValueError:
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=max(0, int(cooldown_minutes)))
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM reddit_signals WHERE symbol=? AND observed_at>=? LIMIT 1",
                (symbol.upper(), cutoff.isoformat()),
            ).fetchone()
        return row is not None

    def _reddit_row(self, signal: sqlite3.Row) -> list[Any]:
        try:
            samples = json.loads(signal["sample_posts_json"] or "[]")
        except json.JSONDecodeError:
            samples = []
        return [
            signal["signal_id"], signal["observed_at"], signal["symbol"], signal["subreddits"],
            round(float(signal["sentiment_score"]), 4), int(signal["mention_count"]),
            round(float(signal["weighted_mentions"]), 4), round(float(signal["baseline_mentions"]), 4),
            round(float(signal["spike_ratio"]), 4), "YES", round(float(signal["price_at_signal"]), 4),
            _yes_no(signal["technical_checked"]), _yes_no(signal["technical_pass"]),
            _yes_no(signal["above_kama"]), _yes_no(signal["rsi_oversold"]),
            _yes_no(signal["intraday_bounce"]),
            "" if signal["rsi"] is None else round(float(signal["rsi"]), 4),
            "" if signal["lower_band"] is None else round(float(signal["lower_band"]), 4),
            "" if signal["kama"] is None else round(float(signal["kama"]), 4),
            "" if signal["atr"] is None else round(float(signal["atr"]), 4),
            signal["market_direction"] or "N/A",
            "" if signal["realized_vol"] is None else round(float(signal["realized_vol"]), 4),
            "\n".join(str(item) for item in samples),
            "" if signal["price_1h"] is None else round(float(signal["price_1h"]), 4),
            "" if signal["return_1h"] is None else round(float(signal["return_1h"]), 6),
            signal["observed_1h_at"] or "",
            "" if signal["price_24h"] is None else round(float(signal["price_24h"]), 4),
            "" if signal["return_24h"] is None else round(float(signal["return_24h"]), 6),
            signal["observed_24h_at"] or "",
            "" if signal["price_120h"] is None else round(float(signal["price_120h"]), 4),
            "" if signal["return_120h"] is None else round(float(signal["return_120h"]), 6),
            signal["observed_120h_at"] or "", signal["updated_at"],
        ]

    def _queue_reddit_signal(self, conn: sqlite3.Connection, signal_id: str) -> None:
        signal = conn.execute("SELECT * FROM reddit_signals WHERE signal_id=?", (signal_id,)).fetchone()
        if signal is None:
            raise KeyError(f"Unknown Reddit signal {signal_id}")
        conn.execute(
            "INSERT INTO reddit_sheet_queue(queue_id, signal_id, row_json, created_at) VALUES (?, ?, ?, ?)",
            (uuid.uuid4().hex, signal_id, json.dumps(self._reddit_row(signal), default=str), utc_now()),
        )

    def add_reddit_signal(self, values: dict[str, Any]) -> str:
        signal_id = str(values.get("signal_id") or uuid.uuid4().hex)
        now = utc_now()
        record = {
            "signal_id": signal_id,
            "observed_at": str(values["observed_at"]),
            "symbol": str(values["symbol"]).upper(),
            "subreddits": ", ".join(values.get("subreddits") or []),
            "sentiment_score": float(values["sentiment_score"]),
            "mention_count": int(values["mention_count"]),
            "weighted_mentions": float(values["weighted_mentions"]),
            "baseline_mentions": float(values.get("baseline_mentions") or 0),
            "baseline_samples": int(values.get("baseline_samples") or 0),
            "spike_ratio": float(values.get("spike_ratio") or 0),
            "price_at_signal": float(values["price_at_signal"]),
            "technical_checked": 1 if values.get("technical_checked") else 0,
            "technical_pass": None if values.get("technical_pass") is None else (1 if values.get("technical_pass") else 0),
            "above_kama": None if values.get("above_kama") is None else (1 if values.get("above_kama") else 0),
            "rsi_oversold": None if values.get("rsi_oversold") is None else (1 if values.get("rsi_oversold") else 0),
            "intraday_bounce": None if values.get("intraday_bounce") is None else (1 if values.get("intraday_bounce") else 0),
            "rsi": values.get("rsi"),
            "lower_band": values.get("lower_band"),
            "kama": values.get("kama"),
            "atr": values.get("atr"),
            "market_direction": values.get("market_direction"),
            "realized_vol": values.get("realized_vol"),
            "sample_posts_json": json.dumps(list(values.get("sample_posts") or [])),
            "created_at": now,
            "updated_at": now,
        }
        columns = ", ".join(record)
        placeholders = ", ".join(f":{name}" for name in record)
        with self.connect() as conn:
            conn.execute(f"INSERT INTO reddit_signals ({columns}) VALUES ({placeholders})", record)
            self._queue_reddit_signal(conn, signal_id)
        return signal_id

    def reddit_signals_needing_outcomes(self, limit: int = 200) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM reddit_signals WHERE return_1h IS NULL OR return_24h IS NULL "
                "OR return_120h IS NULL ORDER BY observed_at LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()

    def update_reddit_outcome(
        self, signal_id: str, horizon: str, price: float, observed_at: str,
    ) -> None:
        mapping = {
            "1h": ("price_1h", "return_1h", "observed_1h_at"),
            "24h": ("price_24h", "return_24h", "observed_24h_at"),
            "120h": ("price_120h", "return_120h", "observed_120h_at"),
        }
        if horizon not in mapping:
            raise ValueError(f"Unsupported Reddit outcome horizon: {horizon}")
        price_column, return_column, observed_column = mapping[horizon]
        with self.connect() as conn:
            signal = conn.execute(
                "SELECT price_at_signal FROM reddit_signals WHERE signal_id=?", (signal_id,)
            ).fetchone()
            if signal is None:
                raise KeyError(f"Unknown Reddit signal {signal_id}")
            initial = float(signal["price_at_signal"])
            realized_return = float(price) / initial - 1 if initial else 0.0
            conn.execute(
                f"UPDATE reddit_signals SET {price_column}=?, {return_column}=?, {observed_column}=?, "
                "updated_at=? WHERE signal_id=?",
                (float(price), realized_return, observed_at, utc_now(), signal_id),
            )
            self._queue_reddit_signal(conn, signal_id)

    def unsynced_reddit_rows(self, limit: int = 100) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM reddit_sheet_queue WHERE synced_at IS NULL "
                "ORDER BY created_at, rowid LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()

    def mark_reddit_synced(self, queue_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE reddit_sheet_queue SET synced_at=?, last_error=NULL WHERE queue_id=?",
                (utc_now(), queue_id),
            )

    def mark_reddit_sync_failed(self, queue_id: str, error: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE reddit_sheet_queue SET attempts=attempts+1, last_error=? WHERE queue_id=?",
                (str(error)[:1000], queue_id),
            )
