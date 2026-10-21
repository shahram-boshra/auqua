# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Data-quality checks at ingestion (BP4 section 4.7; Strategy Module 1).

Gaps are reported, never filled: a missing interval becomes a
:class:`DataGap` event with a reason, and no synthetic record is created.
Duplicates and out-of-order rows inside one raw file are refused by the
parsers before these checks run.
"""

import datetime
import uuid
from collections.abc import Iterable, Iterator

from auqua.contracts.events.ingest import DataGap, GapReason
from auqua.contracts.identity import derived_id
from auqua.ingestion.availability import is_business_day

#: Namespace of gap ids (name-based, so a re-run yields the same ids).
GAP_NAMESPACE = derived_id(uuid.NAMESPACE_URL, b"https://github.com/shahram-boshra/auqua#gap")
_NS_PER_DAY = 86_400 * 1_000_000_000


def _gap(  # noqa: PLR0913 - every argument is a field of the gap
    *,
    source: str,
    key: str,
    start_ns: int,
    end_ns: int,
    reason: GapReason,
    interval_ns: int | None,
    ingest_ns: int,
) -> DataGap:
    return DataGap(
        id=derived_id(
            GAP_NAMESPACE,
            source.encode(),
            key.encode(),
            start_ns.to_bytes(8, "big"),
            end_ns.to_bytes(8, "big"),
            reason.value.encode(),
        ),
        source=source,
        ts_event_ns=start_ns,
        ts_ingest_ns=ingest_ns,
        key=key,
        start_ns=start_ns,
        end_ns=end_ns,
        reason=reason,
        interval_ns=interval_ns,
    )


def interval_gaps(  # noqa: PLR0913 - the grid and the gap fields
    starts: Iterable[int],
    *,
    first_ns: int,
    end_ns: int,
    interval_ns: int,
    source: str,
    key: str,
    reason: GapReason,
    ingest_ns: int,
) -> Iterator[DataGap]:
    """Gaps in a regular grid ``first_ns, first_ns + interval_ns, …`` before ``end_ns``.

    ``starts`` are the interval starts present, ascending. Each maximal run of
    missing intervals is one gap.
    """
    expected = first_ns
    for start in starts:
        if start > expected:
            yield _gap(
                source=source,
                key=key,
                start_ns=expected,
                end_ns=start,
                reason=reason,
                interval_ns=interval_ns,
                ingest_ns=ingest_ns,
            )
        expected = start + interval_ns
    if expected < end_ns:
        yield _gap(
            source=source,
            key=key,
            start_ns=expected,
            end_ns=end_ns,
            reason=reason,
            interval_ns=interval_ns,
            ingest_ns=ingest_ns,
        )


def business_day_gaps(
    days: Iterable[datetime.date],
    *,
    source: str,
    key: str,
    ingest_ns: int,
) -> Iterator[DataGap]:
    """US business days missing between the first and last of ``days`` (ascending).

    Each maximal run of missing business days is one gap over whole UTC days.
    Publishers may also close on days that are not federal holidays, so such
    gaps are findings to review, not errors.
    """
    present = list(days)
    if not present:
        return
    wanted = set(present)
    day, last = present[0], present[-1]
    run_start: datetime.date | None = None
    while day <= last:
        missing = is_business_day(day) and day not in wanted
        if missing and run_start is None:
            run_start = day
        if not missing and run_start is not None and is_business_day(day):
            yield _gap(
                source=source,
                key=key,
                start_ns=_day_ns(run_start),
                end_ns=_day_ns(day),
                reason=GapReason.MISSING,
                interval_ns=None,
                ingest_ns=ingest_ns,
            )
            run_start = None
        day += datetime.timedelta(days=1)


def _day_ns(day: datetime.date) -> int:
    return (day - datetime.date(1970, 1, 1)).days * _NS_PER_DAY
