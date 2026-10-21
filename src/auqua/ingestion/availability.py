# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Availability-time rules of the reference sources (BP4 section 4.6; ADR 0028).

US federal holidays follow 5 U.S.C. 6103(a), with the Uniform Monday Holiday
Act dates from 1971 (Veterans Day on the fourth Monday of October 1971-1977),
Martin Luther King Jr. Day from 1986 and Juneteenth from 2021; a holiday on a
Saturday is observed on the Friday before (6103(b)(1)) and on a Sunday on the
Monday after (Executive Order 11582). Years before 1971 are not covered and
yield no availability time.

- FRB H.10: "On Mondays at 4:15 p.m. the Federal Reserve Board releases daily
  bilateral exchange rates" for the previous business week; "If Monday falls on
  a Federal Holiday, the data will be released on the following business day."
  The weekly release ceased from 2006-05-22 to January 2009: releases computed
  inside that span have no availability time (basis ``unknown``).
- US Treasury par yields: the publication time is not documented; the declared
  rule is 00:00 New York time of the N-th US business day after the observation
  date (basis ``declared``).

New York local times are converted with ``zoneinfo`` (America/New_York).
"""

import datetime
import functools
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
FIRST_COVERED_YEAR = 1971
_H10_GAP = (datetime.date(2006, 5, 22), datetime.date(2009, 1, 31))
_H10_RELEASE = datetime.time(16, 15)
_SATURDAY, _SUNDAY = 5, 6
_MONDAY, _THURSDAY = 0, 3
_NS_PER_SECOND = 1_000_000_000


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> datetime.date:
    first = datetime.date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + datetime.timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> datetime.date:
    following = datetime.date(year + month // 12, month % 12 + 1, 1)
    last = following - datetime.timedelta(days=1)
    return last - datetime.timedelta(days=(last.weekday() - weekday) % 7)


def _observed(day: datetime.date) -> datetime.date:
    if day.weekday() == _SATURDAY:
        return day - datetime.timedelta(days=1)
    if day.weekday() == _SUNDAY:
        return day + datetime.timedelta(days=1)
    return day


@functools.cache
def federal_holidays(year: int) -> frozenset[datetime.date]:
    """Observed US federal holidays falling in ``year``.

    Raises:
        ValueError: ``year`` before :data:`FIRST_COVERED_YEAR`.
    """
    if year < FIRST_COVERED_YEAR:
        raise ValueError(f"federal holidays are covered from {FIRST_COVERED_YEAR}, got {year}")
    fixed = [datetime.date(year, 1, 1), datetime.date(year, 7, 4), datetime.date(year, 12, 25)]
    fixed.append(datetime.date(year + 1, 1, 1))  # observed on 31 December when a Saturday
    if year >= 2021:  # noqa: PLR2004 - Juneteenth National Independence Day Act
        fixed.append(datetime.date(year, 6, 19))
    if year >= 1978:  # noqa: PLR2004 - Veterans Day returned to 11 November
        fixed.append(datetime.date(year, 11, 11))
    days = {_observed(day) for day in fixed}
    days |= {
        _nth_weekday(year, 2, _MONDAY, 3),
        _last_weekday(year, 5, _MONDAY),
        _nth_weekday(year, 9, _MONDAY, 1),
        _nth_weekday(year, 10, _MONDAY, 2),
        _nth_weekday(year, 11, _THURSDAY, 4),
    }
    if year >= 1986:  # noqa: PLR2004 - first observance of Martin Luther King Jr. Day
        days.add(_nth_weekday(year, 1, _MONDAY, 3))
    if year < 1978:  # noqa: PLR2004 - Veterans Day on the fourth Monday of October
        days.add(_nth_weekday(year, 10, _MONDAY, 4))
    return frozenset(day for day in days if day.year == year)


def is_business_day(day: datetime.date) -> bool:
    """Monday to Friday and not an observed US federal holiday."""
    return day.weekday() < _SATURDAY and day not in federal_holidays(day.year)


def next_business_day(day: datetime.date) -> datetime.date:
    """The first US business day after ``day``."""
    following = day + datetime.timedelta(days=1)
    while not is_business_day(following):
        following += datetime.timedelta(days=1)
    return following


def new_york_ns(day: datetime.date, at: datetime.time) -> int:
    """Nanoseconds since the epoch of local time ``at`` on ``day`` in New York."""
    local = datetime.datetime.combine(day, at, tzinfo=NEW_YORK)
    return int(local.timestamp()) * _NS_PER_SECOND


def h10_release_date(observation: datetime.date) -> datetime.date | None:
    """Release date of the H.10 rate for ``observation``, or ``None`` when unknown."""
    if observation.year < FIRST_COVERED_YEAR:
        return None
    monday = observation + datetime.timedelta(days=7 - observation.weekday())
    release = monday if is_business_day(monday) else next_business_day(monday)
    if _H10_GAP[0] <= release <= _H10_GAP[1]:
        return None
    return release


def h10_available_ns(observation: datetime.date) -> int | None:
    """Availability time of the H.10 rate for ``observation`` (16:15 New York), or ``None``."""
    release = h10_release_date(observation)
    return None if release is None else new_york_ns(release, _H10_RELEASE)


def treasury_available_ns(observation: datetime.date, business_days: int) -> int | None:
    """Declared availability of a Treasury par yield: 00:00 New York, N business days later."""
    if observation.year < FIRST_COVERED_YEAR or business_days <= 0:
        return None
    day = observation
    for _ in range(business_days):
        day = next_business_day(day)
    return new_york_ns(day, datetime.time(0, 0))
