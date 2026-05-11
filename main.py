import os
import sys
import csv
import time
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone
import gspread
try:
    import zoneinfo
except ImportError:
    from backports import zoneinfo

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockSnapshotRequest

import risk_manager
import portfolio
import strategy
from dotenv import load_dotenv

env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
load_dotenv(dotenv_path=env_path)

# Log timestamps in US/Eastern time (EST/EDT) by overriding the class-level
# converter used by every logging.Formatter instance.
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
try:
    _eastern = zoneinfo.ZoneInfo("America/New_York")
    logging.Formatter.converter = lambda *args: datetime.now(_eastern).timetuple()
except Exception:
    pass

# Absolute path to the local master trade log. Used for persistent BUY counting
# across reboots and as the ground truth for the daily recap tally.
_BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
TRADES_CSV = os.path.join(_BASE_DIR, 'Aegis Trading Log - Sheet1.csv')


def write_trade_to_csv(row):
    """Append one completed trade row to the local master CSV."""
    try:
        with open(TRADES_CSV, 'a', newline='') as f:
            csv.writer(f).writerow(row)
    except Exception as e:
        logging.error(f"Failed to write trade to local CSV: {type(e).__name__}: {e}")


def count_buys_today(date_str):
    """
    Read TRADES_CSV and return the number of BUY rows whose timestamp starts
    with date_str (YYYY-MM-DD). Returns 0 if the file is absent or unreadable.
    """
    try:
        with open(TRADES_CSV, 'r', newline='') as f:
            return sum(
                1 for row in csv.reader(f)
                if len(row) >= 3
                and row[2].strip() == 'BUY'
                and row[0].startswith(date_str)
            )
    except FileNotFoundError:
        logging.warning(f"Local trades CSV not found at {TRADES_CSV}. Returning 0 for recap.")
        return 0
    except Exception as e:
        logging.error(f"Failed to count buys from local CSV: {type(e).__name__}: {e}")
        return 0


def send_email(subject, body):
    sender_email    = os.environ.get("SENDER_EMAIL")
    sender_password = os.environ.get("SENDER_PASSWORD")
    recipient_email = os.environ.get("RECIPIENT_EMAIL")

    if not sender_email or not sender_password or not recipient_email:
        logging.warning("Email credentials not set. Skipping email.")
        return

    try:
        msg = MIMEMultipart()
        msg['From']    = sender_email
        msg['To']      = recipient_email
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))

        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(sender_email, sender_password)
        server.sendmail(sender_email, recipient_email, msg.as_string())
        server.quit()
        logging.info("Email sent successfully.")
    except Exception as e:
        logging.error(f"Failed to send email: {e}")


