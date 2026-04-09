import yfinance as yf
import logging
import urllib.request
import json

def get_vix_level():
    """Returns the current VIX level as a float, or None on error."""
    try:
        vix = yf.Ticker("^VIX")
        return round(float(vix.history(period="1d")['Close'].iloc[-1]), 2)
    except Exception as e:
        logging.warning(f"Failed to fetch VIX level: {e}")
        try:
            req = urllib.request.Request(
                "https://query1.finance.yahoo.com/v8/finance/chart/^VIX",
                headers={'User-Agent': 'Mozilla/5.0'}
            )
            with urllib.request.urlopen(req) as response:
                data = json.loads(response.read().decode('utf-8'))
                return round(float(data['chart']['result'][0]['meta']['regularMarketPrice']), 2)
        except Exception as e2:
            logging.error(f"Backup VIX fetch also failed: {e2}")
            return None


def check_vix_kill_switch():
    try:
        vix = yf.Ticker("^VIX")
        current_price = vix.history(period="1d")['Close'].iloc[-1]
        if current_price > 35:
            return True
        else:
            return False
    except Exception as e:
        logging.warning(f"Primary VIX sensor failed: {e}. Engaging backup...")
        try:
            req = urllib.request.Request(
                "https://query1.finance.yahoo.com/v8/finance/chart/^VIX",
                headers={'User-Agent': 'Mozilla/5.0'}
            )
            with urllib.request.urlopen(req) as response:
                data = json.loads(response.read().decode('utf-8'))
                current_price = data['chart']['result'][0]['meta']['regularMarketPrice']
            logging.info(f"Backup VIX sensor reading: {current_price}")
            if current_price > 35:
                return True
            else:
                return False
        except Exception as e:
            logging.error(f"Backup VIX sensor failed: {e}. Activating kill switch.")
            return True
