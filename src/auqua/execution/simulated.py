# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Simulated venue — walking skeleton (thin); owned and extended by BP8.

Market orders accepted after bar *t* fill at the open of bar *t + 1 + delay*:
an order never trades at a price that was known when it was decided (no
look-ahead); ``delay_bars`` models latency (BP5 robustness sweep). Fill price
and fee come from a :class:`~auqua.execution.costs.CostModel` (ADR 0032).
"""

import uuid
from dataclasses import dataclass
from decimal import Decimal

from auqua.contracts.events.market import Bar
from auqua.contracts.events.trading import OrderSubmitted, Side
from auqua.contracts.identity import InstrumentId
from auqua.contracts.numeric import arithmetic, require_increment
from auqua.execution.costs import CostModel


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
    """Fills pending market orders at the open of a later bar."""

    def __init__(self, costs: CostModel, price_increment: Decimal, delay_bars: int = 0) -> None:
        """Price fills with ``costs`` on the ``price_increment`` grid after ``delay_bars`` bars."""
        if delay_bars < 0:
            raise ValueError(f"delay_bars must be >= 0, got {delay_bars}")
        require_increment(price_increment, "price_increment")
        self._costs, self._increment, self._delay = costs, price_increment, delay_bars
        self._pending: dict[uuid.UUID, tuple[OrderSubmitted, int]] = {}

    def submit(self, order: OrderSubmitted) -> None:
        """Queue ``order``; it fills at the open of the ``1 + delay_bars``-th next bar."""
        self._pending[order.id] = (order, self._delay)

    def working(self, instrument_id: InstrumentId) -> Decimal:
        """Signed quantity of the accepted, not yet filled orders of ``instrument_id``."""
        with arithmetic():
            return sum(
                (
                    order.quantity if order.side is Side.BUY else -order.quantity
                    for order, _ in self._pending.values()
                    if order.instrument_id == instrument_id
                ),
                Decimal(0),
            )

    def on_bar(self, bar: Bar) -> list[Execution]:
        """Fill the due orders of ``bar``'s instrument at its open, in submission order."""
        executions = []
        for order_id, (order, wait) in list(self._pending.items()):
            if order.instrument_id != bar.instrument_id:
                continue
            if wait:
                self._pending[order_id] = (order, wait - 1)
                continue
            price = self._costs.fill_price(order.side, bar.open, self._increment)
            with arithmetic():
                notional = price * order.quantity
            fee = self._costs.fee(notional)
            executions.append(
                Execution(order, order.side, price, order.quantity, fee, bar.start_ns)
            )
            del self._pending[order_id]
        return executions
