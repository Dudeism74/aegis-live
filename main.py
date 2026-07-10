"""Aegis paper-trading scanner with durable order and execution state."""

from __future__ import annotations

import csv
import logging
import os
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

import gspread
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockSnapshotRequest
from alpaca.trading.client import TradingClient
from dotenv import load_dotenv

import portfolio
import risk_manager
import strategy
from execution import reconcile_pending_orders, submit_order, wait_for_final_order
from instance_lock import AlreadyRunningError, InstanceLock
from ledger import FINAL_ORDER_STATUSES, Ledger
from sheet_sync import retry as sheets_retry
from sheet_sync import sync_trade_queue

try:
    import zoneinfo
except ImportError:
    from backports import zoneinfo  # type: ignore


BASE_DIR = Path(__file__).resolve().parent
TRADES_CSV = BASE_DIR / "Aegis Trading Log - Sheet1.csv"
LEDGER_DB = BASE_DIR / "aegis_ledger.sqlite3"
LOCK_FILE = BASE_DIR / ".aegis.lock"
TICKERS = [
    "TSLA", "NVDA", "AMD", "PLTR", "COIN", "MSTR", "SMCI", "CRWD",
    "SNOW", "SHOP", "ROKU", "MSFT", "META", "NFLX", "AMZN", "UBER", "DASH",
]

load_dotenv(BASE_DIR / ".env")
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
try:
    EASTERN = zoneinfo.ZoneInfo("America/New_York")
    logging.Formatter.converter = lambda *_args: datetime.now(EASTERN).timetuple()
except Exception:
    EASTERN = timezone.utc


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


def validate_trading_mode() -> bool:
    """Return Alpaca's paper flag. Live mode requires two explicit safeguards."""
    mode = os.getenv("AEGIS_TRADING_MODE", "paper").strip().lower()
    if mode not in {"paper", "live"}:
        raise RuntimeError("AEGIS_TRADING_MODE must be 'paper' or 'live'")
    if mode == "live":
        expected = os.getenv("AEGIS_APPROVED_LIVE_ACCOUNT_ID", "").strip()
        actual = os.getenv("APCA_ACCOUNT_ID", "").strip()
        if not env_bool("AEGIS_LIVE_AUTHORIZED") or not expected or actual != expected:
            raise RuntimeError(
                "Live trading blocked: authorization must be true and the approved account must match"
            )
        logging.critical("AEGIS IS STARTING IN LIVE TRADING MODE")
        return False
    logging.info("Aegis trading mode: PAPER")
    return True


def send_email(subject: str, body: str) -> None:
    sender, password, recipient = (
        os.getenv("SENDER_EMAIL"), os.getenv("SENDER_PASSWORD"), os.getenv("RECIPIENT_EMAIL")
    )
    if not sender or not password or not recipient:
        logging.warning("Email credentials are incomplete; skipping '%s'", subject)
        return
    try:
        message = MIMEMultipart()
        message["From"], message["To"], message["Subject"] = sender, recipient, subject
        message.attach(MIMEText(body, "plain"))
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as server:
            server.starttls()
            server.login(sender, password)
            server.sendmail(sender, recipient, message.as_string())
    except Exception as exc:
        logging.error("Failed to send email: %s", exc)


def write_trade_to_csv(row: list[Any]) -> None:
    try:
        with TRADES_CSV.open("a", newline="") as handle:
            csv.writer(handle).writerow(row)
    except Exception as exc:
        logging.error("Failed to append local CSV: %s", exc)


def human_timestamp(value: str | None) -> str:
    if not value:
        return datetime.now(EASTERN).strftime("%Y-%m-%d %H:%M:%S")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(EASTERN).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    except ValueError:
        return value


def duration_text(opened_at: str, completed_at: str | None) -> str:
    try:
        opened = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
        closed_text = completed_at or datetime.now(timezone.utc).isoformat()
        closed = datetime.fromisoformat(closed_text.replace("Z", "+00:00"))
        seconds = max(0, int((closed - opened).total_seconds()))
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    except (ValueError, TypeError):
        return "N/A"


