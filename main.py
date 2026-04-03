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
            logging.info("Google Sheets initialized.")
        else:
            sheet = None
            logging.warning("Google Sheets credentials not found. Logging to sheets disabled.")
    except Exception as e:
        logging.error(f"Failed to initialize gspread: {e}")
        sheet = None

    while True:
        messages = []
        messages.append(f"Aegis Trading Bot Report - {datetime.now().strftime('%Y-%m-%d')}\n")

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
                            msg = f"SELL {position.qty} shares of {symbol} at limit ({reason})"
                        else:
                            order_data = MarketOrderRequest(
                                symbol=symbol,
                                qty=position.qty,
                                side=OrderSide.SELL,
                                time_in_force=TimeInForce.DAY
                            )
                            trading_client.submit_order(order_data=order_data)
                            msg = f"SELL {position.qty} shares of {symbol} at market ({reason})"
                            
                        logging.info(msg)
                        messages.append(msg)
                        successful_trades.append([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), symbol, "SELL", float(position.qty), reason])
                    except Exception as e:
                        logging.error(f"Failed to sell {symbol}: {e}")
                else:
                    logging.info(f"Holding {symbol} (PLPC: {unrealized_plpc:.2%}).")
        except Exception as e:
            logging.error(f"Error during sell management: {e}")

        # 4. Check VIX kill switch
        try:
            if risk_manager.check_vix_kill_switch():
                msg = "VIX > 30. Kill switch activated. Exiting without trading."
                logging.warning(msg)
                messages.append(msg)
                time.sleep(300)
                continue
        except Exception as e:
            logging.error(f"Error checking VIX kill switch: {e}")

        # 5. Scan for Buys
        tickers_to_scan = ['AAPL', 'AMZN', 'CAT', 'CL', 'GE', 'GOOGL', 'GS', 'JPM', 'LLY', 'META', 'MSFT', 'NOC', 'NVDA', 'RTX', 'UNH', 'WMT', 'XOM']

        try:
            positions = trading_client.get_all_positions()
            owned_tickers = {p.symbol for p in positions}

            for ticker in tickers_to_scan:
                if ticker in owned_tickers:
                    logging.info(f"Already own {ticker}. Skipping buy check.")
                    continue

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
                            msg = f"BUY {qty} shares of {ticker} at limit ${limit_price} (${size_usd:.2f} total)"
                            logging.info(msg)
                            messages.append(msg)
                            successful_trades.append([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ticker, "BUY", size_usd, "RSI Strategy"])
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

        if successful_trades:
            compiled_string = "\n".join(messages)
            send_email("Aegis Trade Alert", compiled_string)

        time.sleep(300)

if __name__ == "__main__":
    run_scanner()
