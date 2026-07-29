"""Aegis paper-trading scanner with durable order and execution state."""

from __future__ import annotations

import csv
import logging
import os
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
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
from hedge import (
    RISK_NORMAL,
    RISK_SEVERE,
    RISK_UNKNOWN,
    HedgeConfig,
)
from instance_lock import AlreadyRunningError, InstanceLock
from ledger import FINAL_ORDER_STATUSES, Ledger
from reddit_sensor import RedditSensor
from sheet_sync import retry as sheets_retry
from sheet_sync import ensure_report_card, sync_reddit_queue, sync_trade_queue

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


def validate_hedge_mode(paper: bool, config: HedgeConfig) -> None:
    """Block the active hedge overlay from ever operating in a live account."""
    if config.mode == "paper" and not paper:
        raise RuntimeError(
            "Active hedge mode is paper-only; set AEGIS_HEDGE_MODE=off or observe"
        )


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
        tickers = sheets_retry(lambda: dashboard.col_values(1))
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
    def __init__(
        self, trading_client: Any, data_client: Any, gc: Any, ledger: Ledger,
        hedge_config: HedgeConfig | None = None,
    ):
        self.trading, self.data, self.gc, self.ledger = trading_client, data_client, gc, ledger
        entry_date = ledger.get_metadata("last_entry_date")
        recap_date = ledger.get_metadata("last_recap_date")
        self.last_entry_date = datetime.fromisoformat(entry_date).date() if entry_date else None
        self.last_recap_date = datetime.fromisoformat(recap_date).date() if recap_date else None
        self.realized_vol: float | None = None
        self.market_direction = "N/A"
        self.hedge_config = hedge_config or HedgeConfig.from_env()
        self.market_returns: dict[str, float] = {}
        self.risk_state = RISK_NORMAL if self.hedge_config.mode == "off" else RISK_UNKNOWN
        self.hedge_entry_streak = 0
        self.hedge_exit_streak = 0
        self.hedge_last_observation_at: datetime | None = None
        self.trade_messages: list[str] = []
        self.reddit_sensor = RedditSensor.from_env(ledger, TICKERS)
        self.report_card_ready = False

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
        # Sheet1 formats Slippage (%) as a percentage, so store the decimal
        # ratio. The sign remains fill minus signal for both sides: positive
        # means a higher fill, and negative means a lower fill.
        slippage_pct = slippage / signal_price if signal_price else 0.0
        account_value = float(self.trading.get_account().portfolio_value)
        rsi, lower_band = float(order["rsi"] or 0), float(order["lower_band"] or 0)
        rsi_depth = lower_band - rsi
        order_role = str(order["order_role"] or "strategy").lower()
        if order_role not in {"strategy", "hedge"}:
            logging.error("Order %s has unknown role %s", order["order_id"], order_role)
            return
        entry_atr = float(order["entry_atr"] or 0)
        realized_return = realized_pnl = 0.0
        outcome, hold = "", "N/A"

        if order_role == "hedge" and side == "buy":
            existing = self.ledger.get_hedge_position(symbol)
            if existing:
                old_qty = float(existing["entry_qty"])
                combined_qty = old_qty + filled_qty
                combined_price = (
                    (float(existing["entry_price"]) * old_qty)
                    + (fill_price * filled_qty)
                ) / combined_qty
                opened_at = str(existing["opened_at"])
                entry_order_id = str(existing["entry_order_id"] or order["order_id"])
            else:
                combined_qty = filled_qty
                combined_price = fill_price
                opened_at = order["completed_at"] or order["submitted_at"]
                entry_order_id = order["order_id"]
            self.ledger.save_hedge_position(
                symbol, entry_order_id, combined_price, combined_qty, opened_at
            )
            entry_atr = stop_price = target_price = 0.0
        elif order_role == "hedge" and side == "sell":
            position = self.ledger.get_hedge_position(symbol)
            if position is None:
                logging.error(
                    "Filled hedge SELL %s has no local entry; manual reconciliation required",
                    symbol,
                )
                send_email(
                    "Aegis hedge ledger error",
                    f"Filled hedge SELL {symbol} lacks a local entry; inspect {order['order_id']}.",
                )
                return
            entry_price = float(position["entry_price"])
            realized_return = fill_price / entry_price - 1
            realized_pnl = (fill_price - entry_price) * filled_qty
            outcome = "WIN" if realized_pnl > 0 else "LOSS"
            hold = duration_text(position["opened_at"], order["completed_at"])
            remaining = max(0.0, float(position["entry_qty"]) - filled_qty)
            if remaining > 0.000001:
                self.ledger.save_hedge_position(
                    symbol, position["entry_order_id"], entry_price, remaining,
                    position["opened_at"],
                )
            else:
                self.ledger.close_hedge_position(symbol)
                completed_at = (
                    order["completed_at"]
                    or datetime.now(timezone.utc).isoformat()
                )
                self.ledger.set_metadata(
                    self._hedge_last_exit_key(symbol), str(completed_at)
                )
                self.hedge_entry_streak = 0
                self.hedge_exit_streak = 0
            entry_atr = stop_price = target_price = 0.0
        elif side == "buy":
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
        prefix = "HEDGE " if order_role == "hedge" else ""
        message = (
            f"{prefix}{side.upper()} {filled_qty:g} {symbol} "
            f"filled at ${fill_price:.4f} ({status})"
        )
        if order_role == "hedge" and order["reason"]:
            message = f"{message} | {order['reason']}"
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

    def update_market_risk(self) -> None:
        if self.hedge_config.mode == "off":
            self.market_returns = {}
            self.risk_state = RISK_NORMAL
            return
        returns = risk_manager.get_market_day_returns(self.data)
        self.market_returns = returns or {}
        self.risk_state = self.hedge_config.classify(self.market_returns.get("QQQ"))
        qqq_text = (
            f"{self.market_returns['QQQ']:.2%}"
            if "QQQ" in self.market_returns else "unavailable"
        )
        logging.info(
            "QQQ risk governor mode=%s state=%s session_return=%s",
            self.hedge_config.mode, self.risk_state, qqq_text,
        )

    @staticmethod
    def _hedge_last_exit_key(symbol: str) -> str:
        return f"hedge_last_exit_at:{symbol.strip().upper()}"

    def _hedge_cooldown_remaining_minutes(self, now: datetime) -> float:
        cooldown = self.hedge_config.reentry_cooldown_minutes
        if cooldown <= 0:
            return 0.0
        value = self.ledger.get_metadata(
            self._hedge_last_exit_key(self.hedge_config.symbol)
        )
        if not value:
            return 0.0
        try:
            exited_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if exited_at.tzinfo is None:
                exited_at = exited_at.replace(tzinfo=timezone.utc)
            current = now
            if current.tzinfo is None:
                current = current.replace(tzinfo=EASTERN)
            elapsed = (
                current.astimezone(timezone.utc)
                - exited_at.astimezone(timezone.utc)
            ).total_seconds() / 60
        except (TypeError, ValueError):
            logging.error(
                "Invalid hedge exit timestamp %r; preserving the full cooldown",
                value,
            )
            return float(cooldown)
        return max(0.0, float(cooldown) - max(0.0, elapsed))

    def _reset_stale_hedge_confirmation(self, now: datetime) -> None:
        previous = self.hedge_last_observation_at
        if previous is not None:
            current = now
            if current.tzinfo is None:
                current = current.replace(tzinfo=EASTERN)
            prior = previous
            if prior.tzinfo is None:
                prior = prior.replace(tzinfo=EASTERN)
            gap = current.astimezone(timezone.utc) - prior.astimezone(timezone.utc)
            if current.date() != prior.date() or gap > timedelta(minutes=15):
                self.hedge_entry_streak = 0
                self.hedge_exit_streak = 0
        self.hedge_last_observation_at = now

    def _confirmed_hedge_target(
        self,
        gross_long: float,
        current_hedge: float,
        hedge_is_open: bool,
        now: datetime,
    ) -> tuple[float, float]:
        """Return a target after confirmation and durable cooldown controls."""
        config = self.hedge_config
        self._reset_stale_hedge_confirmation(now)
        cooldown_remaining = self._hedge_cooldown_remaining_minutes(now)

        if hedge_is_open:
            self.hedge_entry_streak = 0
            if gross_long <= 0:
                self.hedge_exit_streak = 0
                return 0.0, cooldown_remaining
            if self.risk_state == RISK_UNKNOWN:
                self.hedge_exit_streak = 0
                return current_hedge, cooldown_remaining
            if self.risk_state == RISK_NORMAL:
                self.hedge_exit_streak += 1
                if self.hedge_exit_streak < config.exit_confirmation_cycles:
                    return current_hedge, cooldown_remaining
                return 0.0, cooldown_remaining
            self.hedge_exit_streak = 0
            return (
                config.target_notional(gross_long, self.risk_state, True),
                cooldown_remaining,
            )

        self.hedge_exit_streak = 0
        if (
            gross_long <= 0
            or self.risk_state != RISK_SEVERE
            or cooldown_remaining > 0
        ):
            self.hedge_entry_streak = 0
            return 0.0, cooldown_remaining
        self.hedge_entry_streak += 1
        if self.hedge_entry_streak < config.entry_confirmation_cycles:
            return 0.0, cooldown_remaining
        return (
            config.target_notional(gross_long, self.risk_state, False),
            cooldown_remaining,
        )

    def _position_market_value(self, broker_position: Any) -> float:
        market_value = getattr(broker_position, "market_value", None)
        if market_value is not None:
            return abs(float(market_value))
        return abs(float(broker_position.qty)) * self.price(str(broker_position.symbol))

    def _strategy_gross_long_notional(self, broker_positions: list[Any]) -> float:
        total = 0.0
        for position in broker_positions:
            symbol = str(position.symbol).upper()
            qty = float(position.qty)
            if symbol in TICKERS and qty > 0:
                total += self._position_market_value(position)
        return round(total, 2)

    def _hedge_broker_position(self, broker_positions: list[Any]) -> Any | None:
        return next(
            (
                position for position in broker_positions
                if str(position.symbol).upper() == self.hedge_config.symbol
                and float(position.qty) > 0
            ),
            None,
        )

    def _submit_hedge_order(
        self, *, side: str, signal_price: float, reason: str,
        confirmation: str, qty: float | None = None,
        notional: float | None = None,
    ) -> None:
        qqq_return = self.market_returns.get("QQQ")
        if qqq_return is None:
            logging.error(
                "Refusing %s hedge order without an exact QQQ session return",
                side,
            )
            return
        contextual_reason = (
            f"{reason} | QQQ session {qqq_return:.3%} | {confirmation}"
        )
        response = submit_order(
            self.trading, self.ledger,
            symbol=self.hedge_config.symbol, side=side, qty=qty, notional=notional,
            signal_price=signal_price, entry_atr=None, rsi=None, lower_band=None,
            realized_vol=self.realized_vol, market_direction=self.market_direction,
            reason=contextual_reason, order_role="hedge",
        )
        if response:
            self.finalize_order(
                wait_for_final_order(self.trading, self.ledger, str(response.id))
            )

    def manage_hedge(self, now: datetime | None = None) -> None:
        """Observe or rebalance the short-term PSQ overlay using confirmed fills."""
        config = self.hedge_config
        if config.mode == "off":
            return
        now = now or datetime.now(EASTERN)
        broker_positions = list(self.trading.get_all_positions())
        gross_long = self._strategy_gross_long_notional(broker_positions)
        broker_hedge = self._hedge_broker_position(broker_positions)
        local_hedge = self.ledger.get_hedge_position(config.symbol)
        hedge_is_open = broker_hedge is not None and local_hedge is not None
        current = self._position_market_value(broker_hedge) if broker_hedge else 0.0
        if broker_hedge is not None and local_hedge is None:
            logging.error(
                "Broker holds %s without Aegis hedge ledger state; refusing to manage it",
                config.symbol,
            )
            return
        if broker_hedge is None and local_hedge is not None:
            logging.error(
                "Aegis hedge ledger contains %s but broker does not; manual reconciliation required",
                config.symbol,
            )
            return
        target, cooldown_remaining = self._confirmed_hedge_target(
            gross_long, current, hedge_is_open, now
        )
        logging.info(
            "Hedge assessment state=%s gross_longs=$%.2f current_%s=$%.2f "
            "target=$%.2f entry_confirm=%d/%d exit_confirm=%d/%d "
            "reentry_cooldown=%.1fm",
            self.risk_state, gross_long, config.symbol, current, target,
            min(self.hedge_entry_streak, config.entry_confirmation_cycles),
            config.entry_confirmation_cycles,
            min(self.hedge_exit_streak, config.exit_confirmation_cycles),
            config.exit_confirmation_cycles,
            cooldown_remaining,
        )

        if config.mode == "observe":
            return
        if self.risk_state == RISK_UNKNOWN:
            logging.warning(
                "QQQ risk data unavailable; preserving any hedge and refusing hedge orders"
            )
            return

        if target <= 0:
            if broker_hedge is not None:
                signal_price = self.price(config.symbol)
                confirmation = (
                    "no strategy long exposure"
                    if gross_long <= 0
                    else f"exit confirmation "
                    f"{min(self.hedge_exit_streak, config.exit_confirmation_cycles)}"
                    f"/{config.exit_confirmation_cycles}"
                )
                self._submit_hedge_order(
                    side="sell", qty=float(broker_hedge.qty),
                    signal_price=signal_price, reason="QQQ Risk Hedge Exit",
                    confirmation=confirmation,
                )
            return

        if not config.rebalance_required(current, target):
            return
        difference = target - current
        if difference > 0:
            if broker_hedge is not None:
                logging.info(
                    "Existing %s hedge is below target; not averaging down",
                    config.symbol,
                )
                return
            signal_price = self.price(config.symbol)
            self._submit_hedge_order(
                side="buy", notional=round(difference, 2),
                signal_price=signal_price, reason="QQQ Risk Hedge",
                confirmation=(
                    f"entry confirmation "
                    f"{min(self.hedge_entry_streak, config.entry_confirmation_cycles)}"
                    f"/{config.entry_confirmation_cycles}"
                ),
            )
            return
        signal_price = self.price(config.symbol)
        qty = min(float(broker_hedge.qty), abs(difference) / signal_price)
        if qty > 0.000001:
            self._submit_hedge_order(
                side="sell", qty=round(qty, 6),
                signal_price=signal_price, reason="QQQ Risk Hedge Rebalance",
                confirmation="confirmed hedge remains active",
            )

    def manage_exits(self) -> None:
        for broker_position in self.trading.get_all_positions():
            symbol = broker_position.symbol
            if str(symbol).upper() == self.hedge_config.symbol:
                continue
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

    def update_reddit_outcomes(self, now: datetime) -> None:
        now_utc = now.astimezone(timezone.utc)
        for signal in self.ledger.reddit_signals_needing_outcomes():
            try:
                observed = datetime.fromisoformat(str(signal["observed_at"]).replace("Z", "+00:00"))
            except ValueError:
                logging.error("Reddit signal %s has an invalid timestamp", signal["signal_id"])
                continue
            due: list[str] = []
            if signal["return_1h"] is None and now_utc >= observed + timedelta(hours=1):
                due.append("1h")
            if signal["return_24h"] is None and now_utc >= observed + timedelta(hours=24):
                due.append("24h")
            if signal["return_120h"] is None and now_utc >= observed + timedelta(hours=120):
                due.append("120h")
            if not due:
                continue
            try:
                current = self.price(str(signal["symbol"]))
            except Exception as exc:
                logging.error("Could not price Reddit outcome for %s: %s", signal["symbol"], exc)
                continue
            for horizon in due:
                self.ledger.update_reddit_outcome(
                    str(signal["signal_id"]), horizon, current, now_utc.isoformat()
                )

    def scan_reddit_research(self, now: datetime) -> None:
        """Run technical checks only after a positive Reddit signal, then log it."""
        if self.reddit_sensor is None:
            return
        now_utc = now.astimezone(timezone.utc)
        last_scan_text = self.ledger.get_metadata("last_reddit_scan_at")
        interval = max(300, int(os.getenv("AEGIS_REDDIT_SCAN_SECONDS", "900")))
        if last_scan_text:
            try:
                last_scan = datetime.fromisoformat(last_scan_text.replace("Z", "+00:00"))
                if (now_utc - last_scan).total_seconds() < interval:
                    return
            except ValueError:
                logging.warning("Ignoring invalid last_reddit_scan_at metadata")
        try:
            signals = self.reddit_sensor.scan(now_utc)
            self.ledger.set_metadata("last_reddit_scan_at", now_utc.isoformat())
        except Exception as exc:
            logging.exception("Reddit sensor scan failed: %s", exc)
            return

        for signal in signals:
            indic = strategy.check_rsi_buy_signal(self.data, signal.symbol)
            try:
                signal_price = self.price(signal.symbol)
            except Exception as exc:
                logging.error("Could not price positive Reddit signal for %s: %s", signal.symbol, exc)
                continue
            technical_checked = indic is not None
            technical_pass = bool(indic["is_buy"]) if indic else None
            signal_id = self.ledger.add_reddit_signal({
                "observed_at": signal.observed_at,
                "symbol": signal.symbol,
                "subreddits": signal.subreddits,
                "sentiment_score": signal.sentiment_score,
                "mention_count": signal.mention_count,
                "weighted_mentions": signal.weighted_mentions,
                "baseline_mentions": signal.baseline_mentions,
                "baseline_samples": signal.baseline_samples,
                "spike_ratio": signal.spike_ratio,
                "price_at_signal": signal_price,
                "technical_checked": technical_checked,
                "technical_pass": technical_pass,
                "above_kama": indic.get("above_kama") if indic else None,
                "rsi_oversold": indic.get("rsi_oversold") if indic else None,
                "intraday_bounce": indic.get("intraday_bounce") if indic else None,
                "rsi": indic.get("rsi_7") if indic else None,
                "lower_band": indic.get("lower_band") if indic else None,
                "kama": indic.get("kama") if indic else None,
                "atr": indic.get("atr_14") if indic else None,
                "market_direction": self.market_direction,
                "realized_vol": self.realized_vol,
                "sample_posts": signal.sample_posts,
            })
            logging.info(
                "Reddit research signal %s %s sentiment=%.3f mentions=%d spike=%.2f technical_pass=%s; no trade authority",
                signal_id, signal.symbol, signal.sentiment_score, signal.mention_count,
                signal.spike_ratio, technical_pass,
            )

    def entry_window_open(self, now: datetime) -> bool:
        start = os.getenv("AEGIS_ENTRY_WINDOW_START", "15:45")
        end = os.getenv("AEGIS_ENTRY_WINDOW_END", "15:55")
        current = now.strftime("%H:%M")
        return start <= current <= end and self.last_entry_date != now.date()

    def scan_entries(self, now: datetime) -> dict[str, Any]:
        recap = {
            "rsi": [], "blocked": [], "closest": None, "margin": float("inf"),
            "reason": "N/A", "risk_state": self.risk_state,
            "qqq_return": self.market_returns.get("QQQ"),
        }
        if not self.entry_window_open(now):
            return recap
        observation = risk_manager.observe_vix_term_structure(self.data)
        if observation:
            logging.info("Observe-only VIXY/VXZ z-score: %.3f (does not block trades)", observation["zscore"])
        exposure_factor = self.hedge_config.entry_exposure_factor(self.risk_state)
        if exposure_factor <= 0:
            recap["reason"] = (
                "QQQ Risk Data Unavailable"
                if self.risk_state == RISK_UNKNOWN
                else "QQQ Severe Risk-Off"
            )
            logging.warning(
                "QQQ risk governor blocked all new entries: state=%s",
                self.risk_state,
            )
            self.last_entry_date = now.date()
            self.ledger.set_metadata("last_entry_date", now.date().isoformat())
            return recap
        broker_positions = list(self.trading.get_all_positions())
        owned = {
            str(position.symbol).upper() for position in broker_positions
            if str(position.symbol).upper() in TICKERS
        }
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
            strategy_position_count = sum(
                1 for position in self.trading.get_all_positions()
                if str(position.symbol).upper() in TICKERS
            )
            if strategy_position_count >= 5:
                logging.warning("Portfolio capacity reached; skipping %s", ticker)
                continue
            signal = self.price(ticker)
            size_usd = portfolio.calculate_position_size(
                self.trading, entry_price=signal, entry_atr=float(indic["atr_14"]),
                strategy_capital=float(os.getenv("AEGIS_STRATEGY_CAPITAL", "3600")),
            )
            if size_usd <= 0:
                continue
            size_usd = round(size_usd * exposure_factor, 2)
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
        if not self.report_card_ready:
            self.report_card_ready = ensure_report_card(
                self.gc,
                strategy_capital=float(os.getenv("AEGIS_STRATEGY_CAPITAL", "3600")),
                hedge_symbol=self.hedge_config.symbol,
            )
        self.reconcile()
        sync_trade_queue(self.gc, self.ledger, ensure_dashboard_ticker)
        sync_reddit_queue(self.gc, self.ledger)
        if not self.trading.get_clock().is_open:
            logging.info("Market closed")
            return
        now = datetime.now(EASTERN)
        self.realized_vol = risk_manager.get_spy_realized_volatility_20d(self.data)
        self.market_direction = risk_manager.get_market_direction(self.data)
        self.update_market_risk()
        self.update_reddit_outcomes(now)
        self.scan_reddit_research(now)
        self.manage_exits()
        self.manage_hedge(now)
        recap = self.scan_entries(now)
        sync_trade_queue(self.gc, self.ledger, ensure_dashboard_ticker)
        sync_reddit_queue(self.gc, self.ledger)
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
    hedge_config = HedgeConfig.from_env()
    validate_hedge_mode(paper, hedge_config)
    key, secret = os.getenv("APCA_API_KEY_ID"), os.getenv("APCA_API_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError("APCA_API_KEY_ID and APCA_API_SECRET_KEY are required")
    trading = TradingClient(key, secret, paper=paper)
    scanner = Scanner(
        trading, StockHistoricalDataClient(key, secret), google_client(),
        Ledger(LEDGER_DB), hedge_config,
    )
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
