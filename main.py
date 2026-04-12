import os
import sys
import time
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone
import gspread
import json
try:
    import zoneinfo
except ImportError:
    from backports import zoneinfo

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest, LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockSnapshotRequest

import risk_manager
import portfolio
import strategy
from dotenv import load_dotenv

env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
load_dotenv(dotenv_path=env_path)

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def send_email(subject, body):
    sender_email = os.environ.get("SENDER_EMAIL")
    sender_password = os.environ.get("SENDER_PASSWORD")
    recipient_email = os.environ.get("RECIPIENT_EMAIL")

    if not sender_email or not sender_password or not recipient_email:
        logging.warning("Email credentials not set. Skipping email.")
        return

    try:
        msg = MIMEMultipart()
        msg['From'] = sender_email
        msg['To'] = recipient_email
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))

        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(sender_email, sender_password)
        text = msg.as_string()
        server.sendmail(sender_email, recipient_email, text)
        server.quit()
        logging.info("Email sent successfully.")
    except Exception as e:
        logging.error(f"Failed to send email: {e}")

def write_daily_recap(gc, data_client, market_direction, daily_trade_count, tickers_to_scan):
    """Write a daily recap row to the 'Daily Recap' tab after market close."""
    try:
        spreadsheet = gc.open('Aegis Trading Log')

        # Get or create the Daily Recap tab
        try:
            recap_sheet = spreadsheet.worksheet('Daily Recap')
        except gspread.exceptions.WorksheetNotFound:
            recap_sheet = spreadsheet.add_worksheet(title='Daily Recap', rows=1000, cols=8)
            headers = [
                "Date", "Market Direction", "VIX Level", "Trades Executed",
                "Closest to Buy", "Reason Not Triggered",
                "SMA-Blocked Tickers", "Avg RSI (All 17)"
            ]
            recap_sheet.update('A1:H1', [headers])
            logging.info("Created 'Daily Recap' tab with headers.")

        vix_level = risk_manager.get_vix_level()

        all_rsi = []
        near_misses = []
        sma_blocked = []

        for ticker in tickers_to_scan:
            details = strategy.get_signal_details(data_client, ticker)
            if details is None:
                continue

            all_rsi.append(details['rsi'])

            if details['rsi'] < 55 and not details['is_buy']:
                if not details['above_sma_20']:
                    reason = "below 20-SMA"
                    sma_blocked.append(ticker)
                else:
                    reason = "below previous close"
                near_misses.append((ticker, details['rsi'], reason))

        closest_ticker = ""
        closest_reason = ""
        if near_misses:
            near_misses.sort(key=lambda x: x[1])
            closest_ticker = f"{near_misses[0][0]} (RSI: {near_misses[0][1]})"
            closest_reason = near_misses[0][2]

        avg_rsi = round(sum(all_rsi) / len(all_rsi), 2) if all_rsi else ""
        sma_blocked_str = ", ".join(sma_blocked) if sma_blocked else ""

        try:
            ny_tz = zoneinfo.ZoneInfo("America/New_York")
        except Exception:
            ny_tz = timezone.utc
        today_str = datetime.now(ny_tz).strftime("%Y-%m-%d")

        row = [
            today_str,
            market_direction,
            vix_level if vix_level is not None else "",
            daily_trade_count,
            closest_ticker,
            closest_reason,
            sma_blocked_str,
            avg_rsi
        ]

        recap_sheet.append_row(row)
        logging.info("Daily recap written to Google Sheets.")
    except Exception as e:
        logging.error(f"Failed to write daily recap: {e}")