def ensure_dashboard_ticker(gc: Any, ticker: str) -> None:
    if gc is None:
        return
    try:
        dashboard = sheets_retry(lambda: gc.open("Aegis Trading Log").worksheet("Dashboard"))
        tickers = dashboard.col_values(1)
        ticker = ticker.strip().upper()
        if ticker in {value.strip().upper() for value in tickers}:
            return
        row_number = len(tickers) + 1
        shares = (
            f'=ROUND(SUMIFS(Sheet1!H:H,Sheet1!B:B,A{row_number},Sheet1!C:C,"BUY")-'
            f'SUMIFS(Sheet1!H:H,Sheet1!B:B,A{row_number},Sheet1!C:C,"SELL"),6)'
        )
        cost = (
            f'=IF(D{row_number}>0,SUMPRODUCT((Sheet1!B:B=A{row_number})*(Sheet1!C:C="BUY")*'
            f'(ROW(Sheet1!B:B)>MAX(INDEX((Sheet1!B:B=A{row_number})*(Sheet1!C:C="SELL")*'
            f'ROW(Sheet1!B:B),0))),Sheet1!D:D,Sheet1!H:H)/D{row_number},0)'
        )
        sheets_retry(lambda: dashboard.append_row([ticker, "", "", shares, cost], value_input_option="USER_ENTERED"))
    except Exception as exc:
        logging.error("Dashboard update failed for %s; trading continues: %s", ticker, exc)


