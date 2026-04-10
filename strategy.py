from datetime import datetime, timedelta
import pandas as pd
import numpy as np
import ta
from alpaca.data.timeframe import TimeFrame
from alpaca.data.requests import StockBarsRequest
from alpaca.data.enums import DataFeed

def check_rsi_buy_signal(data_client, symbol):
    """
    Fetches the last 100 days of daily closing prices for the given symbol,
    calculates the 20-day SMA and 14-day RSI. Returns True ONLY if:
    1) Current price is > 20-day SMA
    2) 14-day RSI is < 55
    3) Current price > previous day's close (bounce confirmation)
    """
    try:
        # We need at least 20 trading days for the 20-day SMA.
        # Fetching 100 calendar days ensures we have enough data.
        end_date = datetime.now()
        start_date = end_date - timedelta(days=100)

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

        # We need at least 20 days of data to compute a 20-day SMA
        if len(bars) < 20:
            return False

        close_prices = bars['close']

        # Calculate 20-day SMA using ta library
        sma_20 = ta.trend.SMAIndicator(close=close_prices, window=20).sma_indicator()

        # Calculate 14-day RSI using ta library
        rsi_14 = ta.momentum.RSIIndicator(close=close_prices, window=14).rsi()

        current_price = close_prices.iloc[-1]
        previous_price = close_prices.iloc[-2]
        current_sma = sma_20.iloc[-1]
        current_rsi = rsi_14.iloc[-1]

        # Handle edge cases where values might be NaN
        if pd.isna(current_sma) or pd.isna(current_rsi):
            return False

        # Evaluate the conditions:
        # 1) Current price > 20-day SMA
        # 2) 14-day RSI < 55
        # 3) Current price > previous day's close
        is_buy = current_price > current_sma and current_rsi < 55 and current_price > previous_price

        import logging
        if is_buy:
            logging.info(f"Ticker: {symbol} | Current RSI: {current_rsi:.1f} | Action: BUY (Threshold: < 55)")
        else:
            logging.info(f"Ticker: {symbol} | Current RSI: {current_rsi:.1f} | Action: HOLD (Threshold: < 55)")

        return is_buy

    except Exception as e:
        # Handle any API connection errors or other exceptions
        print(f"Error computing indicators for {symbol}: {e}")
        return False


def get_current_rsi(data_client, symbol):
    """Returns the current 14-day RSI for the given symbol, or None on error."""
    try:
        end_date = datetime.now()
        start_date = end_date - timedelta(days=100)

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

        if len(bars) < 14:
            return None

        close_prices = bars['close']
        rsi_14 = ta.momentum.RSIIndicator(close=close_prices, window=14).rsi()
        current_rsi = rsi_14.iloc[-1]
        return round(float(current_rsi), 2) if not pd.isna(current_rsi) else None
    except Exception as e:
        print(f"Error computing RSI for {symbol}: {e}")
        return None


def get_market_context(data_client, symbol):
    """Returns market context metrics: distance from 20/50 SMAs and consecutive days RSI below 50."""
    try:
        end_date = datetime.now()
        start_date = end_date - timedelta(days=150)

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

        if len(bars) < 50:
            return None

        close_prices = bars['close']
        current_price = close_prices.iloc[-1]

        sma_20 = ta.trend.SMAIndicator(close=close_prices, window=20).sma_indicator()
        sma_50 = ta.trend.SMAIndicator(close=close_prices, window=50).sma_indicator()
        rsi_14 = ta.momentum.RSIIndicator(close=close_prices, window=14).rsi()

        current_sma_20 = sma_20.iloc[-1]
        current_sma_50 = sma_50.iloc[-1]

        if pd.isna(current_sma_20) or pd.isna(current_sma_50):
            return None

        dist_sma_20 = ((current_price - current_sma_20) / current_sma_20) * 100
        dist_sma_50 = ((current_price - current_sma_50) / current_sma_50) * 100

        # Count consecutive days RSI has been below 50 (from most recent day backwards)
        consecutive_rsi_below_50 = 0
        rsi_values = rsi_14.dropna()
        for val in reversed(rsi_values.values):
            if val < 50:
                consecutive_rsi_below_50 += 1
            else:
                break

        return {
            'dist_sma_20': round(dist_sma_20, 2),
            'dist_sma_50': round(dist_sma_50, 2),
            'consecutive_rsi_below_50': consecutive_rsi_below_50
        }
    except Exception as e:
        print(f"Error computing market context for {symbol}: {e}")
        return None


def get_spy_direction(data_client):
    """Returns 'UP' or 'DOWN' based on whether SPY is above or below yesterday's close."""
    try:
        end_date = datetime.now()
        start_date = end_date - timedelta(days=10)

        req = StockBarsRequest(
            symbol_or_symbols='SPY',
            timeframe=TimeFrame.Day,
            start=start_date,
            end=end_date,
            feed=DataFeed.IEX
        )
        bars = data_client.get_stock_bars(req).df

        if isinstance(bars.index, pd.MultiIndex):
            bars = bars.xs('SPY', level=0)

        if len(bars) < 2:
            return 'UNKNOWN'

        current_price = bars['close'].iloc[-1]
        previous_close = bars['close'].iloc[-2]

        return 'UP' if current_price > previous_close else 'DOWN'
    except Exception as e:
        print(f"Error computing SPY direction: {e}")
        return 'UNKNOWN'