def run_scanner():
    # 1. Initialize clients
    try:
        api_key    = os.environ.get("APCA_API_KEY_ID", "dummy_key")
        api_secret = os.environ.get("APCA_API_SECRET_KEY", "dummy_secret")
        trading_client = TradingClient(api_key, api_secret, paper=True)
        data_client    = StockHistoricalDataClient(api_key, api_secret)
        logging.info("Alpaca Trading Client and Data Client initialized.")
    except Exception as e:
        msg = f"Failed to initialize Alpaca Clients: {e}"
        logging.error(msg)
        send_email("Aegis Trading Error", msg)
        sys.exit(1)

    try:
        gc = None
        cred_path = os.path.join(_BASE_DIR, 'credentials.json')
        gc = gspread.service_account(filename=cred_path)
        if gc:
            logging.info("Google Sheets initialized.")
        else:
            logging.warning("Google Sheets credentials not found. Logging to sheets disabled.")
    except Exception as e:
        logging.error(f"Failed to initialize gspread: {e}")
        gc = None

    # Tracks which calendar date the EOD recap was last sent to prevent repeat fires.
    last_recap_date = None

    while True:
        messages = []
        messages.append(f"Aegis Trading Bot Report - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

        # Resolve NY timezone once per iteration, used for PDT shield and recap gate.
        try:
            ny_tz = zoneinfo.ZoneInfo("America/New_York")
        except Exception:
            ny_tz = timezone.utc
        now_ny = datetime.now(ny_tz)

        # 2. Check if market is open
        try:
            clock = trading_client.get_clock()
            if not clock.is_open:
                logging.info("Market Closed - Sleeping")
                time.sleep(300)
                continue
        except Exception as e:
            logging.error(f"Failed to check market status: {e}")
            time.sleep(300)
            continue

        # Fetch global metrics
        current_vix      = risk_manager.get_vix(data_client)
        market_direction = risk_manager.get_market_direction(data_client)

        successful_trades = []

        # Recap accumulators, populated in step 5 and consumed in step 6.
        recap_rsi_list = []
        recap_blocked  = []
        closest_margin = float('inf')
        closest_ticker = "None"
        reason_no_buy  = "N/A"

        # 3. Manage Sells, dynamic ATR exits with latency, slippage, and duration telemetry
        try:
            positions = trading_client.get_all_positions()

            today_ny  = now_ny.replace(hour=0, minute=0, second=0, microsecond=0)
            today_utc = today_ny.astimezone(timezone.utc)

            req = GetOrdersRequest(
                status=QueryOrderStatus.CLOSED,
                limit=500,
                after=today_utc
            )
            recent_orders = trading_client.get_orders(req)

            bought_today = set()
            for order in recent_orders:
                if order.side == OrderSide.BUY and order.filled_at and order.filled_at >= today_utc:
                    bought_today.add(order.symbol)

            for position in positions:
                symbol = position.symbol
                if symbol in bought_today:
                    logging.info(f"PDT Shield: {symbol} was bought today. Skipping sell check.")
                    continue

                indic = strategy.check_rsi_buy_signal(data_client, symbol)
                if not indic:
                    logging.warning(f"Could not fetch indicators for {symbol}. Skipping sell check.")
                    continue

                atr_14                = indic["atr_14"]
                avg_entry_price       = float(position.avg_entry_price)
                take_profit_threshold = avg_entry_price + (3.0 * atr_14)
                stop_loss_threshold   = avg_entry_price - (2.0 * atr_14)

                try:
                    snapshot      = data_client.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=symbol))[symbol]
                    current_price = snapshot.latest_trade.price
                except Exception as e:
                    logging.error(f"Failed to fetch snapshot for {symbol}: {e}")
                    continue

                take_profit = current_price >= take_profit_threshold
                stop_loss   = current_price <= stop_loss_threshold

                if take_profit or stop_loss:
                    reason = "Take Profit (3xATR)" if take_profit else "Stop Loss (2xATR)"
                    logging.info(
                        f"Triggering {reason} for {symbol} | "
                        f"Entry={avg_entry_price:.2f}  Current={current_price:.2f}  ATR={atr_14:.2f}  "
                        f"TP={take_profit_threshold:.2f}  SL={stop_loss_threshold:.2f}"
                    )
                    try:
                        order_data = MarketOrderRequest(
                            symbol=symbol,
                            qty=position.qty,
                            side=OrderSide.SELL,
                            time_in_force=TimeInForce.DAY
                        )

                        # Latency sensor: bracket submit_order in milliseconds
                        t_before  = time.time()
                        sell_resp = trading_client.submit_order(order_data=order_data)
                        t_after   = time.time()
                        sell_latency_ms = round((t_after - t_before) * 1000, 2)

                        msg = f"SELL {position.qty} shares of {symbol} at market ({reason})"
                        logging.info(msg)
                        messages.append(msg)

                        # Slippage sensor: allow fill window then fetch average_fill_price
                        # Signal price for sells is the pre-order snapshot (current_price)
                        time.sleep(1.0)
                        sell_fill_price = None
                        try:
                            filled_sell = trading_client.get_order_by_id(str(sell_resp.id))
                            if filled_sell.filled_avg_price is not None:
                                sell_fill_price = round(float(filled_sell.filled_avg_price), 4)
                        except Exception as fe:
                            logging.warning(
                                f"Could not fetch sell fill price for {symbol}: "
                                f"{type(fe).__name__}: {fe}"
                            )

                        if sell_fill_price and current_price > 0:
                            sell_slip_dollar = round(sell_fill_price - current_price, 4)
                            sell_slip_pct    = round(
                                (sell_fill_price - current_price) / current_price * 100, 4
                            )
                        else:
                            sell_slip_dollar = 0
                            sell_slip_pct    = 0

                        # Duration sensor: scan closed orders to find the original buy fill time
                        hold_duration_str = "N/A"
                        try:
                            order_history = trading_client.get_orders(GetOrdersRequest(
                                status=QueryOrderStatus.CLOSED,
                                limit=100
                            ))
                            for o in order_history:
                                if o.symbol == symbol and o.side == OrderSide.BUY and o.filled_at:
                                    delta = datetime.now(timezone.utc) - o.filled_at
                                    h = int(delta.total_seconds() // 3600)
                                    m = int((delta.total_seconds() % 3600) // 60)
                                    hold_duration_str = f"{h}h {m}m"
                                    break
                        except Exception as de:
                            logging.warning(
                                f"Could not calculate hold duration for {symbol}: "
                                f"{type(de).__name__}: {de}"
                            )

                        logging.info(
                            f"Telemetry SELL {symbol} | latency={sell_latency_ms}ms  "
                            f"signal={current_price}  fill={sell_fill_price}  "
                            f"slippage=${sell_slip_dollar} ({sell_slip_pct}%)  "
                            f"hold={hold_duration_str}"
                        )

                        unrealized_plpc = float(position.unrealized_plpc)
                        port_val        = float(trading_client.get_account().portfolio_value)

                        trade_row = [
                            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            symbol,
                            "SELL",
                            round(current_price, 2),
                            "N/A",
                            "N/A",
                            "N/A",
                            float(position.qty),
                            round(float(position.qty) * current_price, 2),
                            reason,
                            round(indic["rsi_7"], 2),
                            round(current_vix, 2),
                            round(port_val, 2),
                            f"{unrealized_plpc * 100:.2f}%",
                            "WIN" if unrealized_plpc > 0 else "LOSS",
                            market_direction,
                            round(indic["lower_band"], 2),
                            round(indic["atr_14"], 2),
                            # Telemetry fields appended to master log
                            sell_latency_ms,
                            sell_slip_dollar,
                            sell_slip_pct,
                            hold_duration_str,
                        ]
                        successful_trades.append(trade_row)
                        write_trade_to_csv(trade_row)

                    except Exception as e:
                        logging.error(f"Failed to sell {symbol}: {e}")
                else:
                    logging.info(
                        f"Holding {symbol} | Current={current_price:.2f}  "
                        f"TP={take_profit_threshold:.2f}  SL={stop_loss_threshold:.2f}"
                    )
        except Exception as e:
            logging.error(f"Error during sell management: {e}")

        # 4. Check VIX term structure kill switch
        try:
            if risk_manager.check_vix_kill_switch(data_client):
                msg = "VIX term structure kill switch activated (ratio >= 0.95). Skipping buys."
                logging.warning(msg)
                messages.append(msg)
                time.sleep(300)
                continue
        except Exception as e:
            logging.error(f"Error checking VIX kill switch: {e}")

        # 5. Scan for Buys, fractional notional market orders with latency and slippage telemetry
        tickers_to_scan = [
            'TSLA', 'NVDA', 'AMD', 'PLTR', 'COIN', 'MSTR', 'SMCI', 'CRWD',
            'SNOW', 'SHOP', 'ROKU', 'MSFT', 'META', 'NFLX', 'AMZN', 'UBER', 'DASH'
        ]

        try:
            positions     = trading_client.get_all_positions()
            owned_tickers = {p.symbol for p in positions}

            for ticker in tickers_to_scan:
                indic = strategy.check_rsi_buy_signal(data_client, ticker)
                if not indic:
                    continue

                rsi_7      = indic["rsi_7"]
                lower_band = indic["lower_band"]
                recap_rsi_list.append(rsi_7)

                if rsi_7 >= lower_band:
                    recap_blocked.append(ticker)

                if not indic["is_buy"]:
                    margin = rsi_7 - lower_band
                    if margin < closest_margin:
                        closest_margin = margin
                        closest_ticker = ticker
                        reason_no_buy  = "RSI Not Oversold" if rsi_7 >= lower_band else "KAMA or Bounce Failed"

                if ticker in owned_tickers:
                    logging.info(f"Already own {ticker}. Skipping buy check.")
                    continue

                if indic["is_buy"]:
                    logging.info(f"Buy signal triggered for {ticker}.")

                    # ── Portfolio Governor ────────────────────────────────────
                    # Hard cap: 5 open positions, $720 minimum available cash.
                    live_positions = trading_client.get_all_positions()
                    if len(live_positions) >= 5:
                        gov_msg = f"Portfolio Capacity Reached (5/5). Skipping buy for {ticker}."
                        logging.warning(gov_msg)
                        messages.append("Portfolio Capacity Reached (5/5)")
                        continue
                    acct_cash = float(trading_client.get_account().cash)
                    if acct_cash < 720.0:
                        logging.warning(
                            f"Insufficient cash (${acct_cash:.2f} < $720.00). "
                            f"Skipping buy for {ticker}."
                        )
                        continue
                    # ─────────────────────────────────────────────────────────

                    size_usd = portfolio.calculate_position_size(trading_client)

                    if size_usd > 0:
                        try:
                            # Slippage sensor step 1: capture signal price before order submission
                            signal_price = 0.0
                            try:
                                pre_snap     = data_client.get_stock_snapshot(
                                    StockSnapshotRequest(symbol_or_symbols=ticker)
                                )[ticker]
                                signal_price = round(pre_snap.latest_trade.price, 2)
                            except Exception as se:
                                logging.warning(
                                    f"Pre-order snapshot failed for {ticker}: "
                                    f"{type(se).__name__}: {se}"
                                )

                            order_data = MarketOrderRequest(
                                symbol=ticker,
                                notional=round(size_usd, 2),
                                side=OrderSide.BUY,
                                time_in_force=TimeInForce.DAY
                            )

                            # Latency sensor: bracket submit_order in milliseconds
                            t_before   = time.time()
                            order_resp = trading_client.submit_order(order_data=order_data)
                            t_after    = time.time()
                            buy_latency_ms = round((t_after - t_before) * 1000, 2)

                            msg = f"BUY ${size_usd:.2f} notional of {ticker} (KAMA-BB-RSI signal)"
                            logging.info(msg)
                            messages.append(msg)

                            # Slippage sensor step 2: allow fill window then fetch average_fill_price
                            time.sleep(2.0)
                            buy_fill_price = None
                            actual_filled_qty = None
                            try:
                                filled_order = trading_client.get_order_by_id(str(order_resp.id))
                                if filled_order.filled_avg_price is not None:
                                    buy_fill_price = round(float(filled_order.filled_avg_price), 4)
                                if filled_order.filled_qty is not None:
                                    actual_filled_qty = float(filled_order.filled_qty)
                            except Exception as fe:
                                logging.warning(
                                    f"Could not fetch fill price for {ticker}: "
                                    f"{type(fe).__name__}: {fe}"
                                )

                            if buy_fill_price and signal_price > 0:
                                slippage_dollar = round(buy_fill_price - signal_price, 4)
                                slippage_pct    = round(
                                    (buy_fill_price - signal_price) / signal_price * 100, 4
                                )
                            else:
                                slippage_dollar = 0
                                slippage_pct    = 0

                            log_price         = signal_price if signal_price > 0 else (buy_fill_price or 0.0)
                            fractional_shares = actual_filled_qty if actual_filled_qty is not None else (size_usd / log_price if log_price > 0 else 0.0)

                            entry_atr          = indic["atr_14"]
                            stop_loss_target   = round(log_price - (2 * entry_atr), 2)
                            take_profit_target = round(log_price + (3 * entry_atr), 2)

                            logging.info(
                                f"Telemetry BUY {ticker} | latency={buy_latency_ms}ms  "
                                f"signal={signal_price}  fill={buy_fill_price}  filled_qty={fractional_shares}  "
                                f"slippage=${slippage_dollar} ({slippage_pct}%)  "
                                f"ATR={entry_atr:.2f}  SL={stop_loss_target}  TP={take_profit_target}"
                            )

                            port_val  = float(trading_client.get_account().portfolio_value)
                            trade_row = [
                                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                ticker,
                                "BUY",
                                log_price,
                                entry_atr,
                                stop_loss_target,
                                take_profit_target,
                                fractional_shares,
                                round(size_usd, 2),
                                "KAMA-BB-RSI",
                                round(indic["rsi_7"], 2),
                                round(current_vix, 2),
                                round(port_val, 2),
                                "0.00%",
                                "",
                                market_direction,
                                round(indic["lower_band"], 2),
                                round(indic["atr_14"], 2),
                                # Telemetry fields appended to master log
                                buy_latency_ms,
                                slippage_dollar,
                                slippage_pct,
                                "N/A",
                            ]
                            successful_trades.append(trade_row)
                            write_trade_to_csv(trade_row)

                        except Exception as e:
                            logging.error(f"Failed to buy {ticker}: {e}")
                    else:
                        logging.info(f"Insufficient funds to buy {ticker}.")
                else:
                    logging.info(f"No buy signal for {ticker}.")
        except Exception as e:
            logging.error(f"Error during buy scanning: {e}")

        # 6. Wrap up

        # Log trades to Google Sheets. Local CSV writes are done inline above for reboot resilience.
        if gc and successful_trades:
            # ── Ledger Integrity Loop ─────────────────────────────────────────
            # Open the worksheet by name (never by index) to survive tab reorders.
            # Retry indefinitely on timeout/connection errors — never skip a write.
            sheet1 = None
            while sheet1 is None:
                try:
                    sheet1 = gc.open('Aegis Trading Log').worksheet("Sheet1")
                except Exception as e:
                    logging.error(
                        f"Cannot open 'Sheet1', retrying in 5s: {type(e).__name__}: {e}"
                    )
                    time.sleep(5)

            for trade in successful_trades:
                logged = False
                while not logged:
                    try:
                        sheet1.append_row(trade)
                        logged = True
                    except Exception as e:
                        logging.error(
                            f"append_row failed for trade, retrying in 5s: "
                            f"{type(e).__name__}: {e}"
                        )
                        time.sleep(5)
            logging.info("Individual trades logged to Google Sheets.")
            # ─────────────────────────────────────────────────────────────────

        # Trade alert email, fires only when a trade executed this iteration.
        if successful_trades:
            send_email("Aegis Trade Alert", "\n".join(messages))

        # EOD Daily Recap, time-gated to 15:50-16:00 EST, fires once per calendar day.
        in_recap_window = (
            (now_ny.hour == 15 and now_ny.minute >= 50) or
            (now_ny.hour == 16 and now_ny.minute == 0)
        )
        if in_recap_window and now_ny.date() != last_recap_date:
            try:
                # Read the local CSV directly to count buys, bypassing the volatile RAM counter
                today_str  = now_ny.strftime('%Y-%m-%d')
                buys_today = count_buys_today(today_str)

                avg_rsi = sum(recap_rsi_list) / len(recap_rsi_list) if recap_rsi_list else 0
                closest_label = (
                    f"{closest_ticker} (margin: {round(closest_margin, 2)})"
                    if closest_ticker != "None" else "None"
                )
                recap_payload = [
                    now_ny.strftime('%Y-%m-%d %H:%M:%S'),
                    market_direction,
                    round(current_vix, 2),
                    buys_today,
                    closest_label,
                    reason_no_buy,
                    ", ".join(recap_blocked),
                    round(avg_rsi, 2),
                ]

                if gc:
                    # ── Ledger Integrity Loop (Daily Recap) ──────────────────
                    recap_sheet = None
                    while recap_sheet is None:
                        try:
                            recap_sheet = gc.open('Aegis Trading Log').worksheet("Daily Recap")
                        except Exception as e:
                            logging.error(
                                f"Cannot open 'Daily Recap' sheet, retrying in 5s: "
                                f"{type(e).__name__}: {e}"
                            )
                            time.sleep(5)
                    recap_logged = False
                    while not recap_logged:
                        try:
                            recap_sheet.append_row(recap_payload)
                            recap_logged = True
                            logging.info("Daily Recap logged to Google Sheets.")
                        except Exception as e:
                            logging.error(
                                f"Daily Recap append_row failed, retrying in 5s: "
                                f"{type(e).__name__}: {e}"
                            )
                            time.sleep(5)
                    # ────────────────────────────────────────────────────────

                recap_body = (
                    f"Aegis EOD Recap - {now_ny.strftime('%Y-%m-%d')}\n\n"
                    f"Market Direction : {market_direction}\n"
                    f"VIX              : {round(current_vix, 2)}\n"
                    f"Trades Today     : {buys_today}\n"
                    f"Closest Signal   : {closest_label}\n"
                    f"Reason No Buy    : {reason_no_buy}\n"
                    f"Blocked Tickers  : {', '.join(recap_blocked) if recap_blocked else 'None'}\n"
                    f"Avg RSI_7        : {round(avg_rsi, 2)}\n"
                )
                send_email("Aegis Daily Recap", recap_body)
                last_recap_date = now_ny.date()

            except Exception as e:
                logging.error(f"Failed to process EOD Daily Recap: {e}")

        time.sleep(300)


if __name__ == "__main__":
    run_scanner()
