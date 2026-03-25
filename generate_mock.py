import pandas as pd
import ta
import json

def get_metrics(closes_list):
    df = pd.DataFrame({'close': closes_list})
    rsi = ta.momentum.RSIIndicator(close=df['close'], window=14).rsi().iloc[-1]
    sma = ta.trend.SMAIndicator(close=df['close'], window=50).sma_indicator().iloc[-1]
    curr = closes_list[-1]
    prev = closes_list[-2]
    return {"rsi": float(rsi), "sma": float(sma), "curr": float(curr), "prev": float(prev)}

results = {}

# 1. bullish_oversold_bounce: RSI < 40 but >= 30, Price > 50 SMA, current > previous
closes_bob = [10] * 80 + [10000] * 20 + [10000 - i * 300 for i in range(11)] + [7300]
results["bullish_oversold_bounce"] = get_metrics(closes_bob)

# 2. bullish_oversold_falling: RSI < 40, Price > 50 SMA, current <= previous
closes_bof = [10] * 80 + [10000] * 18 + [10000 - i * 300 for i in range(11)] + [6900]
results["bullish_oversold_falling"] = get_metrics(closes_bof)

# 3. bearish_oversold_bounce: RSI < 40, current > previous, Price <= 50 SMA
closes_beob = [10000] * 80 + [10000] * 20 + [10000 - i * 300 for i in range(11)] + [7100]
results["bearish_oversold_bounce"] = get_metrics(closes_beob)

# 4. bullish_normal: RSI >= 40, Price > 50 SMA, current > previous
closes_bn = [10] * 80 + [10000] * 20 + [10000 - i * 100 for i in range(11)] + [9200]
results["bullish_normal"] = get_metrics(closes_bn)

with open('mock_results.json', 'w') as f:
    json.dump(results, f, indent=2)
