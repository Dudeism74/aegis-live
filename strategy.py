from datetime import datetime, timedelta
import pandas as pd
import numpy as np

def check_rsi_buy_signal(alpaca_client, symbol):
    """
    Fetches the last 20 days of daily closing prices for the given symbol,
    calculates the standard 14-day RSI, and returns True if the most recent
    RSI value is strictly below 30. Returns False if it is 30 or above.
    """
    try:
        # We need the last 20 trading days. Since weekends and holidays happen,
        # fetching a larger window of calendar days ensures we have enough data.
        end_date = datetime.now()
        start_date = end_date - timedelta(days=40)

        # In this project, Alpaca SDK is used. The interface depends on whether
        # alpaca-py or alpaca-trade-api is used. Let's try duck typing.
        try:
            # Try older/standard alpaca-trade-api
            bars = alpaca_client.get_bars(
                symbol,
                "1Day",
                start=start_date.strftime('%Y-%m-%d'),
                end=end_date.strftime('%Y-%m-%d')
            ).df
        except Exception:
            # Fallback to newer alpaca-py
            from alpaca.data.timeframe import TimeFrame
            from alpaca.data.requests import StockBarsRequest

            req = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Day,
                start=start_date,
                end=end_date
            )
            bars = alpaca_client.get_stock_bars(req).df

        # Ensure we have the last 20 daily closing prices
        bars = bars.tail(20)

        # We need at least 15 days of data to compute a 14-day RSI
        # (1 day for initial difference, 14 days for the first average)
        if len(bars) < 15:
            return False

        close_prices = bars['close']

        # Calculate daily returns (differences)
        delta = close_prices.diff()

        # Separate gains and losses
        gain = (delta.where(delta > 0, 0)).fillna(0)
        loss = (-delta.where(delta < 0, 0)).fillna(0)

        # Standard Wilder's RSI smoothing calculation
        # Initial average gain and loss (SMA over 14 days)
        avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()

        # Calculate RS
        rs = avg_gain / avg_loss

        # Calculate RSI
        rsi = 100 - (100 / (1 + rs))

        # Deal with edge case where avg_loss is 0
        rsi = rsi.fillna(100)

        # Get the most recent RSI value
        current_rsi = rsi.iloc[-1]

        if current_rsi < 30:
            return True
        else:
            return False

    except Exception as e:
        # Handle any API connection errors or other exceptions
        print(f"Error computing RSI for {symbol}: {e}")
        return False
