# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Daily series and simple returns shared by research and allocation (BP6 F-A, section 4.9).

One implementation, used by :mod:`auqua.analytics.experiment` (daily equity)
and :mod:`auqua.portfolio.allocation` (daily closes), on primitive inputs:

- :func:`daily_last` groups values by UTC day of a time the **caller** chooses
  and keeps the last value of each day in input order. The rule of which time
  dates a value is the caller's (BP6 amendment 10, F-V): research dates equity
  by the availability of the bar after which it was recorded; allocation dates
  a close by its interval end.
- :func:`simple_returns` is the Decimal-to-float boundary: each return is
  formed exactly in ``Decimal`` and converted to ``float`` once (ADR 0031).
- :func:`periods_per_year` is BP5's annualisation predicate: 365 calendar days
  for crypto, 252 business days otherwise.
"""

import itertools
from collections.abc import Sequence
from decimal import Decimal

from auqua.contracts.instruments import AssetClass
from auqua.contracts.numeric import arithmetic

NS_PER_DAY = 86_400 * 1_000_000_000
CALENDAR_DAYS = 365
BUSINESS_DAYS = 252


class SeriesError(ValueError):
    """Too few values, or a value that has no simple return."""


def daily_last[V](times: Sequence[int], values: Sequence[V]) -> list[tuple[int, V]]:
    """``(day, value)`` per UTC day of ``times``: the day's last value, days in first-seen order.

    ``day`` is ``time // NS_PER_DAY``.

    Raises:
        ValueError: ``times`` and ``values`` differ in length.
    """
    by_day: dict[int, V] = {}
    for time, value in zip(times, values, strict=True):
        by_day[time // NS_PER_DAY] = value
    return list(by_day.items())


def simple_returns(values: Sequence[Decimal]) -> list[float]:
    """Simple returns of consecutive values, each formed exactly and converted to ``float`` once.

    Raises:
        SeriesError: fewer than two values, or a value before the last that is not positive.
    """
    if len(values) < 2:  # noqa: PLR2004 - a return needs two values
        raise SeriesError("returns need at least two equity values")
    out: list[float] = []
    with arithmetic():
        for previous, current in itertools.pairwise(values):
            if previous <= 0:
                raise SeriesError(f"equity {previous} is not positive")
            out.append(float((current - previous) / previous))
    return out


def periods_per_year(asset_class: AssetClass) -> int:
    """Annualisation factor: :data:`CALENDAR_DAYS` for crypto, :data:`BUSINESS_DAYS` otherwise."""
    return CALENDAR_DAYS if asset_class is AssetClass.CRYPTO else BUSINESS_DAYS
