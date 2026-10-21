# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Simulated venue — walking skeleton (thin); owned and extended by BP8.

Market orders accepted after bar *t* fill at the open of bar *t + 1*: an order
never trades at a price that was known when it was decided (no look-ahead).
The fee is a fixed rate in basis points of the notional, rounded once to the
fee increment with an explicit mode (ADR 0016).
"""

import uuid
from dataclasses import dataclass
from decimal import Decimal

from auqua.contracts.events.market import Bar
from auqua.contracts.events.trading import OrderSubmitted, Side
from auqua.contracts.numeric import Rounding, arithmetic, quantize

_BASIS_POINTS = Decimal(10_000)


@dataclass(frozen=True)
class Execution:
    """A fill decided by the venue, before it becomes an event."""

    order: OrderSubmitted
    side: Side
    price: Decimal
    quantity: Decimal
    fee: Decimal
    ts_event_ns: int


class SimulatedVenue:
    """Fills pending market orders at the next bar's open."""

    def __init__(self, fee_bps: Decimal, fee_increment: Decimal) -> None:
        """Charge ``fee_bps`` basis points, rounded half-even to ``fee_increment``."""
        if fee_bps < 0:
            raise ValueError(f"fee_bps must be >= 0, got {fee_bps}")
        self._fee_bps, self._fee_increment = fee_bps, fee_increment
        self._pending: dict[uuid.UUID, OrderSubmitted] = {}

    def submit(self, order: OrderSubmitted) -> None:
        """Queue ``order`` for the next bar."""
        self._pending[order.id] = order

    def on_bar(self, bar: Bar) -> list[Execution]:
        """Fill the pending orders of ``bar``'s instrument at its open, in submission order."""
        executions = []
        for order_id, order in list(self._pending.items()):
            if order.instrument_id != bar.instrument_id:
                continue
            with arithmetic():
                raw_fee = bar.open * order.quantity * self._fee_bps / _BASIS_POINTS
            fee = quantize(raw_fee, self._fee_increment, Rounding.HALF_EVEN)
            executions.append(
                Execution(order, order.side, bar.open, order.quantity, fee, bar.start_ns)
            )
            del self._pending[order_id]
        return executions
