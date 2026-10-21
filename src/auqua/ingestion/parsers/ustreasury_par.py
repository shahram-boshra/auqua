# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""US Treasury daily par yield curve rates, CSV download (BP4 section 4.4).

Layout: a header ``Date`` followed by tenor labels (``1 Mo``, ``1.5 Month``,
``2 Mo`` … ``30 Yr``; the set differs between years), then one row per
business day, dates ``MM/DD/YYYY``, newest first; an empty cell means the
tenor was not published that day. Rates are percent and keep their published
digits. They derive from "indicative quotations obtained by the Federal Reserve
Bank of New York at approximately 3:30 PM each business day", so the
observation time is 15:30 New York time. The publication time is not
documented: availability follows the declared rule of
:func:`auqua.ingestion.availability.treasury_available_ns` (basis
``declared``). A tenor label outside :data:`TENORS` is refused.

Works of the US Government are not subject to copyright (17 U.S.C. 105;
profile ``ustreasury/par-yield``).
"""

import csv
import datetime
import io
import sys
from decimal import Decimal, InvalidOperation

from auqua.contracts.errors import ContractError
from auqua.contracts.events.ingest import AvailabilityBasis
from auqua.contracts.events.market import ReferenceRate
from auqua.contracts.identity import SeriesId
from auqua.ingestion.availability import new_york_ns, treasury_available_ns
from auqua.ingestion.errors import ParseError
from auqua.ingestion.parsers import ParseContext, ParseResult, ParserSpec, rate_id
from auqua.ingestion.quality import business_day_gaps

PUBLISHER = "UST"
UNIT = "percent"
#: Published tenor labels → series code suffix.
TENORS = {
    "1 Mo": "1M",
    "1.5 Month": "1.5M",
    "2 Mo": "2M",
    "3 Mo": "3M",
    "4 Mo": "4M",
    "6 Mo": "6M",
    "1 Yr": "1Y",
    "2 Yr": "2Y",
    "3 Yr": "3Y",
    "5 Yr": "5Y",
    "7 Yr": "7Y",
    "10 Yr": "10Y",
    "20 Yr": "20Y",
    "30 Yr": "30Y",
}
_QUOTE_TIME = datetime.time(15, 30)


def series_id(label: str) -> SeriesId:
    """Series of one published tenor label, e.g. ``UST:PAR/10Y``.

    Raises:
        ParseError: unknown label.
    """
    code = TENORS.get(label.strip())
    if code is None:
        raise ParseError(f"unknown tenor label {label!r}")
    return SeriesId(PUBLISHER, f"PAR/{code}")


def parse(data: bytes, context: ParseContext) -> ParseResult:
    """Reference rates and gaps of one par-yield CSV.

    Raises:
        ParseError: header, date format or order, or value format.
    """
    try:
        rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"), newline="")))
    except (UnicodeDecodeError, csv.Error) as error:
        raise ParseError(f"not a CSV file: {error}") from error
    if not rows or not rows[0] or rows[0][0].strip() != "Date":
        raise ParseError("first header cell must be 'Date'")
    tenors = [series_id(label) for label in rows[0][1:]]
    if len(set(tenors)) != len(tenors):
        raise ParseError("repeated tenor column")
    result = ParseResult(availability=AvailabilityBasis.DECLARED)
    present: dict[SeriesId, list[datetime.date]] = {sid: [] for sid in tenors}
    dates: list[datetime.date] = []
    rates: list[ReferenceRate] = []
    for line, row in enumerate(rows[1:], start=2):
        where = f"line {line}"
        if len(row) != len(tenors) + 1:
            raise ParseError(f"{where}: expected {len(tenors) + 1} cells, got {len(row)}")
        try:
            day = datetime.datetime.strptime(row[0].strip(), "%m/%d/%Y").date()  # noqa: DTZ007
        except ValueError as error:
            raise ParseError(f"{where}: date {row[0]!r}") from error
        dates.append(day)
        published = treasury_available_ns(day, context.business_days)
        observed = new_york_ns(day, _QUOTE_TIME)
        for sid, cell in zip(tenors, row[1:], strict=True):
            text = cell.strip()
            if not text:
                continue
            present[sid].append(day)
            if published is None:
                result.unavailable += 1
                continue
            try:
                rates.append(
                    ReferenceRate(
                        id=rate_id(context.source, str(sid), observed),
                        source=context.source,
                        ts_event_ns=observed,
                        ts_ingest_ns=context.ingest_ns,
                        series_id=sid,
                        value=Decimal(text),
                        unit=UNIT,
                        ts_published_ns=published,
                    )
                )
            except (InvalidOperation, ContractError) as error:
                raise ParseError(f"{where}: {sid}: {error}") from error
    if len(set(dates)) != len(dates) or dates not in (sorted(dates), sorted(dates, reverse=True)):
        raise ParseError("dates are repeated or not in one order")
    rates.sort(key=lambda rate: (rate.ts_event_ns, str(rate.series_id)))
    result.events.extend(rates)
    for sid, days in present.items():
        result.gaps.extend(
            business_day_gaps(
                sorted(days), source=context.source, key=str(sid), ingest_ns=context.ingest_ns
            )
        )
    return result


PARSER = ParserSpec("ustreasury-par-yield-csv", "1", sys.modules[__name__], parse)
