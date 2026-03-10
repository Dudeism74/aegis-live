def calculate_position_size(trading_client):
    try:
        account = trading_client.get_account()

        # Alpaca SDK returns strings for these decimal fields
        portfolio_value = float(account.portfolio_value)
        settled_cash = float(account.settled_cash)

        target_value = portfolio_value * 0.20

        if settled_cash >= target_value:
            return target_value
        else:
            return 0

    except Exception:
        return 0
