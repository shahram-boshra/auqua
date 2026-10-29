# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Day counts and accrued interest from verified sources only (BP6 section 4.5 F; T11).

- 30/360: MSRB Rule G-33(e)(ii), "Day Count Formula":
  "Number of Days = (Y2 - Y1) 360 + (M2 - M1) 30 + (D2 - D1)"; if "D2" is 31
  and "D1" is 30 or 31, "D2" becomes 30; if "D1" is 31, it becomes 30. G-33
  has no other day adjustment (none for February).
- US Treasury notes and bonds (``DayCount.ACT_ACT_TREASURY``, semiannual coupons
  only): 31 CFR 356, Appendix B. The daily interest
  decimal is "1/181, 1/182, 1/183, or 1/184 of a full semiannual interest
  payment", multiplied "by the number of days for which interest has
  accrued", rounded "to five decimal places for a $1,000 par amount, using
  normal rounding procedures". The rule states no rounding of the daily
  decimal, so the accrued interest per $1,000 is one rational rounded once.
  "Normal rounding" is not defined for an exact tie, so ``HALF_UP`` is used
  and an exact tie is refused (BP6 R11).

Every other convention (``ACT_360``, ``ACT_365_FIXED``, ``ACT_ACT_ICMA``,
``ACT_ACT_ISDA``) is refused until its primary text is read (BP6 R2, P9).
"""

import datetime
import math
from collections.abc import Sequence
from decimal import Decimal

from auqua.contracts.instruments import Bond, DayCount
from auqua.contracts.numeric import Rounding, divide, exact
from auqua.portfolio.errors import DayCountNotSupportedError, PortfolioError, RoundingTieError

#: Five decimal places per $1,000 (31 CFR 356, App. B, I.D.4).
ACCRUED_INCREMENT = Decimal("0.00001")
CENT = Decimal("0.01")
_THIRTY = 30
_THIRTY_ONE = 31


_SOURCED = frozenset({DayCount.THIRTY_360, DayCount.ACT_ACT_TREASURY})
#: The Treasury rule is stated for semiannual interest (BP6 amendment 2).
_SEMIANNUAL = 2


def require_supported(convention: DayCount) -> None:
    """Refuse a convention without a verified source.

    Raises:
        DayCountNotSupportedError: anything but ``THIRTY_360`` and ``ACT_ACT_TREASURY``.
    """
    if convention not in _SOURCED:
        raise DayCountNotSupportedError(f"day count {convention} has no verified source")


def require_bond_day_count(bond: Bond) -> DayCount:
    """The bond's day count if it is sourced for this bond.

    ``ACT_ACT_TREASURY`` divides "a full semiannual interest payment" (31 CFR
    356, App. B), so it is accepted only for ``coupon_frequency == 2``.

    Raises:
        DayCountNotSupportedError: an unsourced convention, or the Treasury
            rule for a bond that does not pay semiannually.
    """
    require_supported(bond.day_count)
    if bond.day_count is DayCount.ACT_ACT_TREASURY and bond.coupon_frequency != _SEMIANNUAL:
        raise DayCountNotSupportedError(
            f"act_act_treasury needs semiannual coupons, {bond.id} pays {bond.coupon_frequency}"
        )
    return bond.day_count


def thirty_360_days(start: datetime.date, end: datetime.date) -> int:
    """Days from ``start`` (included) to ``end`` under MSRB Rule G-33(e)(ii)."""
    d1, d2 = start.day, end.day
    if d2 == _THIRTY_ONE and d1 in {_THIRTY, _THIRTY_ONE}:
        d2 = _THIRTY
    if d1 == _THIRTY_ONE:
        d1 = _THIRTY
    return (end.year - start.year) * 360 + (end.month - start.month) * 30 + (d2 - d1)


def _half_up_refusing_ties(numerator: Decimal, denominator: Decimal, step: Decimal) -> Decimal:
    up = divide(numerator, denominator, step, Rounding.HALF_UP)
    if up != divide(numerator, denominator, step, Rounding.HALF_DOWN):
        raise RoundingTieError(f"{numerator}/{denominator} is an exact tie at {step}")
    return up


def treasury_accrued_per_1000(coupon_rate: Decimal, periods: Sequence[tuple[int, int]]) -> Decimal:
    """Accrued interest per $1,000 par of a Treasury note or bond (31 CFR 356, App. B).

    ``periods`` lists (days accrued, days in the half-year) for the one or two
    half-years involved; the result is ``1000 * coupon_rate / 2 * sum(days /
    half_year)``, computed as one rational and rounded once to five decimals.

    Raises:
        PortfolioError: no period, or a period with negative days or a
            non-positive length.
        RoundingTieError: the exact value is a tie at the fifth decimal.
    """
    if not periods or any(days < 0 or length <= 0 for days, length in periods):
        raise PortfolioError(f"invalid accrual periods {periods}")
    product = math.prod(length for _, length in periods)
    weight = sum(days * (product // length) for days, length in periods)
    with exact():
        numerator = Decimal(1000) * coupon_rate * weight
    return _half_up_refusing_ties(numerator, Decimal(2 * product), ACCRUED_INCREMENT)


def treasury_accrued_amount(per_1000: Decimal, par: Decimal) -> Decimal:
    """Accrued interest payable for ``par``: ``par / 1000`` times the per-$1,000 amount, in cents.

    31 CFR 356, App. B: "11 is multiplied by $36.72798, resulting in an
    amount payable of $404.00778" — "($404.01)".

    Raises:
        RoundingTieError: the product is an exact tie at the cent.
    """
    with exact():
        numerator = per_1000 * par
    return _half_up_refusing_ties(numerator, Decimal(1000), CENT)
