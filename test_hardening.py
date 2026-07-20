from __future__ import annotations

import csv
import json
from types import SimpleNamespace

import pandas as pd
import pytest

import execution
import main
import portfolio
import risk_manager
import strategy
from instance_lock import AlreadyRunningError, InstanceLock
from ledger import Ledger
from sheet_sync import retry, sync_trade_queue


class AccountClient:
    def __init__(self, equity=100_000, cash=100_000, buying_power=100_000):
        self.account = SimpleNamespace(
            equity=str(equity), cash=str(cash),
            non_marginable_buying_power=str(buying_power), portfolio_value=str(equity),
        )

    def get_account(self):
        return self.account


def order(order_id="1", status="new", side="buy", filled_qty=None, fill_price=None):
    return SimpleNamespace(
        id=order_id, client_order_id=f"client-{order_id}", symbol="AMD", side=side,
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
):
    ledger.record_submitted_order({
        "order_id": order_id, "client_order_id": f"client-{order_id}", "symbol": "AMD",
        "side": side, "status": status, "submitted_notional": 500,
        "filled_qty": filled_qty, "filled_avg_price": fill_price,
        "submitted_at": "2026-07-10T19:45:00+00:00",
        "completed_at": "2026-07-10T19:46:00+00:00" if status in {"filled", "canceled", "rejected", "expired"} else None,
        "signal_price": signal_price, "entry_atr": 4, "rsi": 25, "lower_band": 27,
        "realized_vol": 18, "market_direction": "BULL", "reason": "test",
    })


def scanner_for(ledger: Ledger):
    return main.Scanner(AccountClient(), object(), None, ledger)


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


def test_ledger_freezes_entry_exits(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.save_position("AMD", "o1", 100, 5, 4)
    position = ledger.get_position("AMD")
    assert position["stop_price"] == 92
    assert position["target_price"] == 112


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
