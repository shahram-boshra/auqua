# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Execution cost models (BP5 section 4, R-8; ADR 0032); extended by BP8.

A cost model decides the fill price from a reference price and the fee on a
notional. :class:`FixedBps` charges a fee in basis points of the notional and
moves the fill price against the trader by a slippage in basis points; the
price is rounded once to the instrument's price grid in the adverse direction
(buys up, sells down), so a rounded price is never better than the model's,
and the fee is rounded half-even to the fee increment (ADR 0016). Market-impact
models belong to BP8.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from auqua.contracts.events.trading import Side
from auqua.contracts.numeric import Rounding, arithmetic, quantize, require_increment

BASIS_POINTS = Decimal(10_000)


class CostModel(Protocol):
    """Fill price and fee of a simulated execution."""

    def fill_price(self, side: Side, reference: Decimal, increment: Decimal) -> Decimal:
        """Price paid or received for ``side`` given the ``reference`` price."""
        ...

    def fee(self, notional: Decimal) -> Decimal:
        """Fee on ``notional``."""
        ...


@dataclass(frozen=True)
class FixedBps:
    """Fee and slippage in basis points."""

    fee_bps: Decimal
    slippage_bps: Decimal
    fee_increment: Decimal

    def __post_init__(self) -> None:
        """Refuse negative rates and invalid increments."""
        if self.fee_bps < 0 or self.slippage_bps < 0:
            raise ValueError("fee_bps and slippage_bps must be >= 0")
        require_increment(self.fee_increment, "fee_increment")

    def fill_price(self, side: Side, reference: Decimal, increment: Decimal) -> Decimal:
        """Reference moved by the slippage against the trader, rounded adversely to the grid."""
        sign = 1 if side is Side.BUY else -1
        with arithmetic():
            raw = reference * (BASIS_POINTS + sign * self.slippage_bps) / BASIS_POINTS
        rounding = Rounding.CEILING if side is Side.BUY else Rounding.FLOOR
        return quantize(raw, increment, rounding)

    def fee(self, notional: Decimal) -> Decimal:
        """``notional * fee_bps / 10_000`` rounded half-even to the fee increment."""
        with arithmetic():
            raw = notional * self.fee_bps / BASIS_POINTS
        return quantize(raw, self.fee_increment, Rounding.HALF_EVEN)

    def scaled(self, factor: Decimal) -> FixedBps:
        """The same model with fee and slippage multiplied by ``factor`` (cost sweeps)."""
        with arithmetic():
            return FixedBps(self.fee_bps * factor, self.slippage_bps * factor, self.fee_increment)
