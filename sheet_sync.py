"""Bounded, retryable Google Sheets synchronization."""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

from ledger import REDDIT_SHEET_HEADERS, Ledger


REDDIT_SHEET_NAME = "Reddit Sensor"
REDDIT_SHEET_END_COLUMN = "AG"
REPORT_CARD_SHEET_NAME = "Report Card"
REPORT_CARD_TITLE = "Aegis Paper Trading Report Card"
REPORT_CARD_VERSION = "AEGIS_REPORT_CARD_V1"


def build_report_card_rows(
    strategy_capital: float = 3600,
    hedge_symbol: str = "PSQ",
) -> list[list[Any]]:
    """Return the Sheet formulas and grading rubric for paper evaluation."""
    capital = float(strategy_capital)
    if capital <= 0:
        raise ValueError("strategy_capital must be positive")
    capital_formula = f"{capital:.12g}"
    capital_label = f"${capital:,.0f}"
    symbol = str(hedge_symbol).strip().upper().replace('"', '""')
    strategy_filters = (
        'Sheet1!C$2:C,"SELL",Sheet1!AH$2:AH,"<>",'
        f'Sheet1!B$2:B,"<>{symbol}"'
    )
    gross_profit = (
        f"SUMIFS(Sheet1!AG$2:AG,{strategy_filters},"
        'Sheet1!AG$2:AG,">0")'
    )
    gross_loss = (
        f"ABS(SUMIFS(Sheet1!AG$2:AG,{strategy_filters},"
        'Sheet1!AG$2:AG,"<0"))'
    )
    return [
        [REPORT_CARD_TITLE, "", "", "", "", ""],
        [
            f"Confirmed ledger exits only | Strategy capital: {capital_label} | "
            f"{symbol}: hedge",
            "", "", "", "", "",
        ],
        [
            "Grades begin at 30 strategy exits. Paper readiness requires 50 "
            "exits and every graded metric at C or better.",
            "", "", "", "", "",
        ],
        [
            "Metric", "Current", "Grade", "C Standard", "A Standard",
            "Interpretation",
        ],
        [
            "Completed Strategy Exits",
            f"=COUNTIFS({strategy_filters})",
            '=IF(B5<30,"N/A",IF(B5<50,"TESTING","PASS"))',
            30,
            50,
            "Sample-size gate; confirmed strategy SELL events with a ledger ID",
        ],
        [
            "Win Rate",
            f'=IFERROR(COUNTIFS({strategy_filters},Sheet1!AG$2:AG,">0")/B5,0)',
            '=IF(B$5<30,"N/A",IF(B6>=0.6,"A",IF(B6>=0.55,"B",'
            'IF(B6>=0.5,"C",IF(B6>=0.45,"D","F")))))',
            0.50,
            0.60,
            "Winning strategy exits divided by completed strategy exits",
        ],
        [
            "Expectancy",
            '=IFERROR(AVERAGE(FILTER(Sheet1!AG$2:AG/'
            '(2*Sheet1!R$2:R*Sheet1!H$2:H),Sheet1!C$2:C="SELL",'
            'Sheet1!AH$2:AH<>"",Sheet1!B$2:B<>"'
            f'{symbol}",Sheet1!R$2:R>0,Sheet1!H$2:H>0)),0)',
            '=IF(B$5<30,"N/A",IF(B7>=0.3,"A",IF(B7>=0.2,"B",'
            'IF(B7>=0.1,"C",IF(B7>0,"D","F")))))',
            0.10,
            0.30,
            "Average realized P&L divided by entry risk (2 x ATR x shares)",
        ],
        [
            "Profit Factor",
            f"=LET(gp,{gross_profit},gl,{gross_loss},"
            "IF(B5=0,0,IF(gl=0,IF(gp>0,99,0),gp/gl)))",
            '=IF(B$5<30,"N/A",IF(B8>=1.75,"A",IF(B8>=1.5,"B",'
            'IF(B8>=1.25,"C",IF(B8>=1,"D","F")))))',
            1.25,
            1.75,
            "Gross strategy profit divided by gross strategy loss",
        ],
        [
            "Maximum Drawdown",
            '=IFERROR(LET(pnl,FILTER(Sheet1!AG$2:AG,'
            'Sheet1!C$2:C="SELL",Sheet1!AH$2:AH<>""),'
            f"equity,{capital_formula}+SCAN(0,pnl,LAMBDA(total,item,total+item)),"
            f"peaks,SCAN({capital_formula},equity,"
            "LAMBDA(peak,item,MAX(peak,item))),"
            "MAX((peaks-equity)/peaks)),0)",
            '=IF(B$5<30,"N/A",IF(B9<=0.05,"A",IF(B9<=0.08,"B",'
            'IF(B9<=0.12,"C",IF(B9<=0.15,"D","F")))))',
            0.12,
            0.05,
            "Largest realized system equity decline, including closed "
            f"{symbol} hedges",
        ],
        [
            "Hedge Cost",
            '=MAX(0,-SUMIFS(Sheet1!AG$2:AG,Sheet1!B$2:B,"'
            f'{symbol}",Sheet1!C$2:C,"SELL",Sheet1!AH$2:AH,"<>"))/'
            f"{capital_formula}",
            '=IF(B$5<30,"N/A",IF(B10<=0.005,"A",IF(B10<=0.01,"B",'
            'IF(B10<=0.02,"C",IF(B10<=0.03,"D","F")))))',
            0.02,
            0.005,
            f"Net realized {symbol} loss / {capital_label}; {symbol} gains "
            "count as zero cost",
        ],
        [
            "Overall Readiness",
            '=IF(B5<30,"INSUFFICIENT DATA",IF(B5<50,"KEEP TESTING",'
            'IF(OR(COUNTIF(C6:C10,"D")>0,COUNTIF(C6:C10,"F")>0),'
            '"PAPER FAIL","PAPER PASS")))',
            '=IF(B5<30,"N/A",IF(COUNTIF(C6:C10,"F"),"F",'
            'IF(COUNTIF(C6:C10,"D"),"D",IF(COUNTIF(C6:C10,"C"),"C",'
            'IF(COUNTIF(C6:C10,"B"),"B","A")))))',
            "All metrics C or better",
            "All metrics A",
            "Paper-trading evidence gate only; this does not authorize live "
            "trading",
        ],
        ["", "", "", "", "", ""],
        ["Supporting Data", "", "", "", "", ""],
        [
            "Strategy Realized P&L",
            f"=SUMIFS(Sheet1!AG$2:AG,{strategy_filters})",
            "", "", "", "Confirmed closed strategy trades",
        ],
        [
            "Hedge Realized P&L",
            '=SUMIFS(Sheet1!AG$2:AG,Sheet1!B$2:B,"'
            f'{symbol}",Sheet1!C$2:C,"SELL",Sheet1!AH$2:AH,"<>")',
            "", "", "", f"Confirmed closed {symbol} hedge trades",
        ],
        [
            "System Realized P&L",
            '=SUMIFS(Sheet1!AG$2:AG,Sheet1!C$2:C,"SELL",'
            'Sheet1!AH$2:AH,"<>")',
            "", "", "", "Strategy plus hedge realized P&L",
        ],
        [
            "Confirmed Hedge Exits",
            '=COUNTIFS(Sheet1!B$2:B,"'
            f'{symbol}",Sheet1!C$2:C,"SELL",Sheet1!AH$2:AH,"<>")',
            "", "", "", f"Completed {symbol} SELL events",
        ],
    ]


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


