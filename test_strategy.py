import pandas as pd
from strategy import check_rsi_buy_signal

class DummyBars:
    def __init__(self, df):
        self.df = df

class DummyAlpacaClient:
    def __init__(self, data_type='normal'):
        self.data_type = data_type

    def get_bars(self, symbol, timeframe, start, end):
        # Create dummy data for 20 days
        # We need a DataFrame with a 'close' column

        if self.data_type == 'normal':
            # RSI should be around 50
            closes = [100 + i % 3 for i in range(25)]
        elif self.data_type == 'oversold':
            # Continuous drop, RSI should be near 0
            closes = [100 - i * 2 for i in range(25)]
        elif self.data_type == 'overbought':
            # Continuous rise, RSI should be near 100
            closes = [100 + i * 2 for i in range(25)]
        elif self.data_type == 'too_short':
            closes = [100, 101, 102]
        else:
            raise Exception("API Connection Error")

        df = pd.DataFrame({'close': closes})
        return DummyBars(df)


def test_strategy():
    print("Running strategy tests...")

    # Test 1: Normal market (RSI > 30)
    client1 = DummyAlpacaClient('normal')
    res1 = check_rsi_buy_signal(client1, "AAPL")
    print(f"Test 1 (Normal Market): Expected False, Got {res1}")
    assert res1 == False

    # Test 2: Oversold market (RSI < 30)
    client2 = DummyAlpacaClient('oversold')
    res2 = check_rsi_buy_signal(client2, "AAPL")
    print(f"Test 2 (Oversold Market): Expected True, Got {res2}")
    assert res2 == True

    # Test 3: Overbought market (RSI > 30)
    client3 = DummyAlpacaClient('overbought')
    res3 = check_rsi_buy_signal(client3, "AAPL")
    print(f"Test 3 (Overbought Market): Expected False, Got {res3}")
    assert res3 == False

    # Test 4: Not enough data
    client4 = DummyAlpacaClient('too_short')
    res4 = check_rsi_buy_signal(client4, "AAPL")
    print(f"Test 4 (Not Enough Data): Expected False, Got {res4}")
    assert res4 == False

    # Test 5: API Error
    client5 = DummyAlpacaClient('error')
    res5 = check_rsi_buy_signal(client5, "AAPL")
    print(f"Test 5 (API Error): Expected False, Got {res5}")
    assert res5 == False

    print("All strategy tests passed.")

if __name__ == '__main__':
    test_strategy()
