# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Exact numeric values (ADR 0008, ADR 0016).

Money, prices and quantities are :class:`decimal.Decimal` values. Every
rounding goes through one path with an explicit :class:`Rounding` mode:
:func:`quantize` for a value and :func:`divide` for a quotient, which is
computed exactly and rounded once (BP6 D1). Binary floats enter only through
:func:`from_float`.

Rounding to an increment is computed on exact rationals, so increments that
are not powers of ten (for example ``0.25`` or ``5``) round exactly.
"""

import decimal
import math
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction

from auqua.contracts.errors import InvalidDecimalError, OffGridError

#: Significant digits of the arithmetic context. A product of operands with
#: n and m significant digits has at most n + m digits, so products of three
#: operands of up to 16 significant digits each are exact.
PRECISION = 50
_TRAPS = (
    decimal.FloatOperation,
    decimal.InvalidOperation,
    decimal.DivisionByZero,
    decimal.Overflow,
)


class Rounding(StrEnum):
    """Rounding modes; the values are the :mod:`decimal` mode names."""

    CEILING = decimal.ROUND_CEILING
    FLOOR = decimal.ROUND_FLOOR
    DOWN = decimal.ROUND_DOWN
    UP = decimal.ROUND_UP
    HALF_UP = decimal.ROUND_HALF_UP
    HALF_DOWN = decimal.ROUND_HALF_DOWN
    HALF_EVEN = decimal.ROUND_HALF_EVEN
    ZERO_FIVE_UP = decimal.ROUND_05UP


def context() -> decimal.Context:
    """Arithmetic context for trading state.

    Mixing floats with decimals, invalid operations, division by zero and
    overflow raise. The context's own rounding applies only to arithmetic
    results beyond :data:`PRECISION`; rounding to an instrument grid always
    goes through :func:`quantize`.
    """
    return decimal.Context(
        prec=PRECISION,
        rounding=decimal.ROUND_HALF_EVEN,
        traps=list(_TRAPS),
    )


@contextmanager
def arithmetic() -> Iterator[decimal.Context]:
    """Run a block under :func:`context` (a local copy, restored on exit)."""
    with decimal.localcontext(context()) as ctx:
        yield ctx


def require_finite(value: Decimal, name: str) -> None:
    """Refuse NaN, infinities and negative zero.

    Raises:
        InvalidDecimalError: ``value`` is not a finite, canonical decimal.
    """
    if not value.is_finite():
        raise InvalidDecimalError(f"{name} must be finite, got {value}")
    if value.is_zero() and value.is_signed():
        raise InvalidDecimalError(f"{name} must not be negative zero")


def require_increment(increment: Decimal, name: str = "increment") -> None:
    """Refuse increments that are not finite and strictly positive.

    Raises:
        InvalidDecimalError: ``increment`` is not finite or not positive.
    """
    require_finite(increment, name)
    if increment <= 0:
        raise InvalidDecimalError(f"{name} must be positive, got {increment}")


def _exponent(increment: Decimal) -> int:
    exponent = increment.as_tuple().exponent
    if not isinstance(exponent, int):  # pragma: no cover - finite values have int exponents
        raise InvalidDecimalError(f"increment must be finite, got {increment}")
    return min(exponent, 0)


def _round_ratio(numerator: int, denominator: int, rounding: Rounding) -> int:  # noqa: PLR0911
    """Round ``numerator / denominator`` (denominator > 0) to an integer."""
    floor, remainder = divmod(numerator, denominator)
    if remainder == 0:
        return floor
    ceiling = floor + 1
    toward_zero, away = (floor, ceiling) if numerator > 0 else (ceiling, floor)
    match rounding:
        case Rounding.CEILING:
            return ceiling
        case Rounding.FLOOR:
            return floor
        case Rounding.DOWN:
            return toward_zero
        case Rounding.UP:
            return away
        case Rounding.ZERO_FIVE_UP:
            return away if abs(toward_zero) % 5 == 0 else toward_zero
    twice = 2 * remainder
    if twice < denominator:
        return floor
    if twice > denominator:
        return ceiling
    match rounding:
        case Rounding.HALF_UP:
            return away
        case Rounding.HALF_DOWN:
            return toward_zero
        case _:
            return floor if floor % 2 == 0 else ceiling


def quantize(value: Decimal, increment: Decimal, rounding: Rounding) -> Decimal:
    """Round ``value`` to the nearest multiple of ``increment`` under ``rounding``.

    The result carries the increment's scale (at most zero exponent), so equal
    values on the same grid have the same text form.

    Raises:
        InvalidDecimalError: ``value`` or ``increment`` is not finite, or ``increment``
            is not positive.
        decimal.Inexact: the result needs more than :data:`PRECISION` digits.
    """
    require_finite(value, "value")
    require_increment(increment)
    return _to_grid(Fraction(value), increment, rounding)


def divide(
    numerator: Decimal, denominator: Decimal, increment: Decimal, rounding: Rounding
) -> Decimal:
    """Round the exact quotient ``numerator / denominator`` once onto ``increment``.

    The quotient is an exact rational, so nothing is rounded before the one
    rounding under ``rounding``; the result carries the increment's scale, as
    :func:`quantize` (BP6 D1, ADR 0008).

    Raises:
        InvalidDecimalError: an operand or ``increment`` is not finite, or
            ``increment`` is not positive.
        decimal.DivisionByZero: ``denominator`` is zero.
        decimal.Inexact: the result needs more than :data:`PRECISION` digits.
    """
    require_finite(numerator, "numerator")
    require_finite(denominator, "denominator")
    require_increment(increment)
    if denominator.is_zero():
        raise decimal.DivisionByZero(f"{numerator} divided by zero")
    return _to_grid(Fraction(numerator) / Fraction(denominator), increment, rounding)


def _to_grid(value: Fraction, increment: Decimal, rounding: Rounding) -> Decimal:
    """The one rounding: ``value`` to a multiple of ``increment`` under ``rounding``."""
    ratio = value / Fraction(increment)
    units = _round_ratio(ratio.numerator, ratio.denominator, Rounding(rounding))
    with arithmetic() as ctx:
        ctx.traps[decimal.Inexact] = True
        return (Decimal(units) * increment).quantize(Decimal(1).scaleb(_exponent(increment)))


def on_grid(value: Decimal, increment: Decimal) -> Decimal:
    """Return ``value`` in canonical form if it is an exact multiple of ``increment``.

    Raises:
        OffGridError: ``value`` is not a multiple of ``increment``.
    """
    require_finite(value, "value")
    require_increment(increment)
    if (Fraction(value) / Fraction(increment)).denominator != 1:
        raise OffGridError(f"{value} is not a multiple of {increment}")
    return quantize(value, increment, Rounding.HALF_EVEN)


def from_float(value: float, increment: Decimal, rounding: Rounding) -> Decimal:
    """Convert a binary float (research output) to trading state.

    The float is converted exactly, then rounded to ``increment``.

    Raises:
        InvalidDecimalError: ``value`` is NaN or infinite.
    """
    if not math.isfinite(value):
        raise InvalidDecimalError(f"value must be finite, got {value}")
    return quantize(Decimal.from_float(value), increment, rounding)
