import yfinance as yf

def check_vix_kill_switch():
    try:
        vix = yf.Ticker("^VIX")
        # fast_info is typically faster and more reliable for simple live price
        current_price = vix.fast_info['lastPrice']
        if current_price > 30:
            return True
        else:
            return False
    except Exception:
        # Default to True as a safety precaution if connection fails
        return True
