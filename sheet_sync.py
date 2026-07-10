"""Bounded, retryable Google Sheets synchronization."""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

from ledger import Ledger


def retry(operation: Callable[[], Any], attempts: int = 3, delay_seconds: float = 1) -> Any:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except Exception as exc:
            last_error = exc
            logging.warning("Sheets attempt %d/%d failed: %s", attempt, attempts, exc)
            if attempt < attempts:
                time.sleep(delay_seconds)
    raise RuntimeError(f"Google Sheets failed after {attempts} attempts") from last_error


def sync_trade_queue(gc: Any, ledger: Ledger, ensure_ticker: Callable[[Any, str], None] | None = None) -> int:
    if gc is None:
        return 0
    try:
        sheet = retry(lambda: gc.open("Aegis Trading Log").worksheet("Sheet1"))
    except Exception as exc:
        logging.error("Could not open Sheet1; queued rows remain local: %s", exc)
        return 0
    synced = 0
    # AH contains the immutable Ledger Event ID. Reading it once makes retries
    # idempotent even if a process dies after append_row succeeds but before the
    # local queue is marked synchronized.
    try:
        existing_event_ids = set(retry(lambda: sheet.col_values(34)))
    except Exception as exc:
        logging.error("Could not read Sheet event IDs; queued rows remain local: %s", exc)
        return 0
    for item in ledger.unsynced_rows():
        row = json.loads(item["row_json"])
        if item["event_id"] in existing_event_ids:
            ledger.mark_synced(item["event_id"])
            synced += 1
            continue
        try:
            retry(lambda row=row: sheet.append_row(row))
            ledger.mark_synced(item["event_id"])
            existing_event_ids.add(item["event_id"])
            synced += 1
            if ensure_ticker:
                ensure_ticker(gc, str(row[1]))
        except Exception as exc:
            ledger.mark_sync_failed(item["event_id"], str(exc))
            logging.error("Sheet event %s remains queued: %s", item["event_id"], exc)
    return synced