class Scanner:
    def __init__(self, trading_client: Any, data_client: Any, gc: Any, ledger: Ledger):
        self.trading, self.data, self.gc, self.ledger = trading_client, data_client, gc, ledger
        entry_date = ledger.get_metadata("last_entry_date")
        recap_date = ledger.get_metadata("last_recap_date")
        self.last_entry_date = datetime.fromisoformat(entry_date).date() if entry_date else None
        self.last_recap_date = datetime.fromisoformat(recap_date).date() if recap_date else None
        self.realized_vol: float | None = None
        self.market_direction = "N/A"
        self.trade_messages: list[str] = []

    def price(self, symbol: str) -> float:
        snapshot = self.data.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=symbol))[symbol]
        return float(snapshot.latest_trade.price)

    def finalize_order(self, order: Any) -> None:
        """Create exactly one trade event, and only for a confirmed nonzero fill."""
        if order is None or order["logged_event_id"]:
            return
        status = str(order["status"]).lower()
        filled_qty = float(order["filled_qty"] or 0)
        fill_price = float(order["filled_avg_price"] or 0)
        if status not in FINAL_ORDER_STATUSES:
            return
        if filled_qty <= 0 or fill_price <= 0:
            logging.warning("%s %s order ended as %s without a fill", order["side"], order["symbol"], status)
            return

        symbol, side = order["symbol"], order["side"]
        signal_price = float(order["signal_price"] or 0)
        slippage = fill_price - signal_price if signal_price else 0.0
        slippage_pct = slippage / signal_price * 100 if signal_price else 0.0
        account_value = float(self.trading.get_account().portfolio_value)
        rsi, lower_band = float(order["rsi"] or 0), float(order["lower_band"] or 0)
        rsi_depth = lower_band - rsi
        entry_atr = float(order["entry_atr"] or 0)
        realized_return = realized_pnl = 0.0
        outcome, hold = "", "N/A"

        if side == "buy":
            if entry_atr <= 0:
                logging.error("Filled BUY %s has no valid entry ATR; refusing to invent exits", symbol)
                send_email("Aegis ledger error", f"Filled BUY {symbol} lacks entry ATR; inspect {order['order_id']}.")
                return
            self.ledger.save_position(
                symbol, order["order_id"], fill_price, filled_qty, entry_atr,
                order["completed_at"] or order["submitted_at"],
            )
            position = self.ledger.get_position(symbol)
            stop_price, target_price = position["stop_price"], position["target_price"]
        else:
            position = self.ledger.get_position(symbol)
            if position is None:
                logging.error("Filled SELL %s has no local entry; manual reconciliation required", symbol)
                send_email("Aegis ledger error", f"Filled SELL {symbol} lacks a local entry; inspect {order['order_id']}.")
                return
            entry_atr = float(position["entry_atr"])
            stop_price, target_price = position["stop_price"], position["target_price"]
            realized_return = fill_price / float(position["entry_price"]) - 1
            realized_pnl = (fill_price - float(position["entry_price"])) * filled_qty
            outcome = "WIN" if realized_pnl > 0 else "LOSS"
            hold = duration_text(position["opened_at"], order["completed_at"])
            remaining = max(0.0, float(position["entry_qty"]) - filled_qty)
            if remaining > 0.000001:
                self.ledger.save_position(
                    symbol, position["entry_order_id"], float(position["entry_price"]),
                    remaining, entry_atr, position["opened_at"],
                )
            else:
                self.ledger.close_position(symbol)

        row: list[Any] = [
            human_timestamp(order["completed_at"]), symbol, side.upper(), round(fill_price, 4),
            round(entry_atr, 4), round(stop_price, 4), round(target_price, 4), filled_qty,
            round(fill_price * filled_qty, 2), order["reason"], round(rsi, 2),
            "" if order["realized_vol"] is None else round(float(order["realized_vol"]), 2),
            round(account_value, 2), f"{realized_return * 100:.2f}%", outcome,
            order["market_direction"] or "N/A", round(lower_band, 2), round(entry_atr, 4),
            round(float(order["latency_ms"] or 0), 2), round(slippage, 4), round(slippage_pct, 4), hold,
            order["order_id"], order["client_order_id"], status.upper(),
            order["submitted_qty"] or order["submitted_notional"] or "", filled_qty,
            human_timestamp(order["submitted_at"]), human_timestamp(order["completed_at"]),
            round(signal_price, 4), round(rsi_depth, 4), "YES" if rsi_depth >= 1.5 else "NO",
            round(realized_pnl, 2), "",
        ]
        self.ledger.add_trade_event(order["order_id"], row)
        write_trade_to_csv(row)
        message = f"{side.upper()} {filled_qty:g} {symbol} filled at ${fill_price:.4f} ({status})"
        self.trade_messages.append(message)
        logging.info(message)

    def reconcile(self) -> None:
        reconcile_pending_orders(self.trading, self.ledger, self.finalize_order)

    def ensure_position_state(self, broker_position: Any) -> Any | None:
        symbol = broker_position.symbol
        stored = self.ledger.get_position(symbol)
        if stored:
            return stored
        if self.ledger.bootstrap_position_from_csv(symbol, TRADES_CSV):
            logging.info("Bootstrapped frozen exits for %s from local CSV", symbol)
            return self.ledger.get_position(symbol)
        indic = strategy.check_rsi_buy_signal(self.data, symbol)
        if not indic or float(indic["atr_14"]) <= 0:
            logging.error("Cannot establish protective exits for existing position %s", symbol)
            send_email("Aegis risk warning", f"No frozen exits available for existing position {symbol}.")
            return None
        self.ledger.save_position(
            symbol, None, float(broker_position.avg_entry_price), float(broker_position.qty),
            float(indic["atr_14"]),
        )
        logging.warning("Migrated %s using current ATR because no historical row was available", symbol)
        return self.ledger.get_position(symbol)

    def manage_exits(self) -> None:
        for broker_position in self.trading.get_all_positions():
            symbol = broker_position.symbol
            position = self.ensure_position_state(broker_position)
            if position is None:
                continue
            current = self.price(symbol)
            reason = None
            if current >= float(position["target_price"]):
                reason = "Take Profit (3xATR)"
            elif current <= float(position["stop_price"]):
                reason = "Stop Loss (2xATR)"
            if reason is None:
                logging.info(
                    "Holding %s current=%.2f fixed_stop=%.2f fixed_target=%.2f",
                    symbol, current, position["stop_price"], position["target_price"],
                )
                continue
            response = submit_order(
                self.trading, self.ledger, symbol=symbol, side="sell", qty=float(broker_position.qty),
                signal_price=current, entry_atr=float(position["entry_atr"]), rsi=None,
                lower_band=None, realized_vol=self.realized_vol,
                market_direction=self.market_direction, reason=reason,
            )
            if response:
                self.finalize_order(wait_for_final_order(self.trading, self.ledger, str(response.id)))

    def entry_window_open(self, now: datetime) -> bool:
        start = os.getenv("AEGIS_ENTRY_WINDOW_START", "15:45")
        end = os.getenv("AEGIS_ENTRY_WINDOW_END", "15:55")
        current = now.strftime("%H:%M")
        return start <= current <= end and self.last_entry_date != now.date()

    def scan_entries(self, now: datetime) -> dict[str, Any]:
        recap = {"rsi": [], "blocked": [], "closest": None, "margin": float("inf"), "reason": "N/A"}
        if not self.entry_window_open(now):
            return recap
        observation = risk_manager.observe_vix_term_structure(self.data)
        if observation:
            logging.info("Observe-only VIXY/VXZ z-score: %.3f (does not block trades)", observation["zscore"])
        owned = {p.symbol for p in self.trading.get_all_positions()}
        for ticker in TICKERS:
            indic = strategy.check_rsi_buy_signal(self.data, ticker)
            if not indic:
                continue
            rsi, lower = float(indic["rsi_7"]), float(indic["lower_band"])
            recap["rsi"].append(rsi)
            if rsi >= lower:
                recap["blocked"].append(ticker)
            if not indic["is_buy"] and rsi - lower < recap["margin"]:
                recap.update(
                    closest=ticker, margin=rsi - lower,
                    reason="RSI Not Oversold" if rsi >= lower else "KAMA or Bounce Failed",
                )
            if not indic["is_buy"] or ticker in owned:
                continue
            if len(self.trading.get_all_positions()) >= 5:
                logging.warning("Portfolio capacity reached; skipping %s", ticker)
                continue
            signal = self.price(ticker)
            size_usd = portfolio.calculate_position_size(
                self.trading, entry_price=signal, entry_atr=float(indic["atr_14"])
            )
            if size_usd <= 0:
                continue
            response = submit_order(
                self.trading, self.ledger, symbol=ticker, side="buy", notional=round(size_usd, 2),
                signal_price=signal, entry_atr=float(indic["atr_14"]), rsi=rsi,
                lower_band=lower, realized_vol=self.realized_vol,
                market_direction=self.market_direction, reason="KAMA-BB-RSI",
            )
            if response:
                self.finalize_order(wait_for_final_order(self.trading, self.ledger, str(response.id)))
                owned.add(ticker)
        self.last_entry_date = now.date()
        self.ledger.set_metadata("last_entry_date", now.date().isoformat())
        return recap

    def daily_recap(self, now: datetime, recap: dict[str, Any]) -> None:
        in_window = (now.hour == 15 and now.minute >= 50) or (now.hour == 16 and now.minute == 0)
        if not in_window or self.last_recap_date == now.date():
            return
        avg_rsi = sum(recap["rsi"]) / len(recap["rsi"]) if recap["rsi"] else 0
        closest = f"{recap['closest']} (margin: {recap['margin']:.2f})" if recap["closest"] else "None"
        buys = self.ledger.count_buys_on(now.strftime("%Y-%m-%d"))
        payload = [
            now.strftime("%Y-%m-%d %H:%M:%S"), self.market_direction,
            "" if self.realized_vol is None else round(self.realized_vol, 2), buys,
            closest, recap["reason"], ", ".join(recap["blocked"]), round(avg_rsi, 2),
        ]
        if self.gc:
            try:
                sheet = sheets_retry(lambda: self.gc.open("Aegis Trading Log").worksheet("Daily Recap"))
                sheets_retry(lambda: sheet.append_row(payload))
            except Exception as exc:
                logging.error("Daily Recap Sheet write failed; trading continues: %s", exc)
                send_email("Aegis Sheets warning", f"Daily Recap upload failed: {exc}")
        send_email(
            "Aegis Daily Recap",
            f"Market: {self.market_direction}\nSPY 20D Realized Volatility: {self.realized_vol}\n"
            f"New positions: {buys}\nClosest signal: {closest}",
        )
        self.last_recap_date = now.date()
        self.ledger.set_metadata("last_recap_date", now.date().isoformat())

    def cycle(self) -> None:
        self.trade_messages = []
        if self.gc is None:
            self.gc = google_client()
        self.reconcile()
        sync_trade_queue(self.gc, self.ledger, ensure_dashboard_ticker)
        if not self.trading.get_clock().is_open:
            logging.info("Market closed")
            return
        now = datetime.now(EASTERN)
        self.realized_vol = risk_manager.get_spy_realized_volatility_20d(self.data)
        self.market_direction = risk_manager.get_market_direction(self.data)
        self.manage_exits()
        recap = self.scan_entries(now)
        sync_trade_queue(self.gc, self.ledger, ensure_dashboard_ticker)
        if self.trade_messages:
            send_email("Aegis Trade Alert", "\n".join(self.trade_messages))
        self.daily_recap(now, recap)


