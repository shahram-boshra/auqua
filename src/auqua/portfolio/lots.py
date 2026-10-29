# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""First-in, first-out lots of one instrument in one account (BP6 section 4.3; PC-3).

A lot is opened by a fill at its price and carries economic cost: fees are
separate postings and never enter a lot. Lots are relieved first in, first out
(26 CFR 1.1012-1(j)(3)(i) as quoted in IRS Notices 2025-7 and 2026-20; IRS
FAQ for shares). All lots of an instrument face one way: a trade in the other
direction relieves them in order, and any excess opens a lot in its own
direction.

Costs are exact. A lot knows its ``unit_cost`` while it is an exact decimal, so
relieving ``u`` units costs ``unit_cost * u`` without rounding. After a split
whose quotient does not terminate, the lot keeps only ``total_cost``; relieving
``u`` units then moves ``divide(total_cost * u, quantity, increment,
HALF_EVEN)`` and the last unit takes the remainder, so the relieved parts and
the remaining cost always sum exactly to the lot's total cost. This is the
one rounding rule of the module (ADR 0008).
"""

from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction

from auqua.contracts.numeric import PRECISION, Rounding, divide, exact, require_increment
from auqua.portfolio.errors import LotError


def _exact_quotient(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    """``numerator / denominator`` as a decimal if it terminates within :data:`PRECISION` digits."""
    ratio = Fraction(numerator) / Fraction(denominator)
    twos = fives = 0
    rest = ratio.denominator
    while rest % 2 == 0:
        rest //= 2
        twos += 1
    while rest % 5 == 0:
        rest //= 5
        fives += 1
    if rest != 1:
        return None
    digits = max(twos, fives)
    units = ratio.numerator * (10**digits // ratio.denominator)
    if len(str(abs(units))) > PRECISION:
        return None
    return Decimal(units).scaleb(-digits)


@dataclass
class Lot:
    """Open units of one acquisition: ``quantity`` > 0, cost in the quote currency.

    ``unit_cost`` is ``None`` when ``total_cost / quantity`` does not terminate.
    """

    quantity: Decimal
    total_cost: Decimal
    unit_cost: Decimal | None


@dataclass(frozen=True)
class Relief:
    """Units taken from one lot and their cost."""

    quantity: Decimal
    cost: Decimal


@dataclass(frozen=True)
class TradeResult:
    """What a trade did to the lots: the relieved parts, in FIFO order, and the units opened."""

    relieved: tuple[Relief, ...]
    opened: Decimal

    @property
    def relieved_quantity(self) -> Decimal:
        """Units relieved in total."""
        with exact():
            return sum((r.quantity for r in self.relieved), Decimal(0))

    @property
    def relieved_cost(self) -> Decimal:
        """Cost relieved in total."""
        with exact():
            return sum((r.cost for r in self.relieved), Decimal(0))


class Lots:
    """FIFO lots of one instrument; ``increment`` is the quote currency's minor unit."""

    def __init__(self, increment: Decimal) -> None:
        """Start with no lots.

        Raises:
            InvalidDecimalError: ``increment`` is not a positive finite decimal.
        """
        require_increment(increment)
        self._increment = increment
        self._lots: deque[Lot] = deque()
        self._direction = 0

    @property
    def position(self) -> Decimal:
        """Signed open quantity: positive long, negative short, zero flat."""
        with exact():
            total = sum((lot.quantity for lot in self._lots), Decimal(0))
            return total * self._direction

    @property
    def cost(self) -> Decimal:
        """Total cost of the open lots (positive for long and short lots alike)."""
        with exact():
            return sum((lot.total_cost for lot in self._lots), Decimal(0))

    def lots(self) -> tuple[Lot, ...]:
        """A copy of the open lots, oldest first."""
        return tuple(Lot(lot.quantity, lot.total_cost, lot.unit_cost) for lot in self._lots)

    def trade(self, quantity: Decimal, price: Decimal) -> TradeResult:
        """Apply a fill of signed ``quantity`` (buy > 0, sell < 0) at ``price``.

        Units against the current direction relieve lots first in, first out;
        the rest opens a lot at ``price``.

        Raises:
            LotError: ``quantity`` is zero.
        """
        if quantity.is_zero():
            raise LotError("a trade moves a non-zero quantity")
        direction = 1 if quantity > 0 else -1
        with exact():
            remaining = abs(quantity)
        relieved: list[Relief] = []
        while remaining and self._lots and direction != self._direction:
            lot = self._lots[0]
            used = min(lot.quantity, remaining)
            relieved.append(Relief(used, self._relieve(lot, used)))
            with exact():
                remaining -= used
            if lot.quantity.is_zero():
                self._lots.popleft()
        if not self._lots:
            self._direction = 0
        if remaining:
            with exact():
                cost = price * remaining
            self._lots.append(Lot(remaining, cost, price))
            self._direction = direction
        return TradeResult(tuple(relieved), remaining)

    def _relieve(self, lot: Lot, units: Decimal) -> Decimal:
        with exact():
            if units == lot.quantity:
                cost = lot.total_cost
            elif lot.unit_cost is not None:
                cost = lot.unit_cost * units
            else:
                cost = divide(
                    lot.total_cost * units, lot.quantity, self._increment, Rounding.HALF_EVEN
                )
            lot.quantity -= units
            lot.total_cost -= cost
        return cost

    def split(self, multiplier: Decimal) -> None:
        """Multiply every lot's quantity by ``multiplier``; each lot's total cost is unchanged.

        A split of r-for-1 has multiplier r; a stock dividend of d new units per
        unit held has multiplier 1 + d (IRS FAQ: 100 shares at $10 → 200 at $5).

        Raises:
            LotError: ``multiplier`` is not positive.
        """
        if multiplier <= 0:
            raise LotError(f"split multiplier must be positive, got {multiplier}")
        for lot in self._lots:
            with exact():
                lot.quantity *= multiplier
            lot.unit_cost = _exact_quotient(lot.total_cost, lot.quantity)
