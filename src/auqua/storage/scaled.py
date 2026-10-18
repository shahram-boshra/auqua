# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Exact decimals as scaled 64-bit integers for SQLite columns (ADR 0023).

A column declares a fixed ``scale``: the stored integer is ``value * 10**scale``.
Encoding never rounds. A value that is not a whole multiple of ``10**-scale``,
or whose scaled value falls outside SQLite's signed 64-bit integer range, is
refused; rounding is the owner's explicit step before storage (ADR 0016).
"""

from dataclasses import dataclass
from decimal import Decimal

from auqua.storage.errors import EncodingError

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1
#: A signed 64-bit integer holds 18 full decimal digits.
MAX_SCALE = 18


def _power_of_ten(exponent: int) -> int:
    """``10**exponent`` for ``exponent >= 0``, as an exact integer."""
    return int("1" + "0" * exponent)


@dataclass(frozen=True)
class ScaledDecimal:
    """Codec for one column with a fixed number of decimal places."""

    scale: int

    def __post_init__(self) -> None:
        """Refuse a scale outside 0..18."""
        if not 0 <= self.scale <= MAX_SCALE:
            raise ValueError(f"scale must be within 0..{MAX_SCALE}, got {self.scale}")

    def encode(self, value: Decimal) -> int:
        """Return ``value * 10**scale`` exactly.

        Raises:
            EncodingError: not finite, finer than the scale, or outside 64 bits.
        """
        if not value.is_finite():
            raise EncodingError(f"{value} is not finite")
        sign, digits, exponent = value.as_tuple()
        if not isinstance(exponent, int):  # pragma: no cover - finite values have int exponents
            raise EncodingError(f"{value} is not finite")
        coefficient = int("".join(map(str, digits)) or "0")
        shift = exponent + self.scale
        if shift >= 0:
            scaled: int = coefficient * _power_of_ten(shift)
        else:
            scaled, remainder = divmod(coefficient, _power_of_ten(-shift))
            if remainder:
                raise EncodingError(f"{value} has more than {self.scale} decimal places")
        if sign:
            scaled = -scaled
        if not INT64_MIN <= scaled <= INT64_MAX:
            raise EncodingError(f"{value} at scale {self.scale} is outside 64 bits")
        return scaled

    def decode(self, stored: int) -> Decimal:
        """Return the decimal with exactly ``scale`` places.

        Raises:
            EncodingError: ``stored`` is outside 64 bits.
        """
        if not INT64_MIN <= stored <= INT64_MAX:
            raise EncodingError(f"{stored} is outside 64 bits")
        return Decimal(f"{stored}E-{self.scale}")