def google_client() -> Any | None:
    try:
        return gspread.service_account(filename=str(BASE_DIR / "credentials.json"))
    except Exception as exc:
        logging.error("Google Sheets unavailable at startup; local queue remains active: %s", exc)
        return None


def run_scanner() -> None:
    paper = validate_trading_mode()
    key, secret = os.getenv("APCA_API_KEY_ID"), os.getenv("APCA_API_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError("APCA_API_KEY_ID and APCA_API_SECRET_KEY are required")
    trading = TradingClient(key, secret, paper=paper)
    scanner = Scanner(trading, StockHistoricalDataClient(key, secret), google_client(), Ledger(LEDGER_DB))
    run_once = env_bool("AEGIS_RUN_ONCE", False)
    interval = max(30, int(os.getenv("AEGIS_CYCLE_SECONDS", "300")))
    while True:
        try:
            scanner.cycle()
        except Exception as exc:
            logging.exception("Aegis cycle failed: %s", exc)
            send_email("Aegis Trading Error", str(exc))
        if run_once:
            return
        time.sleep(interval)


if __name__ == "__main__":
    try:
        with InstanceLock(LOCK_FILE):
            run_scanner()
    except AlreadyRunningError as exc:
        logging.error("%s", exc)
        sys.exit(2)