def run_scanner():
    # 1. Initialize Alpaca trading client, gspread
    try:
        api_key = os.environ.get("APCA_API_KEY_ID", "dummy_key")
        api_secret = os.environ.get("APCA_API_SECRET_KEY", "dummy_secret")
        trading_client = TradingClient(api_key, api_secret, paper=True)
        data_client = StockHistoricalDataClient(api_key, api_secret)
        logging.info("Alpaca Trading Client and Data Client initialized.")
    except Exception as e:
        msg = f"Failed to initialize Alpaca Clients: {e}"
        logging.error(msg)
        messages = [msg]
        send_email("Aegis Trading Error", "\n".join(messages))
        sys.exit(1)

    try:
        # Initialize Google Sheets
        gc = None
        cred_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'credentials.json')
        gc = gspread.service_account(filename=cred_path)

        if gc:
            sheet = gc.open('Aegis Trading Log').sheet1
            # Ensure headers are set
            expected_headers = [
                "Date/Time", "Ticker", "Action", "Price", "Shares",
                "Total Value", "Reason", "RSI", "VIX Level",
                "Portfolio Value", "P&L", "Result",
                "Market Direction", "Dist 20-SMA %", "Dist 50-SMA %",
                "Consec Days RSI<50"
            ]
            try:
                current_headers = sheet.row_values(1)
                if current_headers != expected_headers:
                    sheet.update('A1:P1', [expected_headers])
                    logging.info("Google Sheets headers updated.")
            except Exception as e:
                logging.error(f"Failed to update sheet headers: {e}")
            logging.info("Google Sheets initialized.")
        else:
            sheet = None
            logging.warning("Google Sheets credentials not found. Logging to sheets disabled.")
    except Exception as e:
        logging.error(f"Failed to initialize gspread: {e}")
        sheet = None

    # Ticker universe (constant)
    tickers_to_scan = ['AAPL', 'AMZN', 'CAT', 'CL', 'GE', 'GOOGL', 'GS', 'JPM', 'LLY', 'META', 'MSFT', 'NOC', 'NVDA', 'RTX', 'UNH', 'WMT', 'XOM']

    # Daily recap tracking
    daily_trade_count = 0
    recap_written_date = None
    market_open_today = False
    last_market_direction = "UNKNOWN"
    current_tracking_date = None

    while True:
        # Track date transitions for daily reset
        try:
            ny_tz = zoneinfo.ZoneInfo("America/New_York")
        except Exception:
            ny_tz = timezone.utc
        now_ny = datetime.now(ny_tz)
        today_date = now_ny.date()
        if current_tracking_date != today_date:
            current_tracking_date = today_date
            daily_trade_count = 0
            market_open_today = False
            last_market_direction = "UNKNOWN"

        messages = []
        messages.append(f"Aegis Trading Bot Report - {datetime.now().strftime('%Y-%m-%d')}\n")

        # Log portfolio snapshot
        try:
            account = trading_client.get_account()
            logging.info(
                f"Portfolio | Value: ${float(account.portfolio_value):,.2f} | "
                f"Cash: ${float(account.cash):,.2f} | "
                f"Buying Power: ${float(account.buying_power):,.2f} | "
                f"Equity: ${float(account.equity):,.2f}"
            )
        except Exception as e:
            logging.error(f"Failed to log portfolio snapshot: {e}")

        # 2. Check if market is open
        try:
            clock = trading_client.get_clock()
            if not clock.is_open:
                # Check for daily recap at 4:05 PM ET
                recap_time = now_ny.replace(hour=16, minute=5, second=0, microsecond=0)
                if (market_open_today
                        and now_ny >= recap_time
                        and recap_written_date != today_date
                        and gc):
                    write_daily_recap(gc, data_client, last_market_direction,
                                     daily_trade_count, tickers_to_scan)
                    recap_written_date = today_date

                logging.info("Market Closed - Sleeping")
                time.sleep(300)
                continue
        except Exception as e:
            logging.error(f"Failed to check market status: {e}")
            time.sleep(300)
            continue

        market_open_today = True

        # Check SPY market direction
        market_direction = strategy.get_spy_direction(data_client)
        last_market_direction = market_direction
        logging.info(f"Market Direction: {market_direction}")

        # Variables to track actions
        successful_trades = []

        # 3. Manage Sells
        try:
            positions = trading_client.get_all_positions()

            # Get today's filled orders for PDT shield
            # Use US/Eastern time for PDT calculation
            try:
                ny_tz = zoneinfo.ZoneInfo("America/New_York")
            except Exception:
                # Fallback if zoneinfo is not available or tzdata is missing
                ny_tz = timezone.utc

            now_ny = datetime.now(ny_tz)
            today_ny = now_ny.replace(hour=0, minute=0, second=0, microsecond=0)

            # Alpaca API expects UTC for timestamps
            today_utc = today_ny.astimezone(timezone.utc)

            req = GetOrdersRequest(
                status=QueryOrderStatus.CLOSED,
                limit=500,
                after=today_utc
            )
            recent_orders = trading_client.get_orders(req)

            # Identify tickers bought today
            bought_today = set()
            for order in recent_orders:
                if order.side == OrderSide.BUY and order.filled_at and order.filled_at >= today_utc:
                    bought_today.add(order.symbol)

            for position in positions:
                symbol = position.symbol
                if symbol in bought_today:
                    logging.info(f"PDT Shield: {symbol} was bought today. Skipping sell check.")
                    continue

                unrealized_plpc = float(position.unrealized_plpc)

                if unrealized_plpc >= 0.10 or unrealized_plpc <= -0.05:
                    reason = "Take Profit" if unrealized_plpc >= 0.10 else "Stop Loss"
                    logging.info(f"Triggering {reason} for {symbol} (PLPC: {unrealized_plpc:.2%}).")
                    try:
                        sell_qty = float(position.qty)
                        avg_entry_price = float(position.avg_entry_price)
                        sell_rsi = strategy.get_current_rsi(data_client, symbol)
                        sell_vix = risk_manager.get_vix_level()
                        sell_portfolio_value = float(trading_client.get_account().portfolio_value)
                        sell_ctx = strategy.get_market_context(data_client, symbol)

                        if unrealized_plpc >= 0.10:
                            snapshot = data_client.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=symbol))[symbol]
                            current_price = snapshot.latest_trade.price
                            limit_price = round(current_price * 0.995, 2)

                            order_data = LimitOrderRequest(
                                symbol=symbol,
                                qty=position.qty,
                                side=OrderSide.SELL,
                                time_in_force=TimeInForce.DAY,
                                limit_price=limit_price
                            )
                            trading_client.submit_order(order_data=order_data)
                            sell_price = float(limit_price)
                            msg = f"SELL {position.qty} shares of {symbol} at limit ({reason})"
                        else:
                            snapshot = data_client.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=symbol))[symbol]
                            sell_price = float(snapshot.latest_trade.price)

                            order_data = MarketOrderRequest(
                                symbol=symbol,
                                qty=position.qty,
                                side=OrderSide.SELL,
                                time_in_force=TimeInForce.DAY
                            )
                            trading_client.submit_order(order_data=order_data)
                            msg = f"SELL {position.qty} shares of {symbol} at market ({reason})"

                        total_value = round(sell_price * sell_qty, 2)
                        pnl = round((sell_price - avg_entry_price) * sell_qty, 2)
                        result = "Win" if pnl >= 0 else "Loss"

                        logging.info(msg)
                        messages.append(msg)
                        successful_trades.append([
                            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            symbol, "SELL", sell_price, sell_qty,
                            total_value, reason,
                            sell_rsi if sell_rsi is not None else "",
                            sell_vix if sell_vix is not None else "",
                            round(sell_portfolio_value, 2),
                            pnl, result,
                            market_direction,
                            sell_ctx['dist_sma_20'] if sell_ctx else "",
                            sell_ctx['dist_sma_50'] if sell_ctx else "",
                            sell_ctx['consecutive_rsi_below_50'] if sell_ctx else ""
                        ])
                    except Exception as e:
                        logging.error(f"Failed to sell {symbol}: {e}")
                else:
                    logging.info(f"Holding {symbol} (PLPC: {unrealized_plpc:.2%}).")
        except Exception as e:
            logging.error(f"Error during sell management: {e}")

        # 4. Check VIX kill switch
        try:
            if risk_manager.check_vix_kill_switch():
                msg = "VIX > 35. Kill switch activated. Exiting without trading."
                logging.warning(msg)
                messages.append(msg)
                time.sleep(300)
                continue
        except Exception as e:
            logging.error(f"Error checking VIX kill switch: {e}")

        # 5. Scan for Buys
        try:
            positions = trading_client.get_all_positions()
            owned_tickers = {p.symbol for p in positions}

            for ticker in tickers_to_scan:
                if ticker in owned_tickers:
                    logging.info(f"Already own {ticker}. Skipping buy check.")
                    continue

                ticker_ctx = strategy.get_market_context(data_client, ticker)
                if ticker_ctx:
                    logging.info(
                        f"Context for {ticker} | Dist 20-SMA: {ticker_ctx['dist_sma_20']:.2f}% | "
                        f"Dist 50-SMA: {ticker_ctx['dist_sma_50']:.2f}% | "
                        f"Consec RSI<50: {ticker_ctx['consecutive_rsi_below_50']}d"
                    )

                if strategy.check_rsi_buy_signal(data_client, ticker):
                    logging.info(f"Buy signal triggered for {ticker}.")
                    size_usd = portfolio.calculate_position_size(trading_client)

                    if size_usd > 0:
                        try:
                            snapshot = data_client.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=ticker))[ticker]
                            current_price = snapshot.latest_trade.price
                            limit_price = round(current_price * 1.005, 2)
                            qty = round(size_usd / current_price, 4)

                            order_data = LimitOrderRequest(
                                symbol=ticker,
                                qty=qty,
                                side=OrderSide.BUY,
                                time_in_force=TimeInForce.DAY,
                                limit_price=limit_price
                            )
                            trading_client.submit_order(order_data=order_data)

                            buy_price = float(limit_price)
                            total_value = round(buy_price * qty, 2)
                            buy_rsi = strategy.get_current_rsi(data_client, ticker)
                            buy_vix = risk_manager.get_vix_level()
                            buy_portfolio_value = float(trading_client.get_account().portfolio_value)

                            msg = f"BUY {qty} shares of {ticker} at limit ${limit_price} (${size_usd:.2f} total)"
                            logging.info(msg)
                            messages.append(msg)
                            successful_trades.append([
                                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                ticker, "BUY", buy_price, qty,
                                total_value, "RSI Strategy",
                                buy_rsi if buy_rsi is not None else "",
                                buy_vix if buy_vix is not None else "",
                                round(buy_portfolio_value, 2),
                                "", "",
                                market_direction,
                                ticker_ctx['dist_sma_20'] if ticker_ctx else "",
                                ticker_ctx['dist_sma_50'] if ticker_ctx else "",
                                ticker_ctx['consecutive_rsi_below_50'] if ticker_ctx else ""
                            ])
                        except Exception as e:
                            logging.error(f"Failed to buy {ticker}: {e}")
                    else:
                        logging.info(f"Insufficient funds to buy {ticker}.")
                else:
                    logging.info(f"No buy signal for {ticker}.")
        except Exception as e:
            logging.error(f"Error during buy scanning: {e}")

        # 6. Wrap up
        try:
            if sheet and successful_trades:
                for trade in successful_trades:
                    sheet.append_row(trade)
                logging.info("Trades logged to Google Sheets.")
        except Exception as e:
            logging.error(f"Failed to log to Google Sheets: {e}")

        daily_trade_count += len(successful_trades)

        if successful_trades:
            compiled_string = "\n".join(messages)
            send_email("Aegis Trade Alert", compiled_string)

        time.sleep(300)

if __name__ == "__main__":
    run_scanner()
