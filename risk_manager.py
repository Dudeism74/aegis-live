"""Market-risk telemetry. Unvalidated observations never control orders."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.requests import StockBarsRequest, StockSnapshotRequest
from alpaca.data.timeframe import TimeFrame


def _fetch_bars(data_client, symbol: str, days: int = 60, retries: int = 3):
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            end = datetime.now()
            request = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Day,
                start=end - timedelta(days=days),
                end=end,
                feed=DataFeed.IEX,
                adjustment=Adjustment.ALL,
            )
            bars = data_client.get_stock_bars(request).df
            if isinstance(bars.index, pd.MultiIndex):
                bars = bars.xs(symbol, level=0)
            if bars.empty:
                raise ValueError(f"Empty bar data for {symbol}")
            return bars
        except Exception as exc:
            last_error = exc
            logging.warning("Bar fetch %d/%d failed for %s: %s", attempt, retries, symbol, exc)
            if attempt < retries:
                time.sleep(2)
    raise RuntimeError(f"All bar fetch attempts failed for {symbol}") from last_error


def get_spy_realized_volatility_20d(data_client):
    """Return annualized 20-day SPY realized volatility, or ``None`` on failure."""
    try:
        close = _fetch_bars(data_client, "SPY", days=40)["close"].dropna()
        if len(close) < 21:
            logging.warning("Only %d SPY bars; 21 are required for realized volatility", len(close))
            return None
        returns = np.log(close / close.shift(1)).dropna()
        value = float(returns.iloc[-20:].std() * np.sqrt(252) * 100)
        logging.info("SPY 20-day realized volatility: %.2f", value)
        return value
    except Exception as exc:
        logging.error("SPY realized-volatility calculation failed: %s", exc)
        return None


def get_vix(data_client):
    """Backward-compatible alias; this value is not the Cboe VIX."""
    return get_spy_realized_volatility_20d(data_client)


def get_market_direction(data_client):
    try:
        close = _fetch_bars(data_client, "SPY", days=310)["close"].dropna()
        if len(close) < 200:
            logging.warning("Only %d SPY bars; 200 are required for market direction", len(close))
            return "N/A"
        average = float(close.rolling(window=200).mean().iloc[-1])
        current = float(close.iloc[-1])
        direction = "BULL" if current > average else "BEAR"
        logging.info("Market direction %s (SPY %.2f, SMA200 %.2f)", direction, current, average)
        return direction
    except Exception as exc:
        logging.error("Market-direction calculation failed: %s", exc)
        return "N/A"


def get_market_day_returns(
    data_client, symbols: tuple[str, ...] = ("QQQ", "SPY"), retries: int = 3
) -> dict[str, float] | None:
    """Return current session returns from Alpaca snapshots.

    Missing or invalid market data returns ``None`` so the paper risk governor
    can fail closed for new entries without fabricating a hedge signal.
    """
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            snapshots = data_client.get_stock_snapshot(
                StockSnapshotRequest(symbol_or_symbols=list(symbols))
            )
            returns: dict[str, float] = {}
            for symbol in symbols:
                snapshot = snapshots[symbol]
                daily_bar = getattr(snapshot, "daily_bar", None)
                previous_bar = getattr(snapshot, "previous_daily_bar", None)
                current = float(getattr(daily_bar, "close"))
                previous = float(getattr(previous_bar, "close"))
                if current <= 0 or previous <= 0:
                    raise ValueError(f"Invalid snapshot prices for {symbol}")
                returns[symbol] = current / previous - 1.0
            logging.info(
                "Market session returns: %s",
                ", ".join(f"{symbol}={value:.2%}" for symbol, value in returns.items()),
            )
            return returns
        except Exception as exc:
            last_error = exc
            logging.warning(
                "Market snapshot %d/%d failed: %s", attempt, retries, exc
            )
            if attempt < retries:
                time.sleep(2)
    logging.error("Market session returns unavailable: %s", last_error)
    return None


def observe_vix_term_structure(data_client):
    """Return normalized VIXY/VXZ telemetry without blocking any trades."""
    try:
        vixy = _fetch_bars(data_client, "VIXY", days=40)
        vxz = _fetch_bars(data_client, "VXZ", days=40)
        aligned = pd.concat(
            [vixy["close"].rename("vixy"), vxz["close"].rename("vxz")], axis=1
        ).dropna()
        if len(aligned) < 5:
            logging.warning("Only %d aligned VIXY/VXZ bars; skipping observation", len(aligned))
            return None
        ratios = aligned["vixy"] / aligned["vxz"]
        window = min(20, len(ratios))
        sample = ratios.iloc[-window:]
        current, mean, std = float(sample.iloc[-1]), float(sample.mean()), float(sample.std())
        zscore = (current - mean) / std if std > 0 else 0.0
        result = {"ratio": current, "mean": mean, "std": std, "zscore": zscore, "window": window}
        logging.info("VIXY/VXZ observe-only ratio=%.4f z-score=%.3f", current, zscore)
        return result
    except Exception as exc:
        logging.error("VIXY/VXZ observation failed: %s", exc)
        return None


def check_vix_kill_switch(data_client):
    """Deprecated compatibility hook. The unvalidated ratio no longer blocks entries."""
    observe_vix_term_structure(data_client)
    return False
