import logging

logger = logging.getLogger(__name__)

def calculate_position_size(trading_client):
    try:
        account = trading_client.get_account()

        # Alpaca SDK returns strings for these decimal fields
        available_cash = float(account.cash) 
        non_marginable_buying_power = float(account.non_marginable_buying_power)

        SYNTHETIC_BASE_CAPITAL = 3600.0
        target_value = SYNTHETIC_BASE_CAPITAL * 0.20

        logger.info(
            f"available_cash={available_cash}, "
            f"non_marginable_buying_power={non_marginable_buying_power}, "
            f"target_value={target_value}"
        )

        if non_marginable_buying_power >= target_value:
            if available_cash < target_value:
                logger.warning(
                    f"available_cash ({available_cash}) would have blocked this trade, "
                    f"but non_marginable_buying_power ({non_marginable_buying_power}) is sufficient"
                )
            return target_value
        else:
            return 0

    except Exception as e:
        # We MUST log the error so it doesn't fail silently
        logger.error(f"Error calculating position size: {e}")
        return 0
