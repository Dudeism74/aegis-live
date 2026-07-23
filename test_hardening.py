from __future__ import annotations

import csv
import json
import sqlite3
from types import SimpleNamespace

import pandas as pd
import pytest

import execution
import hedge
import main
import portfolio
import risk_manager
import strategy
from instance_lock import AlreadyRunningError, InstanceLock
from ledger import Ledger
from sheet_sync import (
    REPORT_CARD_VERSION,
    build_report_card_rows,
    ensure_report_card,
    retry,
    sync_trade_queue,
)


class AccountClient:
    def __init__(self, equity=100_000, cash=100_000, buying_power=100_000):
        self.account = SimpleNamespace(
            equity=str(equity), cash=str(cash),
            non_marginable_buying_power=str(buying_power), portfolio_value=str(equity),
        )

    def get_account(self):
        return self.account


def order(
    order_id="1",
    status="new",
    side="buy",
    filled_qty=None,
    fill_price=None,
    symbol="AMD",
):
    return SimpleNamespace(
        id=order_id, client_order_id=f"client-{order_id}", symbol=symbol, side=side,
        status=status, qty=None, notional="500", filled_qty=filled_qty,
        filled_avg_price=fill_price, submitted_at=None, filled_at=None,
        canceled_at=None, expired_at=None,
    )


def submitted(
    ledger: Ledger,
    order_id="1",
    side="buy",
    status="new",
    signal_price=100,
    filled_qty=None,
    fill_price=None,
    symbol="AMD",
    order_role="strategy",
):
    ledger.record_submitted_order({
        "order_id": order_id, "client_order_id": f"client-{order_id}", "symbol": symbol,
        "side": side, "status": status, "submitted_notional": 500,
        "filled_qty": filled_qty, "filled_avg_price": fill_price,
        "submitted_at": "2026-07-10T19:45:00+00:00",
        "completed_at": "2026-07-10T19:46:00+00:00" if status in {"filled", "canceled", "rejected", "expired"} else None,
        "signal_price": signal_price, "entry_atr": 4, "rsi": 25, "lower_band": 27,
        "realized_vol": 18, "market_direction": "BULL", "reason": "test",
        "order_role": order_role,
    })


def scanner_for(ledger: Ledger):
    return main.Scanner(AccountClient(), object(), None, ledger)


def active_hedge_config(**overrides):
    values = {
        "mode": "paper",
        "symbol": "PSQ",
        "moderate_qqq_return": -0.01,
        "severe_qqq_return": -0.015,
        "moderate_exposure_factor": 0.50,
        "hedge_ratio": 0.25,
        "minimum_rebalance_usd": 25.0,
        "rebalance_tolerance": 0.10,
    }
    values.update(overrides)
    return hedge.HedgeConfig(**values)


def queued_row(ledger: Ledger):
    return json.loads(ledger.unsynced_rows()[0]["row_json"])


def test_risk_position_sizing_uses_two_atr_stop():
    # $18 risk / $8 stop = 2.25 shares * $100.
    assert portfolio.calculate_position_size(AccountClient(), 100, 4) == 225


def test_position_sizing_respects_twenty_percent_cap():
    assert portfolio.calculate_position_size(AccountClient(), 100, 0.25) == 720


def test_position_sizing_can_explicitly_use_full_account_equity():
    assert portfolio.calculate_position_size(
        AccountClient(), 100, 4, strategy_capital=None
    ) == 6250


def test_position_sizing_respects_cash():
    assert portfolio.calculate_position_size(AccountClient(cash=100), 100, 4) == 100


def test_invalid_atr_blocks_position():
    assert portfolio.calculate_position_size(AccountClient(), 100, 0) == 0


def test_paper_mode_is_default(monkeypatch):
    monkeypatch.delenv("AEGIS_TRADING_MODE", raising=False)
    assert main.validate_trading_mode() is True


def test_live_mode_requires_explicit_matching_authorization(monkeypatch):
    monkeypatch.setenv("AEGIS_TRADING_MODE", "live")
    monkeypatch.delenv("AEGIS_LIVE_AUTHORIZED", raising=False)
    with pytest.raises(RuntimeError):
        main.validate_trading_mode()


def test_live_authorization_defaults_false(monkeypatch):
    monkeypatch.delenv("AEGIS_LIVE_AUTHORIZED", raising=False)
    assert main.env_bool("AEGIS_LIVE_AUTHORIZED") is False


