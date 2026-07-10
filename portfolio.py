"""Portfolio sizing rules."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def calculate_position_size(
    trading_client,
    entry_price: float | None = None,
    entry_atr: float | None = None,
    risk_percent: float = 0.005,
    max_position_percent: float = 0.20,
) -> float:
    """Return notional sized to a 2-ATR stop and capped by equity and cash.

    The default risks at most 0.5% of account equity if the fixed stop is hit.
    When price/ATR are omitted, the equity cap is returned for compatibility.
    """
    try:
        account = trading_client.get_account()
        equity = float(account.equity)
        cash = float(account.cash)
        buying_power = float(account.non_marginable_buying_power)
        if equity <= 0 or cash <= 0 or buying_power <= 0:
            return 0.0
        equity_cap = equity * max_position_percent
        if entry_price is not None or entry_atr is not None:
            if not entry_price or not entry_atr or entry_price <= 0 or entry_atr <= 0:
                logger.error("Invalid entry price/ATR for risk sizing: %s/%s", entry_price, entry_atr)
                return 0.0
            risk_budget = equity * risk_percent
            shares = risk_budget / (2.0 * entry_atr)
            risk_notional = shares * entry_price
            target = min(risk_notional, equity_cap, cash, buying_power)
        else:
            target = min(equity_cap, cash, buying_power)
        return round(max(0.0, target), 2)
    except Exception as exc:
        logger.error("Error calculating position size: %s", exc)
        return 0.0
