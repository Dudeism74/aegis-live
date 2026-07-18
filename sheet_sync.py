"""Bounded, retryable Google Sheets synchronization."""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

from ledger import REDDIT_SHEET_HEADERS, Ledger


REDDIT_SHEET_NAME = "Reddit Sensor"
REDDIT_SHEET_END_COLUMN = "AG"


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


def _reddit_worksheet(gc: Any) -> Any:
    workbook = retry(lambda: gc.open("Aegis Trading Log"))
    try:
        sheet = retry(lambda: workbook.worksheet(REDDIT_SHEET_NAME))
    except Exception:
        sheet = retry(
            lambda: workbook.add_worksheet(
                title=REDDIT_SHEET_NAME,
                rows=2000,
                cols=len(REDDIT_SHEET_HEADERS),
            )
        )
    header = retry(lambda: sheet.row_values(1))
    if not header:
        retry(lambda: sheet.append_row(REDDIT_SHEET_HEADERS))
    elif header[: len(REDDIT_SHEET_HEADERS)] != REDDIT_SHEET_HEADERS:
        logging.error(
            "%s header does not match Aegis schema; Reddit rows remain queued",
            REDDIT_SHEET_NAME,
        )
        raise RuntimeError(f"{REDDIT_SHEET_NAME} header mismatch")
    return sheet


def sync_reddit_queue(gc: Any, ledger: Ledger) -> int:
    """Upsert the newest durable revision for every Reddit signal."""
    if gc is None:
        return 0
    try:
        sheet = _reddit_worksheet(gc)
        existing_ids = retry(lambda: sheet.col_values(1))
    except Exception as exc:
        logging.error("Could not open Reddit Sensor Sheet; queued rows remain local: %s", exc)
        return 0

    row_by_signal = {
        signal_id: row_number
        for row_number, signal_id in enumerate(existing_ids, start=1)
        if row_number > 1 and signal_id
    }
    synced = 0
    for item in ledger.unsynced_reddit_rows():
        row = json.loads(item["row_json"])
        signal_id = str(item["signal_id"])
        try:
            if signal_id in row_by_signal:
                row_number = row_by_signal[signal_id]
                cell_range = f"A{row_number}:{REDDIT_SHEET_END_COLUMN}{row_number}"
                retry(lambda cell_range=cell_range, row=row: sheet.update(cell_range, [row]))
            else:
                retry(lambda row=row: sheet.append_row(row))
                row_by_signal[signal_id] = len(row_by_signal) + 2
            ledger.mark_reddit_synced(item["queue_id"])
            synced += 1
        except Exception as exc:
            ledger.mark_reddit_sync_failed(item["queue_id"], str(exc))
            logging.error("Reddit Sheet revision %s remains queued: %s", item["queue_id"], exc)
    return synced
