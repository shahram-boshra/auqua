# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Pre-trade risk gate — walking skeleton (thin); owned and extended by BP7.

Every order is checked before the venue sees it (Strategy Module 7). The
limits come from the environment's risk-limits file (ADR 0019): the gate is
built from a :class:`TradingLimits` model loaded by
:func:`auqua.config.loader.load` as ``limits_model``. Checks run in a fixed
order and the first breach decides; the decision names the rule, the limit
and the value that breached it.
"""

from dataclasses import dataclass
from decimal import Decimal

from pydantic import Field

from auqua.config.base import ConfigModel
from auqua.contracts.events.trading import Side
from auqua.contracts.numeric import arithmetic

MAX_ORDER_NOTIONAL = "max_order_notional"
MAX_POSITION = "max_position"


class TradingLimits(ConfigModel):
    """Limits of the skeleton's pre-trade gate (both positive)."""

    max_order_notional: Decimal = Field(gt=0)
    max_position: Decimal = Field(gt=0)


@dataclass(frozen=True)
class Check:
    """Outcome of a pre-trade check."""

    accepted: bool
    rule: str
    limit: Decimal | None = None
    value: Decimal | None = None


class PreTradeGate:
    """Checks order notional and resulting position against the limits."""

    def __init__(self, limits: TradingLimits) -> None:
        """Use ``limits`` for every check."""
        self._limits = limits

    def check(self, side: Side, quantity: Decimal, price: Decimal, position: Decimal) -> Check:
        """Check an order of ``quantity`` at reference ``price`` given the current ``position``."""
        with arithmetic():
            notional = quantity * price
            after = position + quantity if side is Side.BUY else position - quantity
        if notional > self._limits.max_order_notional:
            return Check(False, MAX_ORDER_NOTIONAL, self._limits.max_order_notional, notional)
        if abs(after) > self._limits.max_position:
            return Check(False, MAX_POSITION, self._limits.max_position, abs(after))
        return Check(True, "all")
