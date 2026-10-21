# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Cash, position and P&L of one instrument — walking skeleton (thin); owned by BP6.

Exact decimals throughout. Realized P&L uses first-in, first-out lots, which
needs no division; fees are paid from cash and reported separately. Equity is
cash plus the position marked at a price. Short positions are refused here
(the skeleton trades spot, long or flat).
"""

from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal

from auqua.contracts.events.trading import Fill, Side
from auqua.contracts.numeric import arithmetic


@dataclass
class _Lot:
    quantity: Decimal
    price: Decimal


@dataclass
class Book:
    """Account state for one instrument in its quote currency."""

    cash: Decimal
    position: Decimal = Decimal(0)
    realized: Decimal = Decimal(0)
    fees: Decimal = Decimal(0)
    _lots: deque[_Lot] = field(default_factory=deque, repr=False)

    def apply(self, fill: Fill) -> None:
        """Update cash, position, lots, realized P&L and fees with ``fill``.

        Raises:
            ValueError: the fill would sell more than the position (no shorts).
        """
        with arithmetic():
            notional = fill.price * fill.quantity
            if fill.side is Side.BUY:
                self.cash -= notional + fill.fee
                self._lots.append(_Lot(fill.quantity, fill.price))
                self.position += fill.quantity
            else:
                if fill.quantity > self.position:
                    raise ValueError(f"sell {fill.quantity} exceeds position {self.position}")
                self.cash += notional - fill.fee
                remaining = fill.quantity
                while remaining:
                    lot = self._lots[0]
                    used = min(lot.quantity, remaining)
                    self.realized += (fill.price - lot.price) * used
                    lot.quantity -= used
                    remaining -= used
                    if not lot.quantity:
                        self._lots.popleft()
                self.position -= fill.quantity
            self.fees += fill.fee

    def equity(self, mark: Decimal) -> Decimal:
        """Cash plus the position valued at ``mark``."""
        with arithmetic():
            return self.cash + self.position * mark