def test_live_mode_requires_matching_account_even_when_authorized(monkeypatch):
    monkeypatch.setenv("AEGIS_TRADING_MODE", "live")
    monkeypatch.setenv("AEGIS_LIVE_AUTHORIZED", "true")
    monkeypatch.setenv("AEGIS_APPROVED_LIVE_ACCOUNT_ID", "approved-account")
    monkeypatch.setenv("APCA_ACCOUNT_ID", "different-account")
    with pytest.raises(RuntimeError):
        main.validate_trading_mode()


def test_active_hedge_mode_is_blocked_for_live_trading():
    with pytest.raises(RuntimeError):
        main.validate_hedge_mode(False, active_hedge_config())
    main.validate_hedge_mode(True, active_hedge_config())


def test_qqq_risk_classification_and_entry_scaling():
    config = active_hedge_config()
    assert config.classify(-0.009) == hedge.RISK_NORMAL
    assert config.classify(-0.010) == hedge.RISK_MODERATE
    assert config.classify(-0.015) == hedge.RISK_SEVERE
    assert config.classify(None) == hedge.RISK_UNKNOWN
    assert config.entry_exposure_factor(hedge.RISK_NORMAL) == 1.0
    assert config.entry_exposure_factor(hedge.RISK_MODERATE) == 0.5
    assert config.entry_exposure_factor(hedge.RISK_SEVERE) == 0.0
    assert config.entry_exposure_factor(hedge.RISK_UNKNOWN) == 0.0


def test_observe_mode_never_changes_entry_size():
    config = active_hedge_config(mode="observe")
    assert config.entry_exposure_factor(hedge.RISK_SEVERE) == 1.0
    assert config.entry_exposure_factor(hedge.RISK_UNKNOWN) == 1.0


def test_hedge_target_is_twenty_five_percent_with_hysteresis():
    config = active_hedge_config()
    assert config.target_notional(1000, hedge.RISK_SEVERE, False) == 250
    assert config.target_notional(1000, hedge.RISK_MODERATE, False) == 0
    assert config.target_notional(1000, hedge.RISK_MODERATE, True) == 250
    assert config.target_notional(1000, hedge.RISK_NORMAL, True) == 0
    assert not config.rebalance_required(230, 250)
    assert config.rebalance_required(200, 250)


