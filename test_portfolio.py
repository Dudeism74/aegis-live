from portfolio import calculate_position_size

class DummyAccount:
    def __init__(self, portfolio_value, settled_cash):
        # Alpaca typically returns these as strings
        self.portfolio_value = str(portfolio_value)
        self.settled_cash = str(settled_cash)

class DummyClient:
    def __init__(self, account):
        self.account = account

    def get_account(self):
        return self.account

def run_tests():
    # Test 1: Enough settled cash
    # Portfolio value: $10000, 20% = $2000
    # Settled cash: $5000 (enough)
    account1 = DummyAccount(10000.0, 5000.0)
    client1 = DummyClient(account1)
    result1 = calculate_position_size(client1)
    print(f"Test 1 (Enough Cash): Expected 2000.0, Got {result1}")
    assert result1 == 2000.0

    # Test 2: Not enough settled cash
    # Portfolio value: $10000, 20% = $2000
    # Settled cash: $1000 (not enough)
    account2 = DummyAccount(10000.0, 1000.0)
    client2 = DummyClient(account2)
    result2 = calculate_position_size(client2)
    print(f"Test 2 (Not Enough Cash): Expected 0, Got {result2}")
    assert result2 == 0

    # Test 3: Exactly enough settled cash
    # Portfolio value: $10000, 20% = $2000
    # Settled cash: $2000 (exactly enough)
    account3 = DummyAccount(10000.0, 2000.0)
    client3 = DummyClient(account3)
    result3 = calculate_position_size(client3)
    print(f"Test 3 (Exactly Enough Cash): Expected 2000.0, Got {result3}")
    assert result3 == 2000.0

    # Test 4: Error handling (invalid attribute)
    class BadClient:
        def get_account(self):
            raise Exception("API error")

    client4 = BadClient()
    result4 = calculate_position_size(client4)
    print(f"Test 4 (Error Handling): Expected 0, Got {result4}")
    assert result4 == 0

if __name__ == '__main__':
    run_tests()
    print("All tests passed.")
