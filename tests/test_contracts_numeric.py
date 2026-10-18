# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Tests for exact numeric values (BP1 X4, X5; ADR 0016)."""

import decimal
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from auqua.contracts.errors import InvalidDecimalError, OffGridError
from auqua.contracts.numeric import (
    PRECISION,
    Rounding,
    arithmetic,
    context,
    from_float,
    on_grid,
    quantize,
    require_finite,
)

FINITE = st.decimals(
    allow_nan=False, allow_infinity=False, places=8, min_value=-(10**9), max_value=10**9
)
MODES = st.sampled_from(list(Rounding))


@given(value=FINITE, places=st.integers(min_value=0, max_value=8), mode=MODES)
def test_quantize_matches_decimal_for_powers_of_ten(
    value: Decimal, places: int, mode: Rounding
) -> None:
    """For increments 10**-n the result equals decimal's own quantize (negative zero aside)."""
    increment = Decimal(1).scaleb(-places)
    expected = value.quantize(increment, rounding=mode)
    result = quantize(value, increment, mode)
    assert result == expected
    assert str(result) == str(abs(expected) if expected.is_zero() else expected)


@pytest.mark.parametrize(
    ("value", "increment", "mode", "expected"),
    [
        ("0.375", "0.25", Rounding.HALF_EVEN, "0.50"),
        ("0.125", "0.25", Rounding.HALF_EVEN, "0.00"),
        ("0.125", "0.25", Rounding.HALF_UP, "0.25"),
        ("0.125", "0.25", Rounding.HALF_DOWN, "0.00"),
        ("-0.125", "0.25", Rounding.HALF_UP, "-0.25"),
        ("-0.125", "0.25", Rounding.FLOOR, "-0.25"),
        ("-0.125", "0.25", Rounding.CEILING, "0.00"),
        ("12", "5", Rounding.HALF_EVEN, "10"),
        ("12.5", "5", Rounding.HALF_EVEN, "10"),
        ("17.5", "5", Rounding.HALF_EVEN, "20"),
        ("17.5", "5", Rounding.DOWN, "15"),
        ("17.5", "5", Rounding.UP, "20"),
        ("1.01", "0.1", Rounding.ZERO_FIVE_UP, "1.1"),
        ("1.11", "0.1", Rounding.ZERO_FIVE_UP, "1.1"),
    ],
)
def test_quantize_to_other_increments(
    value: str, increment: str, mode: Rounding, expected: str
) -> None:
    """Increments that are not powers of ten round exactly, in the increment's scale."""
    assert str(quantize(Decimal(value), Decimal(increment), mode)) == expected


def test_quantize_never_returns_negative_zero() -> None:
    """A negative value that rounds to zero gives canonical (unsigned) zero."""
    result = quantize(Decimal("-0.001"), Decimal("0.01"), Rounding.HALF_EVEN)
    assert result.is_zero()
    assert not result.is_signed()


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "-0", "-0.00"])
def test_non_canonical_decimals_refused(value: str) -> None:
    """NaN, infinities and negative zero are refused."""
    with pytest.raises(InvalidDecimalError):
        require_finite(Decimal(value), "x")


@pytest.mark.parametrize("increment", ["0", "-0.01", "NaN"])
def test_invalid_increment_refused(increment: str) -> None:
    """Increments must be finite and positive."""
    with pytest.raises(InvalidDecimalError):
        quantize(Decimal(1), Decimal(increment), Rounding.HALF_EVEN)


def test_on_grid() -> None:
    """Values on the grid come back canonical; values off it are refused."""
    assert str(on_grid(Decimal("100.5"), Decimal("0.25"))) == "100.50"
    with pytest.raises(OffGridError):
        on_grid(Decimal("100.10"), Decimal("0.25"))


def test_from_float_is_exact_then_rounded() -> None:
    """0.1 as a float is slightly above 0.1; rounding is explicit."""
    assert str(from_float(0.1, Decimal("0.01"), Rounding.HALF_EVEN)) == "0.10"
    assert str(from_float(2.675, Decimal("0.01"), Rounding.HALF_UP)) == "2.67"
    with pytest.raises(InvalidDecimalError):
        from_float(float("nan"), Decimal("0.01"), Rounding.HALF_EVEN)


def test_float_mixing_raises_under_the_context() -> None:
    """Mixing a float into decimal arithmetic raises (FloatOperation trapped)."""
    with arithmetic(), pytest.raises(decimal.FloatOperation):
        _ = Decimal("1.5") < 2.0  # noqa: PLR2004


def test_context_traps_and_precision() -> None:
    """The trading context traps float mixing and invalid operations."""
    ctx = context()
    assert ctx.prec == PRECISION
    assert ctx.traps[decimal.FloatOperation]
    assert ctx.traps[decimal.InvalidOperation]
    assert ctx.traps[decimal.DivisionByZero]


def test_precision_covers_three_sixteen_digit_operands() -> None:
    """A product of three 16-significant-digit operands is exact under the context."""
    operand = Decimal("9999999999999999")
    with arithmetic() as ctx:
        ctx.traps[decimal.Inexact] = True
        product = operand * operand * operand
    assert product == Decimal(9999999999999999**3)
