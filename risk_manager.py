import yfinance as yf
import logging
import urllib.request
import json


def get_vix():
    try:
        vix = yf.Ticker("^VIX")
        return vix.history(period="1d")['Close'].iloc[-1]
    except Exception as e:
        logging.error(f"Failed to fetch VIX level: {e}")
        return 0.0


def get_market_direction():
    try:
        spy = yf.Ticker("SPY")
        hist = spy.history(period="300d")
        if len(hist) < 200:
            return "N/A"
        sma_200 = hist['Close'].rolling(window=200).mean().iloc[-1]
        current = hist['Close'].iloc[-1]
        return "BULL" if current > sma_200 else "BEAR"
    except Exception as e:
        logging.error(f"Failed to fetch Market Direction: {e}")
        return "N/A"


def check_vix_kill_switch():
    """
    Kill switch based on VIX term structure ratio (^VIX / ^VIX3M).

    A ratio >= 0.95 indicates the volatility curve is in backwardation or near-flat,
    signaling acute market stress. Returns True (kill switch ON) to halt trading.
    Returns False (safe to trade) when ratio < 0.95 (normal contango).
    Fails closed: any unrecoverable sensor error activates the kill switch.
    """
    try:
        vix_spot = yf.Ticker("^VIX")
        vix3m = yf.Ticker("^VIX3M")

        vix_current = vix_spot.history(period="1d")['Close'].iloc[-1]
        vix3m_current = vix3m.history(period="1d")['Close'].iloc[-1]

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
        logging.warning(f"Primary VIX term structure sensor failed: {e}. Engaging backup...")
        try:
            def _yahoo_price(ticker_encoded):
                url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker_encoded}"
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                return data["chart"]["result"][0]["meta"]["regularMarketPrice"]

            vix_current = _yahoo_price("%5EVIX")
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
            logging.error(f"Backup VIX sensor failed: {e2}. Activating kill switch as failsafe.")
            return True
