import time
import logging
import urllib.request
import json

import yfinance as yf


def _yf_fetch_close(ticker_str, period="5d", retries=3):
    """
    Fetch the most recent non-NaN closing price for `ticker_str` via yfinance.
    Uses period="5d" by default so weekends and market-closed runs still return data.
    Retries up to `retries` times with a 2-second pause on failure, logging the
    exact exception type and message each time so failures are diagnosable.
    """
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            hist = yf.Ticker(ticker_str).history(period=period)
            if hist.empty:
                raise ValueError(f"yfinance returned an empty DataFrame for '{ticker_str}'")
            close_series = hist["Close"].dropna()
            if close_series.empty:
                raise ValueError(f"All Close values are NaN for '{ticker_str}'")
            return float(close_series.iloc[-1])
        except Exception as e:
            last_exc = e
            logging.warning(
                f"yfinance fetch attempt {attempt}/{retries} for '{ticker_str}' failed — "
                f"{type(e).__name__}: {e}"
            )
            if attempt < retries:
                time.sleep(2)

    raise RuntimeError(
        f"All {retries} yfinance fetch attempts exhausted for '{ticker_str}'"
    ) from last_exc


def get_vix():
    try:
        return _yf_fetch_close("^VIX")
    except Exception as e:
        logging.error(f"get_vix() failed after all retries — {type(e).__name__}: {e}")
        return 0.0


def get_market_direction():
    try:
        hist = yf.Ticker("SPY").history(period="300d")
        if hist.empty or len(hist) < 200:
            logging.warning(
                f"get_market_direction(): insufficient SPY bars ({len(hist)}). Returning N/A."
            )
            return "N/A"
        close = hist["Close"].dropna()
        sma_200 = close.rolling(window=200).mean().iloc[-1]
        current = close.iloc[-1]
        direction = "BULL" if current > sma_200 else "BEAR"
        logging.info(f"Market Direction: {direction} (SPY={current:.2f}, SMA200={sma_200:.2f})")
        return direction
    except Exception as e:
        logging.error(f"get_market_direction() failed — {type(e).__name__}: {e}")
        return "N/A"


def check_vix_kill_switch():
    """
    Kill switch based on VIX term structure ratio (^VIX / ^VIX3M).

    Ratio >= 0.95 → backwardation / near-flat curve → acute stress → kill switch ON (True).
    Ratio  < 0.95 → normal contango → safe to trade → kill switch OFF (False).
    Fails closed: if both primary and backup sensors fail, returns True.
    """
    try:
        vix_current  = _yf_fetch_close("^VIX")
        vix3m_current = _yf_fetch_close("^VIX3M")

        ratio = vix_current / vix3m_current
        logging.info(
            f"VIX Term Structure | VIX={vix_current:.2f}  VIX3M={vix3m_current:.2f}  Ratio={ratio:.4f}"
        )

        if ratio >= 0.95:
            logging.warning(
                f"Kill switch ON: term structure ratio {ratio:.4f} >= 0.95 "
                "(backwardation / stress detected)."
            )
            return True

        logging.info(f"Kill switch OFF: term structure ratio {ratio:.4f} < 0.95 (normal contango).")
        return False

    except Exception as e:
        logging.warning(
            f"Primary VIX term structure sensor failed — {type(e).__name__}: {e}. "
            "Engaging HTTP backup..."
        )
        try:
            def _yahoo_price(ticker_encoded):
                url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker_encoded}"
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                return float(data["chart"]["result"][0]["meta"]["regularMarketPrice"])

            vix_current   = _yahoo_price("%5EVIX")
            vix3m_current = _yahoo_price("%5EVIX3M")

            ratio = vix_current / vix3m_current
            logging.info(
                f"Backup sensor | VIX={vix_current:.2f}  VIX3M={vix3m_current:.2f}  Ratio={ratio:.4f}"
            )

            if ratio >= 0.95:
                logging.warning(f"Kill switch ON (backup): ratio {ratio:.4f} >= 0.95.")
                return True

            logging.info(f"Kill switch OFF (backup): ratio {ratio:.4f} < 0.95.")
            return False

        except Exception as e2:
            logging.error(
                f"Backup VIX sensor also failed — {type(e2).__name__}: {e2}. "
                "Activating kill switch as failsafe."
            )
            return True
