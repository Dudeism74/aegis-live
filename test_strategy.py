import pandas as pd
from strategy import check_rsi_buy_signal

class DummyBars:
    def __init__(self, df):
        self.df = df

class DummyAlpacaClient:
    def __init__(self, data_type='normal'):
        self.data_type = data_type

    def get_bars(self, symbol, timeframe, start, end):
        # We need at least 50 days of data for the 50-day SMA.
        # Let's provide 100 days of data to be safe.

        if self.data_type == 'bullish_oversold_bounce':
            # Price > 50 SMA, RSI < 40, current > previous
            closes = [10] * 80
            closes += [10000] * 20
            for i in range(11):
                closes.append(10000 - i * 300)
            closes.append(7300)
        elif self.data_type == 'bullish_oversold_falling':
            # Price > 50 SMA, RSI < 40, current <= previous
            closes = [10] * 80
            closes += [10000] * 18
            for i in range(11):
                closes.append(10000 - i * 300)
            closes.append(6900)
        elif self.data_type == 'bearish_oversold_bounce':
            # Price <= 50 SMA, RSI < 40, current > previous
            closes = [10000] * 80
            closes += [10000] * 20
            for i in range(11):
                closes.append(10000 - i * 300)
            closes.append(7100)
        elif self.data_type == 'bullish_normal':
            # Price > 50 SMA, RSI >= 40, current > previous
            closes = [10] * 80
            closes += [10000] * 20
            for i in range(11):
                closes.append(10000 - i * 100)
            closes.append(9200)
        elif self.data_type == 'too_short':
            closes = [100, 101, 102]
        else:
            raise Exception("API Connection Error")

        df = pd.DataFrame({'close': closes})
        return DummyBars(df)

    def get_stock_bars(self, req):
        # Provide fallback for new Alpaca SDK just in case
        return self.get_bars(req.symbol_or_symbols, req.timeframe, req.start, req.end)

def test_strategy():
    print("Running strategy tests...")

    client1 = DummyAlpacaClient('bullish_oversold_bounce')
    res1 = check_rsi_buy_signal(client1, "AAPL")
    print(f"Test 1 (Bullish Oversold Bounce): Expected True, Got {res1}")
    assert res1 == True

    client2 = DummyAlpacaClient('bullish_oversold_falling')
    res2 = check_rsi_buy_signal(client2, "AAPL")
    print(f"Test 2 (Bullish Oversold Falling): Expected False, Got {res2}")
    assert res2 == False

    client3 = DummyAlpacaClient('bearish_oversold_bounce')
    res3 = check_rsi_buy_signal(client3, "AAPL")
    print(f"Test 3 (Bearish Oversold Bounce): Expected False, Got {res3}")
    assert res3 == False

    client4 = DummyAlpacaClient('bullish_normal')
    res4 = check_rsi_buy_signal(client4, "AAPL")
    print(f"Test 4 (Bullish Normal): Expected False, Got {res4}")
    assert res4 == False

    client5 = DummyAlpacaClient('too_short')
    res5 = check_rsi_buy_signal(client5, "AAPL")
    print(f"Test 5 (Not Enough Data): Expected False, Got {res5}")
    assert res5 == False

    client6 = DummyAlpacaClient('error')
    res6 = check_rsi_buy_signal(client6, "AAPL")
    print(f"Test 6 (API Error): Expected False, Got {res6}")
    assert res6 == False

    print("All strategy tests passed.")

if __name__ == '__main__':
    test_strategy()