def ensure_report_card(
    gc: Any,
    strategy_capital: float = 3600,
    hedge_symbol: str = "PSQ",
) -> bool:
    """Create or repair the formula-driven paper evaluation tab once per run."""
    if gc is None:
        return False
    try:
        workbook = retry(lambda: gc.open("Aegis Trading Log"))
        created = False
        try:
            sheet = retry(lambda: workbook.worksheet(REPORT_CARD_SHEET_NAME))
        except Exception:
            sheet = retry(
                lambda: workbook.add_worksheet(
                    title=REPORT_CARD_SHEET_NAME,
                    rows=30,
                    cols=6,
                )
            )
            created = True

        marker = retry(lambda: sheet.acell("F30").value)
        if marker == REPORT_CARD_VERSION:
            return True

        rows = build_report_card_rows(strategy_capital, hedge_symbol)
        retry(
            lambda: sheet.update(
                values=rows,
                range_name="A1:F17",
                value_input_option="USER_ENTERED",
            )
        )
        if created:
            for cell_range in ("A1:F1", "A2:F2", "A3:F3", "A13:F13"):
                retry(lambda cell_range=cell_range: sheet.merge_cells(cell_range))
            retry(lambda: sheet.freeze(rows=4))
            retry(
                lambda: sheet.batch_format([
                    {
                        "range": "A1:F1",
                        "format": {
                            "backgroundColor": {
                                "red": 0.8117647,
                                "green": 0.8862745,
                                "blue": 0.9529412,
                            },
                            "horizontalAlignment": "CENTER",
                            "textFormat": {"bold": True, "fontSize": 16},
                        },
                    },
                    {
                        "range": "A4:F4",
                        "format": {
                            "backgroundColor": {
                                "red": 0.25882354,
                                "green": 0.52156866,
                                "blue": 0.95686275,
                            },
                            "horizontalAlignment": "CENTER",
                            "textFormat": {
                                "bold": True,
                                "foregroundColor": {
                                    "red": 1,
                                    "green": 1,
                                    "blue": 1,
                                },
                            },
                        },
                    },
                    {
                        "range": "A13:F13",
                        "format": {
                            "backgroundColor": {
                                "red": 0.8117647,
                                "green": 0.8862745,
                                "blue": 0.9529412,
                            },
                            "textFormat": {"bold": True},
                        },
                    },
                    {
                        "range": "A1:F17",
                        "format": {
                            "verticalAlignment": "MIDDLE",
                            "wrapStrategy": "WRAP",
                        },
                    },
                ])
            )
        retry(lambda: sheet.update_acell("F30", REPORT_CARD_VERSION))
        return True
    except Exception as exc:
        logging.error("Could not prepare Report Card; trading continues: %s", exc)
        return False


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
