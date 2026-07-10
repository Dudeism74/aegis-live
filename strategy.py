import time
import logging
from datetime import datetime, timedelta

import pandas as pd
import ta
from alpaca.data.timeframe import TimeFrame
from alpaca.data.requests import StockBarsRequest
from alpaca.data.enums import DataFeed, Adjustment


def check_rsi_buy_signal(data_client, symbol):
    """
    Fetches 150 calendar days of split/dividend-adjusted daily bars for `symbol`,
    then evaluates three indicators:

      KAMA(21, fast=2, slow=30)   — macro trend filter
      BB-RSI(7 / SMA-14 / STD-14) — dynamic momentum band
      ATR(14)                      — volatility base for position sizing

    BUY signal requires ALL three gates simultaneously:
      1. Close > KAMA          (trend intact)
      2. RSI_7 < Lower Band    (statistically oversold)
      3. Close > Open          (intraday bounce confirmation)

    Returns dict: {is_buy, rsi_7, lower_band, atr_14}
    Returns None on insufficient data or unrecoverable error.
    """
    try:
        time.sleep(1.5)  # rate-limit courtesy delay

        end_date = datetime.now()
        start_date = end_date - timedelta(days=150)

        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=start_date,
            end=end_date,
            feed=DataFeed.IEX,
            adjustment=Adjustment.ALL
        )
        bars = data_client.get_stock_bars(req).df

        if isinstance(bars.index, pd.MultiIndex):
            bars = bars.xs(symbol, level=0)

        if len(bars) < 30:
            logging.warning(f"{symbol}: only {len(bars)} bars returned — insufficient for indicators.")
            return None

        close = bars['close']
        high  = bars['high']
        low   = bars['low']
        open_ = bars['open']

        # --- Indicator 1: KAMA(21, fast=2, slow=30) ---
        kama = ta.momentum.KAMAIndicator(close=close, window=21, pow1=2, pow2=30).kama()

        # --- Indicator 2: BB-RSI ---
        rsi_7      = ta.momentum.RSIIndicator(close=close, window=7).rsi()
        rsi_sma    = rsi_7.rolling(window=14).mean()
        rsi_std    = rsi_7.rolling(window=14).std()
        lower_band = rsi_sma - (1.25 * rsi_std)

        # --- Indicator 3: ATR(14) ---
        atr_14 = ta.volatility.AverageTrueRange(
            high=high, low=low, close=close, window=14
        ).average_true_range()

        current_close      = close.iloc[-1]
        current_open       = open_.iloc[-1]
        current_kama       = kama.iloc[-1]
        current_rsi_7      = rsi_7.iloc[-1]
        current_lower_band = lower_band.iloc[-1]
        current_atr        = atr_14.iloc[-1]

        if any(
            pd.isna(v)
            for v in [current_kama, current_rsi_7, current_lower_band, current_atr]
        ):
            logging.warning(f"{symbol}: NaN in one or more indicators — skipping.")
            return None

        # --- Logic Gates ---
        above_kama      = current_close > current_kama
        rsi_oversold    = current_rsi_7 < current_lower_band
        intraday_bounce = current_close > current_open
        # Normalize NumPy boolean scalars so callers and serialized telemetry
        # always receive a regular Python bool.
        is_buy          = bool(above_kama and rsi_oversold and intraday_bounce)

        if is_buy:
            logging.info(
                f"{symbol} | BUY  | Close={current_close:.2f}  KAMA={current_kama:.2f}  "
                f"RSI_7={current_rsi_7:.2f}  LowerBand={current_lower_band:.2f}  ATR={current_atr:.2f}"
            )
        else:
            logging.info(
                f"{symbol} | HOLD | above_kama={above_kama}  rsi_oversold={rsi_oversold}  "
                f"bounce={intraday_bounce} | RSI_7={current_rsi_7:.2f}  LowerBand={current_lower_band:.2f}"
            )

        return {
            "is_buy":     is_buy,
            "rsi_7":      float(current_rsi_7),
            "lower_band": float(current_lower_band),
            "atr_14":     float(current_atr),
        }

    except Exception as e:
        logging.error(f"Error computing indicators for {symbol}: {e}")
        return None
