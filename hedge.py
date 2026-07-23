"""Configurable QQQ risk governor and short-term PSQ hedge rules."""

from __future__ import annotations

import os
from dataclasses import dataclass


RISK_NORMAL = "NORMAL"
RISK_MODERATE = "MODERATE"
RISK_SEVERE = "SEVERE"
RISK_UNKNOWN = "UNKNOWN"
HEDGE_MODES = {"off", "observe", "paper"}


@dataclass(frozen=True)
class HedgeConfig:
    """Validated hedge settings loaded from the environment."""

    mode: str = "observe"
    symbol: str = "PSQ"
    moderate_qqq_return: float = -0.01
    severe_qqq_return: float = -0.015
    moderate_exposure_factor: float = 0.50
    hedge_ratio: float = 0.25
    minimum_rebalance_usd: float = 25.0
    rebalance_tolerance: float = 0.10

    @classmethod
    def from_env(cls) -> "HedgeConfig":
        config = cls(
            mode=os.getenv("AEGIS_HEDGE_MODE", "observe").strip().lower(),
            symbol=os.getenv("AEGIS_HEDGE_SYMBOL", "PSQ").strip().upper(),
            moderate_qqq_return=float(
                os.getenv("AEGIS_HEDGE_MODERATE_QQQ_RETURN", "-0.01")
            ),
            severe_qqq_return=float(
                os.getenv("AEGIS_HEDGE_SEVERE_QQQ_RETURN", "-0.015")
            ),
            moderate_exposure_factor=float(
                os.getenv("AEGIS_HEDGE_MODERATE_EXPOSURE_FACTOR", "0.50")
            ),
            hedge_ratio=float(os.getenv("AEGIS_HEDGE_RATIO", "0.25")),
            minimum_rebalance_usd=float(
                os.getenv("AEGIS_HEDGE_MIN_REBALANCE_USD", "25")
            ),
            rebalance_tolerance=float(
                os.getenv("AEGIS_HEDGE_REBALANCE_TOLERANCE", "0.10")
            ),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.mode not in HEDGE_MODES:
            raise RuntimeError(
                "AEGIS_HEDGE_MODE must be 'off', 'observe', or 'paper'"
            )
        if not self.symbol:
            raise RuntimeError("AEGIS_HEDGE_SYMBOL cannot be empty")
        if not (
            self.severe_qqq_return < self.moderate_qqq_return < 0
        ):
            raise RuntimeError(
                "Hedge QQQ thresholds must satisfy severe < moderate < 0"
            )
        if not 0 <= self.moderate_exposure_factor <= 1:
            raise RuntimeError(
                "AEGIS_HEDGE_MODERATE_EXPOSURE_FACTOR must be between 0 and 1"
            )
        if not 0 <= self.hedge_ratio <= 1:
            raise RuntimeError("AEGIS_HEDGE_RATIO must be between 0 and 1")
        if self.minimum_rebalance_usd < 0:
            raise RuntimeError("AEGIS_HEDGE_MIN_REBALANCE_USD cannot be negative")
        if not 0 <= self.rebalance_tolerance <= 1:
            raise RuntimeError(
                "AEGIS_HEDGE_REBALANCE_TOLERANCE must be between 0 and 1"
            )

    def classify(self, qqq_return: float | None) -> str:
        if qqq_return is None:
            return RISK_UNKNOWN
        if qqq_return <= self.severe_qqq_return:
            return RISK_SEVERE
        if qqq_return <= self.moderate_qqq_return:
            return RISK_MODERATE
        return RISK_NORMAL

    def entry_exposure_factor(self, risk_state: str) -> float:
        """Return the paper-mode multiplier for otherwise valid new entries."""
        if self.mode != "paper":
            return 1.0
        if risk_state in {RISK_SEVERE, RISK_UNKNOWN}:
            return 0.0
        if risk_state == RISK_MODERATE:
            return self.moderate_exposure_factor
        return 1.0

    def target_notional(
        self, gross_long_notional: float, risk_state: str, hedge_is_open: bool
    ) -> float:
        """Return desired PSQ notional with hysteresis for an existing hedge."""
        gross = max(0.0, gross_long_notional)
        should_hold = risk_state == RISK_SEVERE or (
            hedge_is_open and risk_state == RISK_MODERATE
        )
        return round(gross * self.hedge_ratio, 2) if should_hold else 0.0

    def rebalance_required(self, current: float, target: float) -> bool:
        difference = abs(float(target) - float(current))
        tolerance = max(
            self.minimum_rebalance_usd,
            abs(float(target)) * self.rebalance_tolerance,
        )
        return difference >= tolerance
