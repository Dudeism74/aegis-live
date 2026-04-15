from datetime import datetime, timedelta
import pandas as pd
import numpy as np
import ta
from alpaca.data.timeframe import TimeFrame
from alpaca.data.requests import StockBarsRequest
from alpaca.data.enums import DataFeed

def check_rsi_buy_signal(data_client, symbol):
    """
    Fetches the last 400 days of daily closing prices for the given symbol,
    calculates the 200-day SMA and 14-day RSI. Returns True ONLY if:
    1) Current price is > 200-day SMA
    2) 14-day RSI is < 40
    3) Current price > previous day's close (bounce confirmation)
    """
    try:
        # We need at least 200 trading days for the 200-day SMA.
        # Fetching 400 calendar days ensures we have enough data.
        end_date = datetime.now()
        start_date = end_date - timedelta(days=400)

        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=start_date,
            end=end_date,
            feed=DataFeed.IEX
        )
        bars = data_client.get_stock_bars(req).df

        if isinstance(bars.index, pd.MultiIndex):
            bars = bars.xs(symbol, level=0)

        # We need at least 200 days of data to compute a 200-day SMA
        if len(bars) < 200:
            return False

        close_prices = bars['close']

        # Calculate 200-day SMA using ta library
        sma_200 = ta.trend.SMAIndicator(close=close_prices, window=200).sma_indicator()

        # Calculate 14-day RSI using ta library
        rsi_14 = ta.momentum.RSIIndicator(close=close_prices, window=14).rsi()

        current_price = close_prices.iloc[-1]
        previous_price = close_prices.iloc[-2]
        current_sma = sma_200.iloc[-1]
        current_rsi = rsi_14.iloc[-1]

        # Handle edge cases where values might be NaN
        if pd.isna(current_sma) or pd.isna(current_rsi):
            return False

        # Evaluate the conditions:
        # 1) Current price > 200-day SMA
        # 2) 14-day RSI < 40
        # 3) Current price > previous day's close
        is_buy = current_price > current_sma and current_rsi < 40 and current_price > previous_price

        import logging
        if is_buy:
            logging.info(f"Ticker: {symbol} | Current RSI: {current_rsi:.1f} | Action: BUY (Threshold: < 40)")
        else:
            logging.info(f"Ticker: {symbol} | Current RSI: {current_rsi:.1f} | Action: HOLD (Threshold: < 40)")

        return is_buy

    except Exception as e:
        # Handle any API connection errors or other exceptions
        print(f"Error computing indicators for {symbol}: {e}")
        return False
