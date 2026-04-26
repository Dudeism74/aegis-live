import os
import sys
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

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


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
        cred_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'credentials.json')
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

        # Resolve NY timezone once per iteration — used for PDT shield and recap gate.
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

        # Recap accumulators — populated in step 5, consumed in step 6.
        recap_rsi_list = []
        recap_blocked  = []
        closest_margin = float('inf')
        closest_ticker = "None"
        reason_no_buy  = "N/A"

        # 3. Manage Sells — dynamic ATR exits
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

                atr_14            = indic["atr_14"]
                avg_entry_price   = float(position.avg_entry_price)
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
                    reason = "Take Profit (3×ATR)" if take_profit else "Stop Loss (2×ATR)"
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
                        trading_client.submit_order(order_data=order_data)
                        msg = f"SELL {position.qty} shares of {symbol} at market ({reason})"
                        logging.info(msg)
                        messages.append(msg)

                        unrealized_plpc = float(position.unrealized_plpc)
                        port_val        = float(trading_client.get_account().portfolio_value)

                        successful_trades.append([
                            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            symbol,
                            "SELL",
                            round(current_price, 2),
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
                        ])
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

        # 5. Scan for Buys — fractional notional market orders
        tickers_to_scan = [
            'TSLA', 'NVDA', 'AMD', 'PLTR', 'COIN', 'MSTR', 'SMCI', 'CRWD',
            'SNOW', 'SHOP', 'ROKU', 'SQ', 'META', 'NFLX', 'AMZN', 'UBER', 'DASH'
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
                    size_usd = portfolio.calculate_position_size(trading_client)

                    if size_usd > 0:
                        try:
                            order_data = MarketOrderRequest(
                                symbol=ticker,
                                notional=round(size_usd, 2),
                                side=OrderSide.BUY,
                                time_in_force=TimeInForce.DAY
                            )
                            trading_client.submit_order(order_data=order_data)
                            msg = f"BUY ${size_usd:.2f} notional of {ticker} (KAMA-BB-RSI signal)"
                            logging.info(msg)
                            messages.append(msg)

                            try:
                                snapshot  = data_client.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=ticker))[ticker]
                                log_price = round(snapshot.latest_trade.price, 2)
                            except Exception:
                                log_price = 0.0

                            port_val = float(trading_client.get_account().portfolio_value)
                            successful_trades.append([
                                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                ticker,
                                "BUY",
                                log_price,
                                round(size_usd, 2),
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

        # Log individual trades to Google Sheets on every iteration that has trades.
        if gc and successful_trades:
            try:
                sheet1 = gc.open('Aegis Trading Log').sheet1
                for trade in successful_trades:
                    sheet1.append_row(trade)
                logging.info("Individual trades logged to Google Sheets.")
            except Exception as e:
                logging.error(f"Failed to log trades to Google Sheets: {e}")

        # Trade alert email — fires only when a trade executed this iteration.
        if successful_trades:
            send_email("Aegis Trade Alert", "\n".join(messages))

        # EOD Daily Recap — time-gated to 15:50–16:00 EST, fires once per calendar day.
        in_recap_window = (now_ny.hour == 15 and now_ny.minute >= 50) or (now_ny.hour == 16 and now_ny.minute == 0)
        if in_recap_window and now_ny.date() != last_recap_date:
            try:
                avg_rsi = sum(recap_rsi_list) / len(recap_rsi_list) if recap_rsi_list else 0
                closest_label = (
                    f"{closest_ticker} (margin: {round(closest_margin, 2)})"
                    if closest_ticker != "None" else "None"
                )
                recap_payload = [
                    now_ny.strftime('%Y-%m-%d %H:%M:%S'),
                    market_direction,
                    round(current_vix, 2),
                    len(successful_trades),
                    closest_label,
                    reason_no_buy,
                    ", ".join(recap_blocked),
                    round(avg_rsi, 2),
                ]

                if gc:
                    try:
                        recap_sheet = gc.open('Aegis Trading Log').worksheet("Daily Recap")
                        recap_sheet.append_row(recap_payload)
                        logging.info("Daily Recap logged to Google Sheets.")
                    except Exception as e:
                        logging.error(f"Failed to log Daily Recap to Google Sheets: {e}")

                recap_body = (
                    f"Aegis EOD Recap — {now_ny.strftime('%Y-%m-%d')}\n\n"
                    f"Market Direction : {market_direction}\n"
                    f"VIX              : {round(current_vix, 2)}\n"
                    f"Trades Today     : {len(successful_trades)}\n"
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
