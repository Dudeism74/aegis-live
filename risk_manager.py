import time
import logging
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from alpaca.data.timeframe import TimeFrame
from alpaca.data.requests import StockBarsRequest
from alpaca.data.enums import DataFeed, Adjustment


def _fetch_bars(data_client, symbol, days=60, retries=3):
    """
    Fetch `days` calendar days of daily bars for `symbol` via the Alpaca
    StockHistoricalDataClient. Retries up to `retries` times with a 2-second
    pause on each failure, logging the exact exception type and message so
    failures are diagnosable. Returns a single-level DataFrame indexed by
    timestamp, or raises RuntimeError after all attempts are exhausted.
    """
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            end   = datetime.now()
            start = end - timedelta(days=days)
            req   = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Day,
                start=start,
                end=end,
                feed=DataFeed.IEX,
                adjustment=Adjustment.ALL,
            )
            bars = data_client.get_stock_bars(req).df
            if isinstance(bars.index, pd.MultiIndex):
                bars = bars.xs(symbol, level=0)
            if bars.empty:
                raise ValueError(f"Empty bar DataFrame returned for '{symbol}'")
            return bars
        except Exception as e:
            last_exc = e
            logging.warning(
                f"Alpaca bar fetch attempt {attempt}/{retries} for '{symbol}' failed — "
                f"{type(e).__name__}: {e}"
            )
            if attempt < retries:
                time.sleep(2)

    raise RuntimeError(
        f"All {retries} Alpaca bar fetch attempts exhausted for '{symbol}'"
    ) from last_exc


def get_vix(data_client):
    """
    Returns a VIX-proxy value: the 20-day annualized realized volatility of SPY,
    expressed as a percentage (e.g., 18.5 ≈ VIX of ~18.5).
    This is structurally equivalent to the CBOE VIX methodology and does not
    depend on any external index feed. Falls back to 0.0 on failure.
    """
    try:
        bars  = _fetch_bars(data_client, "SPY", days=40)
        close = bars["close"].dropna()
        if len(close) < 21:
            logging.warning(
                f"get_vix(): only {len(close)} SPY bars — need 21 for 20-day realized vol."
            )
            return 0.0
        log_returns  = np.log(close / close.shift(1)).dropna()
        realized_vol = float(log_returns.iloc[-20:].std() * np.sqrt(252) * 100)
        logging.info(f"VIX Proxy (20d realized vol SPY): {realized_vol:.2f}")
        return realized_vol
    except Exception as e:
        logging.error(f"get_vix() failed — {type(e).__name__}: {e}")
        return 0.0


def get_market_direction(data_client):
    """
    Returns "BULL" if SPY is above its 200-day SMA, "BEAR" if below, "N/A" on error.
    Fetches 310 calendar days to guarantee at least 200 trading bars.
    """
    try:
        bars  = _fetch_bars(data_client, "SPY", days=310)
        close = bars["close"].dropna()
        if len(close) < 200:
            logging.warning(
                f"get_market_direction(): only {len(close)} SPY bars — need 200 for SMA."
            )
            return "N/A"
        sma_200   = close.rolling(window=200).mean().iloc[-1]
        current   = close.iloc[-1]
        direction = "BULL" if current > sma_200 else "BEAR"
        logging.info(
            f"Market Direction: {direction} (SPY={current:.2f}, SMA200={sma_200:.2f})"
        )
        return direction
    except Exception as e:
        logging.error(f"get_market_direction() failed — {type(e).__name__}: {e}")
        return "N/A"


def check_vix_kill_switch(data_client):
    """
    Volatility term structure kill switch using Alpaca-available ETF proxies:
      VIXY — ProShares VIX Short-Term Futures ETF  (tracks ~1M VIX futures, proxy for ^VIX)
      VXZ  — iPath S&P 500 VIX Mid-Term Futures ETN (tracks ~5M VIX futures, proxy for ^VIX3M)

    In normal contango the VIXY/VXZ price ratio is suppressed (typically 0.45–0.70).
    In backwardation / acute stress the ratio rises toward or above 1.0.

    Kill switch fires (returns True) if the current ratio breaches EITHER gate:
      1. Adaptive gate  — current ratio >= 20-day rolling mean + 1.5 × rolling std
                          (self-calibrating; detects sudden term-structure spikes)
      2. Hard floor     — current ratio >= 0.85
                          (absolute backstop for severe backwardation)

    Fails closed: returns True on any unrecoverable data error.
    """
    try:
        vixy_bars = _fetch_bars(data_client, "VIXY", days=40)
        vxz_bars  = _fetch_bars(data_client, "VXZ",  days=40)

        # Align on shared trading days before computing ratio
        aligned = pd.concat(
            [vixy_bars["close"].rename("vixy"), vxz_bars["close"].rename("vxz")],
            axis=1,
        ).dropna()

        if len(aligned) < 5:
            logging.warning(
                f"check_vix_kill_switch(): only {len(aligned)} aligned VIXY/VXZ bars — "
                "insufficient for ratio. Activating kill switch as failsafe."
            )
            return True

        ratio_series  = aligned["vixy"] / aligned["vxz"]
        current_ratio = float(ratio_series.iloc[-1])

        window        = min(20, len(ratio_series))
        rolling_mean  = float(ratio_series.iloc[-window:].mean())
        rolling_std   = float(ratio_series.iloc[-window:].std())
        adaptive_gate = rolling_mean + 1.5 * rolling_std

        logging.info(
            f"Vol Term Structure (VIXY/VXZ) | current={current_ratio:.4f}  "
            f"{window}d_mean={rolling_mean:.4f}  std={rolling_std:.4f}  "
            f"adaptive_gate={adaptive_gate:.4f}  hard_floor=0.8500"
        )

        adaptive_breach    = current_ratio >= adaptive_gate
        backwardation_hard = current_ratio >= 0.85

        if adaptive_breach or backwardation_hard:
            reason = (
                f"adaptive spike (ratio {current_ratio:.4f} >= {adaptive_gate:.4f})"
                if adaptive_breach
                else f"hard backwardation floor (ratio {current_ratio:.4f} >= 0.85)"
            )
            logging.warning(f"Kill switch ON: {reason}.")
            return True

        logging.info(
            f"Kill switch OFF: VIXY/VXZ={current_ratio:.4f} within normal range."
        )
        return False

    except Exception as e:
        logging.error(
            f"check_vix_kill_switch() failed — {type(e).__name__}: {e}. "
            "Activating kill switch as failsafe."
        )
        return True