def test_ledger_freezes_entry_exits(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.save_position("AMD", "o1", 100, 5, 4)
    position = ledger.get_position("AMD")
    assert position["stop_price"] == 92
    assert position["target_price"] == 112


def test_hedge_position_survives_restart(tmp_path):
    path = tmp_path / "ledger.db"
    first = Ledger(path)
    first.save_hedge_position("PSQ", "hedge-1", 25, 10)
    reopened = Ledger(path)
    position = reopened.get_hedge_position("PSQ")
    assert position["entry_order_id"] == "hedge-1"
    assert position["entry_price"] == 25
    assert position["entry_qty"] == 10


def test_existing_ledger_migrates_order_role_without_losing_orders(tmp_path):
    path = tmp_path / "ledger.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE orders (
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
            )
            """
        )
        conn.execute(
            "INSERT INTO orders "
            "(order_id,client_order_id,symbol,side,status,updated_at) "
            "VALUES ('old-1','old-client','AMD','buy','new','2026-07-10')"
        )
    ledger = Ledger(path)
    migrated = ledger.get_order("old-1")
    assert migrated["symbol"] == "AMD"
    assert migrated["order_role"] == "strategy"


def test_confirmed_hedge_buy_uses_separate_ledger_state(monkeypatch, tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    submitted(
        ledger, status="filled", filled_qty=10, fill_price=25, signal_price=24.9,
        symbol="PSQ", order_role="hedge",
    )
    monkeypatch.setattr(main, "write_trade_to_csv", lambda _row: None)
    scanner_for(ledger).finalize_order(ledger.get_order("1"))
    hedge_position = ledger.get_hedge_position("PSQ")
    assert hedge_position["entry_price"] == 25
    assert hedge_position["entry_qty"] == 10
    assert ledger.get_position("PSQ") is None
    row = queued_row(ledger)
    assert len(row) == 34
    assert row[1] == "PSQ"
    assert row[4:7] == [0.0, 0.0, 0.0]


def test_hedge_rebalance_buy_uses_weighted_average_entry(monkeypatch, tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.save_hedge_position("PSQ", "hedge-1", 25, 10)
    submitted(
        ledger, order_id="hedge-2", status="filled", filled_qty=5, fill_price=28,
        signal_price=27.9, symbol="PSQ", order_role="hedge",
    )
    monkeypatch.setattr(main, "write_trade_to_csv", lambda _row: None)
    scanner_for(ledger).finalize_order(ledger.get_order("hedge-2"))
    position = ledger.get_hedge_position("PSQ")
    assert position["entry_qty"] == 15
    assert position["entry_price"] == pytest.approx(26)


def test_hedge_sell_uses_stored_entry_and_confirmed_quantity(monkeypatch, tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.save_hedge_position(
        "PSQ", "hedge-1", 25, 10, "2026-07-09T19:45:00+00:00"
    )
    submitted(
        ledger, side="sell", status="filled", filled_qty=4, fill_price=27,
        signal_price=27.1, symbol="PSQ", order_role="hedge",
    )
    monkeypatch.setattr(main, "write_trade_to_csv", lambda _row: None)
    scanner_for(ledger).finalize_order(ledger.get_order("1"))
    row = queued_row(ledger)
    assert row[7] == 4
    assert row[13] == "8.00%"
    assert row[32] == 8.0
    assert ledger.get_hedge_position("PSQ")["entry_qty"] == 6


def test_trade_event_is_idempotent(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    submitted(ledger, status="filled")
    row = ["value", ""]
    first = ledger.add_trade_event("1", row)
    second = ledger.add_trade_event("1", ["other", ""])
    assert first == second
    assert len(ledger.unsynced_rows()) == 1


def test_pending_order_blocks_duplicate(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    submitted(ledger)
    assert ledger.has_open_order("AMD", "buy")
    ledger.update_order("1", status="filled")
    assert not ledger.has_open_order("AMD", "buy")


def test_pending_order_survives_new_ledger_instance(tmp_path):
    path = tmp_path / "ledger.db"
    submitted(Ledger(path))
    reopened = Ledger(path)
    assert reopened.has_open_order("AMD", "buy")
    assert [item["order_id"] for item in reopened.pending_orders()] == ["1"]


def test_bootstrap_uses_latest_unmatched_buy(tmp_path):
    csv_path = tmp_path / "trades.csv"
    with csv_path.open("w", newline="") as handle:
        csv.writer(handle).writerows([
            ["2026-01-01", "AMD", "BUY", 90, 2, 86, 96, 5],
            ["2026-01-02", "AMD", "SELL", 96, "", "", "", 5],
            ["2026-01-03", "AMD", "BUY", 100, 4, 92, 112, 3],
        ])
    ledger = Ledger(tmp_path / "ledger.db")
    assert ledger.bootstrap_position_from_csv("AMD", csv_path)
    assert ledger.get_position("AMD")["entry_price"] == 100


def test_client_order_ids_are_unique_and_bounded():
    first = execution.client_order_id("AMD", "buy")
    second = execution.client_order_id("AMD", "buy")
    assert first != second
    assert len(first) <= 48


def test_broker_open_order_is_duplicate():
    client = SimpleNamespace(get_orders=lambda _request: [order(side="buy")])
    assert execution.broker_has_open_order(client, "AMD", "buy")


def test_broker_open_order_blocks_submission(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    client = SimpleNamespace(
        get_orders=lambda _request: [order(side="buy")],
        submit_order=lambda **_kwargs: pytest.fail("duplicate broker order must block submission"),
    )
    response = execution.submit_order(
        client, ledger, symbol="AMD", side="buy", notional=500,
        signal_price=100, entry_atr=4, rsi=25, lower_band=27,
        realized_vol=18, market_direction="BULL", reason="test",
    )
    assert response is None


def test_local_open_order_blocks_submission(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    submitted(ledger)
    client = SimpleNamespace(
        get_orders=lambda _request: [],
        submit_order=lambda **_kwargs: pytest.fail("duplicate local order must block submission"),
    )
    response = execution.submit_order(
        client, ledger, symbol="AMD", side="buy", notional=500,
        signal_price=100, entry_atr=4, rsi=25, lower_band=27,
        realized_vol=18, market_direction="BULL", reason="test",
    )
    assert response is None


def test_reconcile_updates_delayed_fill(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    submitted(ledger)
    client = SimpleNamespace(get_order_by_id=lambda _order_id: order(status="filled", filled_qty="5", fill_price="101"))
    refreshed = execution.refresh_order(client, ledger, "1")
    assert refreshed["status"] == "filled"
    assert refreshed["filled_avg_price"] == 101


def test_repeated_reconciliation_creates_one_trade_event(monkeypatch, tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    submitted(ledger)
    client = AccountClient()
    client.get_order_by_id = lambda _order_id: order(
        status="filled", filled_qty="5", fill_price="101"
    )
    scanner = main.Scanner(client, object(), None, ledger)
    monkeypatch.setattr(main, "write_trade_to_csv", lambda _row: None)
    scanner.reconcile()
    scanner.reconcile()
    assert len(ledger.unsynced_rows()) == 1


@pytest.mark.parametrize("status", ["canceled", "rejected", "expired"])
def test_zero_fill_terminal_orders_do_not_create_trade_events(monkeypatch, tmp_path, status):
    ledger = Ledger(tmp_path / "ledger.db")
    submitted(ledger, status=status, filled_qty=0, fill_price=None)
    monkeypatch.setattr(main, "write_trade_to_csv", lambda _row: None)
    scanner_for(ledger).finalize_order(ledger.get_order("1"))
    assert ledger.unsynced_rows() == []
    assert ledger.get_position("AMD") is None


def test_nonfinal_order_does_not_create_trade_event(monkeypatch, tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    submitted(ledger, status="partially_filled", filled_qty=2, fill_price=100.5)
    monkeypatch.setattr(main, "write_trade_to_csv", lambda _row: None)
    scanner_for(ledger).finalize_order(ledger.get_order("1"))
    assert ledger.unsynced_rows() == []


def test_terminal_partial_fill_records_confirmed_quantity(monkeypatch, tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    submitted(ledger, status="canceled", filled_qty=2, fill_price=100.5)
    monkeypatch.setattr(main, "write_trade_to_csv", lambda _row: None)
    scanner_for(ledger).finalize_order(ledger.get_order("1"))
    assert ledger.get_position("AMD")["entry_qty"] == 2
    assert queued_row(ledger)[7] == 2


@pytest.mark.parametrize(
    ("side", "fill_price", "expected"),
    [
        ("buy", 100.10, 0.001),
        ("buy", 99.90, -0.001),
        ("sell", 100.10, 0.001),
        ("sell", 99.90, -0.001),
    ],
)
def test_slippage_percentage_is_decimal_ratio(monkeypatch, tmp_path, side, fill_price, expected):
    ledger = Ledger(tmp_path / "ledger.db")
    if side == "sell":
        ledger.save_position("AMD", "entry", 100, 1, 4, "2026-07-09T19:45:00+00:00")
    submitted(ledger, side=side, status="filled", filled_qty=1, fill_price=fill_price)
    monkeypatch.setattr(main, "write_trade_to_csv", lambda _row: None)
    scanner_for(ledger).finalize_order(ledger.get_order("1"))
    assert queued_row(ledger)[20] == pytest.approx(expected)


def test_zero_signal_price_has_zero_slippage_ratio(monkeypatch, tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    submitted(ledger, status="filled", signal_price=0, filled_qty=1, fill_price=100.10)
    monkeypatch.setattr(main, "write_trade_to_csv", lambda _row: None)
    scanner_for(ledger).finalize_order(ledger.get_order("1"))
    assert queued_row(ledger)[20] == 0.0


def test_google_sheet_row_contains_decimal_slippage_ratio(monkeypatch, tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    submitted(ledger, status="filled", filled_qty=1, fill_price=100.10)
    monkeypatch.setattr(main, "write_trade_to_csv", lambda _row: None)
    scanner_for(ledger).finalize_order(ledger.get_order("1"))
    appended = []
    sheet = SimpleNamespace(
        col_values=lambda _column: [],
        append_row=lambda row: appended.append(row),
    )
    gc = SimpleNamespace(open=lambda _name: SimpleNamespace(worksheet=lambda _tab: sheet))
    assert sync_trade_queue(gc, ledger) == 1
    assert appended[0][20] == pytest.approx(0.001)


def test_sell_uses_stored_entry_for_return_and_pnl(monkeypatch, tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.save_position("AMD", "entry", 100, 5, 4, "2026-07-09T19:45:00+00:00")
    submitted(ledger, side="sell", status="filled", filled_qty=5, fill_price=110)
    monkeypatch.setattr(main, "write_trade_to_csv", lambda _row: None)
    scanner_for(ledger).finalize_order(ledger.get_order("1"))
    row = queued_row(ledger)
    assert row[13] == "10.00%"
    assert row[32] == 50.0
    assert ledger.get_position("AMD") is None


def test_same_day_position_remains_eligible_for_protective_exit(monkeypatch, tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.save_position("AMD", "entry", 100, 5, 4, "2026-07-10T19:45:00+00:00")
    position = SimpleNamespace(symbol="AMD", qty="5", avg_entry_price="100")
    trading = AccountClient()
    trading.get_all_positions = lambda: [position]
    data = SimpleNamespace(
        get_stock_snapshot=lambda _request: {
            "AMD": SimpleNamespace(latest_trade=SimpleNamespace(price=91.0))
        }
    )
    calls = []
    monkeypatch.setattr(main, "submit_order", lambda *_args, **kwargs: calls.append(kwargs) or SimpleNamespace(id="sell-1"))
    monkeypatch.setattr(main, "wait_for_final_order", lambda *_args, **_kwargs: None)
    main.Scanner(trading, data, None, ledger).manage_exits()
    assert calls[0]["side"] == "sell"
    assert calls[0]["qty"] == 5.0


def test_existing_position_keeps_frozen_atr_exits(monkeypatch, tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.save_position("AMD", "entry", 100, 5, 4, "2026-07-09T19:45:00+00:00")
    position = SimpleNamespace(symbol="AMD", qty="5", avg_entry_price="100")
    trading = AccountClient()
    trading.get_all_positions = lambda: [position]
    data = SimpleNamespace(
        get_stock_snapshot=lambda _request: {
            "AMD": SimpleNamespace(latest_trade=SimpleNamespace(price=100.0))
        }
    )
    monkeypatch.setattr(
        main.strategy, "check_rsi_buy_signal",
        lambda *_args: pytest.fail("stored position must not recalculate entry ATR"),
    )
    main.Scanner(trading, data, None, ledger).manage_exits()
    stored = ledger.get_position("AMD")
    assert stored["entry_atr"] == 4
    assert stored["stop_price"] == 92
    assert stored["target_price"] == 112


def test_sheet_retry_is_bounded():
    calls = []
    def fail():
        calls.append(1)
        raise ConnectionError("offline")
    with pytest.raises(RuntimeError):
        retry(fail, attempts=3, delay_seconds=0)
    assert len(calls) == 3


def test_failed_sheet_sync_keeps_local_queue(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    submitted(ledger, status="filled")
    ledger.add_trade_event("1", ["date", "AMD", "BUY", ""])
    gc = SimpleNamespace(open=lambda _name: (_ for _ in ()).throw(ConnectionError("offline")))
    assert sync_trade_queue(gc, ledger) == 0
    assert len(ledger.unsynced_rows()) == 1


def test_sheet_sync_deduplicates_event_id_after_restart(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    submitted(ledger, status="filled")
    event_id = ledger.add_trade_event("1", ["date", "AMD", "BUY", ""])
    sheet = SimpleNamespace(
        col_values=lambda _column: [event_id],
        append_row=lambda _row: pytest.fail("existing event must not be appended twice"),
    )
    gc = SimpleNamespace(open=lambda _name: SimpleNamespace(worksheet=lambda _tab: sheet))
    assert sync_trade_queue(gc, ledger) == 1
    assert not ledger.unsynced_rows()


def test_repeated_sheet_sync_appends_event_once(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    submitted(ledger, status="filled")
    ledger.add_trade_event("1", ["date", "AMD", "BUY", ""])
    appended = []
    sheet = SimpleNamespace(
        col_values=lambda _column: [row[-1] for row in appended],
        append_row=lambda row: appended.append(row),
    )
    gc = SimpleNamespace(open=lambda _name: SimpleNamespace(worksheet=lambda _tab: sheet))
    assert sync_trade_queue(gc, ledger) == 1
    assert sync_trade_queue(gc, ledger) == 0
    assert len(appended) == 1


def test_report_card_grades_confirmed_strategy_and_hedge_results():
    rows = build_report_card_rows(3600, "PSQ")
    assert len(rows) == 17
    assert rows[4][1] == (
        '=COUNTIFS(Sheet1!C$2:C,"SELL",Sheet1!AH$2:AH,"<>",'
        'Sheet1!B$2:B,"<>PSQ")'
    )
    assert 'Sheet1!R$2:R*Sheet1!H$2:H' in rows[6][1]
    assert 'Sheet1!B$2:B,"PSQ"' in rows[9][1]
    assert "INSUFFICIENT DATA" in rows[10][1]
    assert "PAPER PASS" in rows[10][1]


def test_report_card_sheet_update_uses_formulas_and_version_marker():
    updates = []
    marker_updates = []
    sheet = SimpleNamespace(
        acell=lambda _cell: SimpleNamespace(value=""),
        update=lambda **kwargs: updates.append(kwargs),
        update_acell=lambda cell, value: marker_updates.append((cell, value)),
    )
    workbook = SimpleNamespace(worksheet=lambda _tab: sheet)
    gc = SimpleNamespace(open=lambda _name: workbook)
    assert ensure_report_card(gc, 3600, "PSQ")
    assert updates[0]["range_name"] == "A1:F17"
    assert updates[0]["value_input_option"] == "USER_ENTERED"
    assert updates[0]["values"][5][0] == "Win Rate"
    assert marker_updates == [("F30", REPORT_CARD_VERSION)]


def test_current_report_card_version_skips_rewrite():
    sheet = SimpleNamespace(
        acell=lambda _cell: SimpleNamespace(value=REPORT_CARD_VERSION),
        update=lambda **_kwargs: pytest.fail("current template must not be rewritten"),
    )
    workbook = SimpleNamespace(worksheet=lambda _tab: sheet)
    gc = SimpleNamespace(open=lambda _name: workbook)
    assert ensure_report_card(gc)


def test_market_day_returns_use_current_and_previous_snapshot_closes():
    snapshots = {
        "QQQ": SimpleNamespace(
            daily_bar=SimpleNamespace(close=98),
            previous_daily_bar=SimpleNamespace(close=100),
        ),
        "SPY": SimpleNamespace(
            daily_bar=SimpleNamespace(close=99),
            previous_daily_bar=SimpleNamespace(close=100),
        ),
    }
    data = SimpleNamespace(get_stock_snapshot=lambda _request: snapshots)
    returns = risk_manager.get_market_day_returns(data, retries=1)
    assert returns["QQQ"] == pytest.approx(-0.02)
    assert returns["SPY"] == pytest.approx(-0.01)


def test_missing_market_snapshot_fails_closed_without_fake_return(monkeypatch):
    calls = []

    def fail(_request):
        calls.append(1)
        raise ConnectionError("offline")

    monkeypatch.setattr(risk_manager.time, "sleep", lambda _seconds: None)
    data = SimpleNamespace(get_stock_snapshot=fail)
    assert risk_manager.get_market_day_returns(data, retries=3) is None
    assert len(calls) == 3


def test_severe_risk_buys_psq_at_twenty_five_percent_of_gross(
    monkeypatch, tmp_path
):
    ledger = Ledger(tmp_path / "ledger.db")
    trading = AccountClient()
    trading.get_all_positions = lambda: [
        SimpleNamespace(symbol="AMD", qty="10", market_value="1000")
    ]
    scanner = main.Scanner(
        trading, object(), None, ledger, active_hedge_config()
    )
    scanner.risk_state = hedge.RISK_SEVERE
    scanner.price = lambda _symbol: 25
    calls = []
    monkeypatch.setattr(
        main, "submit_order",
        lambda *_args, **kwargs: calls.append(kwargs) or SimpleNamespace(id="hedge-1"),
    )
    monkeypatch.setattr(main, "wait_for_final_order", lambda *_args, **_kwargs: None)
    scanner.manage_hedge()
    assert calls[0]["symbol"] == "PSQ"
    assert calls[0]["side"] == "buy"
    assert calls[0]["notional"] == 250
    assert calls[0]["order_role"] == "hedge"


def test_normal_risk_closes_confirmed_psq_hedge(monkeypatch, tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.save_hedge_position("PSQ", "hedge-entry", 25, 10)
    trading = AccountClient()
    trading.get_all_positions = lambda: [
        SimpleNamespace(symbol="AMD", qty="10", market_value="1000"),
        SimpleNamespace(symbol="PSQ", qty="10", market_value="260"),
    ]
    scanner = main.Scanner(
        trading, object(), None, ledger, active_hedge_config()
    )
    scanner.risk_state = hedge.RISK_NORMAL
    scanner.price = lambda _symbol: 26
    calls = []
    monkeypatch.setattr(
        main, "submit_order",
        lambda *_args, **kwargs: calls.append(kwargs) or SimpleNamespace(id="hedge-exit"),
    )
    monkeypatch.setattr(main, "wait_for_final_order", lambda *_args, **_kwargs: None)
    scanner.manage_hedge()
    assert calls[0]["side"] == "sell"
    assert calls[0]["qty"] == 10
    assert calls[0]["reason"] == "QQQ Risk Hedge Exit"


def test_existing_hedge_is_never_averaged_down(monkeypatch, tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.save_hedge_position("PSQ", "hedge-entry", 25, 8)
    trading = AccountClient()
    trading.get_all_positions = lambda: [
        SimpleNamespace(symbol="AMD", qty="10", market_value="1000"),
        SimpleNamespace(symbol="PSQ", qty="8", market_value="200"),
    ]
    scanner = main.Scanner(
        trading, object(), None, ledger, active_hedge_config()
    )
    scanner.risk_state = hedge.RISK_SEVERE
    monkeypatch.setattr(
        main, "submit_order",
        lambda *_args, **_kwargs: pytest.fail("an open hedge must not be averaged down"),
    )
    scanner.manage_hedge()


def test_unknown_risk_preserves_existing_hedge(monkeypatch, tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.save_hedge_position("PSQ", "hedge-entry", 25, 10)
    trading = AccountClient()
    trading.get_all_positions = lambda: [
        SimpleNamespace(symbol="AMD", qty="10", market_value="1000"),
        SimpleNamespace(symbol="PSQ", qty="10", market_value="260"),
    ]
    scanner = main.Scanner(
        trading, object(), None, ledger, active_hedge_config()
    )
    scanner.risk_state = hedge.RISK_UNKNOWN
    monkeypatch.setattr(
        main, "submit_order",
        lambda *_args, **_kwargs: pytest.fail("unknown risk must not change hedge"),
    )
    scanner.manage_hedge()


def test_psq_hedge_is_not_managed_by_atr_exit_logic(monkeypatch, tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.save_hedge_position("PSQ", "hedge-entry", 25, 10)
    trading = AccountClient()
    trading.get_all_positions = lambda: [
        SimpleNamespace(symbol="PSQ", qty="10", avg_entry_price="25")
    ]
    scanner = main.Scanner(
        trading, object(), None, ledger, active_hedge_config()
    )
    monkeypatch.setattr(
        scanner, "ensure_position_state",
        lambda _position: pytest.fail("PSQ must bypass ATR exit logic"),
    )
    scanner.manage_exits()


def test_severe_risk_blocks_entry_before_strategy_scan(monkeypatch, tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    scanner = main.Scanner(
        AccountClient(), object(), None, ledger, active_hedge_config()
    )
    scanner.risk_state = hedge.RISK_SEVERE
    now = main.datetime(2026, 7, 10, 15, 50, tzinfo=main.EASTERN)
    monkeypatch.setattr(risk_manager, "observe_vix_term_structure", lambda _data: None)
    monkeypatch.setattr(
        strategy, "check_rsi_buy_signal",
        lambda *_args: pytest.fail("severe risk must block the strategy scan"),
    )
    recap = scanner.scan_entries(now)
    assert recap["reason"] == "QQQ Severe Risk-Off"
    assert scanner.last_entry_date == now.date()


def test_moderate_risk_halves_otherwise_valid_entry_notional(
    monkeypatch, tmp_path
):
    ledger = Ledger(tmp_path / "ledger.db")
    trading = AccountClient()
    trading.get_all_positions = lambda: []
    scanner = main.Scanner(
        trading, object(), None, ledger, active_hedge_config()
    )
    scanner.risk_state = hedge.RISK_MODERATE
    scanner.market_returns = {"QQQ": -0.012}
    scanner.price = lambda _symbol: 100
    now = main.datetime(2026, 7, 10, 15, 50, tzinfo=main.EASTERN)
    monkeypatch.setattr(risk_manager, "observe_vix_term_structure", lambda _data: None)
    monkeypatch.setattr(
        strategy, "check_rsi_buy_signal",
        lambda _data, symbol: {
            "is_buy": True, "rsi_7": 20, "lower_band": 25, "atr_14": 4
        } if symbol == "AMD" else None,
    )
    monkeypatch.setattr(portfolio, "calculate_position_size", lambda *_args, **_kwargs: 200)
    calls = []
    monkeypatch.setattr(
        main, "submit_order",
        lambda *_args, **kwargs: calls.append(kwargs) or None,
    )
    scanner.scan_entries(now)
    assert calls[0]["notional"] == 100


def test_realized_vol_failure_returns_none(monkeypatch):
    monkeypatch.setattr(risk_manager, "_fetch_bars", lambda *_args, **_kwargs: pd.DataFrame({"close": [1] * 10}))
    assert risk_manager.get_spy_realized_volatility_20d(object()) is None


def test_term_structure_is_observe_only(monkeypatch):
    frame1 = pd.DataFrame({"close": [1, 1, 1, 1, 2]})
    frame2 = pd.DataFrame({"close": [1, 1, 1, 1, 1]})
    monkeypatch.setattr(
        risk_manager, "_fetch_bars", lambda _client, symbol, **_kwargs: frame1 if symbol == "VIXY" else frame2
    )
    result = risk_manager.observe_vix_term_structure(object())
    assert result["zscore"] > 0
    assert risk_manager.check_vix_kill_switch(object()) is False


def test_strategy_returns_indicator_payload_for_buy(monkeypatch):
    count = 40
    bars = pd.DataFrame({
        "open": [99.0] * count, "high": [102.0] * count,
        "low": [96.0] * count, "close": [100.0] * count,
    })
    client = SimpleNamespace(get_stock_bars=lambda _request: SimpleNamespace(df=bars))
    monkeypatch.setattr(strategy.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        strategy.ta.momentum, "KAMAIndicator",
        lambda **_kwargs: SimpleNamespace(kama=lambda: pd.Series([90.0] * count)),
    )
    monkeypatch.setattr(
        strategy.ta.momentum, "RSIIndicator",
        lambda **_kwargs: SimpleNamespace(rsi=lambda: pd.Series([40.0] * (count - 1) + [10.0])),
    )
    monkeypatch.setattr(
        strategy.ta.volatility, "AverageTrueRange",
        lambda **_kwargs: SimpleNamespace(average_true_range=lambda: pd.Series([4.0] * count)),
    )
    result = strategy.check_rsi_buy_signal(client, "AMD")
    assert result["is_buy"] is True
    assert result["atr_14"] == 4


def test_strategy_fails_safely_on_insufficient_bars(monkeypatch):
    bars = pd.DataFrame({"open": [1] * 3, "high": [1] * 3, "low": [1] * 3, "close": [1] * 3})
    client = SimpleNamespace(get_stock_bars=lambda _request: SimpleNamespace(df=bars))
    monkeypatch.setattr(strategy.time, "sleep", lambda _seconds: None)
    assert strategy.check_rsi_buy_signal(client, "AMD") is None


def test_entry_window_runs_once_per_day(monkeypatch, tmp_path):
    scanner = main.Scanner(AccountClient(), object(), None, Ledger(tmp_path / "ledger.db"))
    now = main.datetime(2026, 7, 10, 15, 50, tzinfo=main.EASTERN)
    assert scanner.entry_window_open(now)
    scanner.last_entry_date = now.date()
    assert not scanner.entry_window_open(now)


def test_entry_window_state_survives_restart(tmp_path):
    path = tmp_path / "ledger.db"
    first = Ledger(path)
    first.set_metadata("last_entry_date", "2026-07-10")
    scanner = main.Scanner(AccountClient(), object(), None, Ledger(path))
    now = main.datetime(2026, 7, 10, 15, 50, tzinfo=main.EASTERN)
    assert not scanner.entry_window_open(now)


@pytest.mark.skipif(main.os.name == "nt", reason="Linux flock behavior is verified on CI")
def test_instance_lock_blocks_concurrent_holder(tmp_path):
    path = tmp_path / ".aegis.lock"
    first = InstanceLock(path)
    second = InstanceLock(path)
    first.acquire()
    try:
        with pytest.raises(AlreadyRunningError):
            second.acquire()
    finally:
        first.release()


@pytest.mark.skipif(main.os.name == "nt", reason="Linux flock behavior is verified on CI")
def test_instance_lock_allows_sequential_runs_and_releases_on_exception(tmp_path):
    path = tmp_path / ".aegis.lock"
    with pytest.raises(RuntimeError):
        with InstanceLock(path):
            raise RuntimeError("simulated scanner failure")
    with InstanceLock(path):
        pass
    with InstanceLock(path):
        pass
