import os
import sys
import logging

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, StopLossRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest

import risk_manager
import portfolio
import strategy

# Basic logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('AegisBot')

def main():
    logger.info("Starting Aegis trading bot...")

    # 1. Initialize the Alpaca trading client
    api_key = os.environ.get('APCA_API_KEY_ID', 'dummy_key')
    secret_key = os.environ.get('APCA_API_SECRET_KEY', 'dummy_secret')

    # Assuming paper trading for safety, could be driven by another env var
    client = TradingClient(api_key, secret_key, paper=True)

    # Also need a data client to fetch current market prices
    data_client = StockHistoricalDataClient(api_key, secret_key)

    # 2. Check VIX kill switch
    logger.info("Checking VIX kill switch...")
    if risk_manager.check_vix_kill_switch():
        logger.warning("VIX is above 30! Kill switch activated. Exiting script entirely.")
        sys.exit(0)

    # 3. Calculate position size
    logger.info("VIX is safe. Proceeding with trading.")
    logger.info("Calculating target buy amount...")
    target_amount = portfolio.calculate_position_size(client)

    if target_amount == 0:
        logger.error("Insufficient funds or error in calculating position size. Exiting.")
        sys.exit(0)

    logger.info(f"Target buy amount per position: ${target_amount:.2f}")

    # 4. List of 17 tickers
    tickers = [
        "AAPL", "AMZN", "CAT", "CL", "GE", "GOOGL", "GS", "JPM",
        "LLY", "META", "MSFT", "NOC", "NVDA", "RTX", "UNH", "WMT", "XOM"
    ]

    # 5. Loop through tickers
    for ticker in tickers:
        logger.info(f"Checking RSI buy signal for {ticker}...")

        if strategy.check_rsi_buy_signal(client, ticker):
            logger.info(f"BUY SIGNAL detected for {ticker}!")

            try:
                # Fetch the current market price
                req = StockLatestTradeRequest(symbol_or_symbols=ticker)
                latest_trade = data_client.get_stock_latest_trade(req)
                current_price = latest_trade[ticker].price

                logger.info(f"Current market price for {ticker}: ${current_price:.2f}")

                # We are doing a fractional market buy order for the target dollar amount.
                # Alpaca supports notional orders for market buys.
                # Stop loss requires stop price = current_price * 0.95
                stop_price = round(current_price * 0.95, 2)
                logger.info(f"Setting stop loss at 5% below current price: ${stop_price:.2f}")

                # Place bracket order
                stop_loss = StopLossRequest(stop_price=stop_price)

                market_order_data = MarketOrderRequest(
                    symbol=ticker,
                    notional=target_amount,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                    stop_loss=stop_loss
                )

                client.submit_order(order_data=market_order_data)
                logger.info(f"Successfully submitted market buy bracket order for {ticker}.")

            except Exception as e:
                logger.error(f"Error placing order for {ticker}: {e}")

        else:
            logger.info(f"No buy signal for {ticker}.")

    logger.info("Finished processing all tickers. Bot execution complete.")

if __name__ == "__main__":
    main()
