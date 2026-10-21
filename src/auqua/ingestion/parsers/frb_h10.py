# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Federal Reserve Board H.10 daily rates, Data Download Program CSV (BP4 section 4.4).

Layout (series in columns, ``label=include``): six quoted header rows —
``Series Description``, ``Unit:``, ``Multiplier:``, ``Currency:``,
``Unique Identifier:`` and ``Time Period`` (whose cells are the series codes)
— then one row per date ``YYYY-MM-DD`` with a value or ``ND`` (no data) per
series. The H.10 rates are "noon buying rates in New York for cable transfers
payable in the listed currencies", so the observation time is 12:00 New York
time. Quote convention from the series code: ``RXI$US_…`` is US dollars per
unit of the currency, ``RXI_…`` units of the currency per US dollar; other
series keep the published unit text. Values keep their published digits; a
multiplier other than 1 is refused. Availability is the release time
(:mod:`auqua.ingestion.availability`); dates without a known release time are
counted as unavailable and not stored. ``ND`` on a US business day is
reported as a gap.

Board information may be copied and distributed without permission; the Board
asks to be cited as the source (profile ``frb/h10/daily``).
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
from auqua.ingestion.availability import h10_available_ns, new_york_ns
from auqua.ingestion.errors import ParseError
from auqua.ingestion.parsers import ParseContext, ParseResult, ParserSpec, rate_id
from auqua.ingestion.quality import business_day_gaps

PUBLISHER = "FRB"
NO_DATA = "ND"
_HEADERS = (
    "Series Description",
    "Unit:",
    "Multiplier:",
    "Currency:",
    "Unique Identifier:",
    "Time Period",
)
_NOON = datetime.time(12, 0)


def _unit(code: str, currency: str, published: str) -> str:
    if code.startswith("RXI$US_"):
        return f"USD per {currency}"
    if code.startswith("RXI_"):
        return f"{currency} per USD"
    return published


def _series(header: list[list[str]]) -> list[tuple[SeriesId, str]]:
    for row, label in zip(header, _HEADERS, strict=True):
        if not row or row[0].strip() != label:
            raise ParseError(f"header row {label!r} expected, got {row[:1]!r}")
    width = len(header[5])
    if width < 2 or any(len(row) != width for row in header):  # noqa: PLR2004 - date + series
        raise ParseError("header rows differ in width")
    series: list[tuple[SeriesId, str]] = []
    for column in range(1, width):
        identifier, code = header[4][column].strip(), header[5][column].strip()
        if header[2][column].strip() != "1":
            raise ParseError(f"{code}: multiplier {header[2][column]!r} is not 1")
        prefix, _, rest = identifier.partition("/")
        if prefix != "H10" or not rest.endswith(code):
            raise ParseError(f"{code}: unique identifier {identifier!r} is not an H.10 series")
        currency = header[3][column].strip()
        unit = _unit(code, currency, header[1][column].strip())
        try:
            series.append((SeriesId(PUBLISHER, rest), unit))
        except ContractError as error:
            raise ParseError(f"{code}: {error}") from error
    return series


def parse(data: bytes, context: ParseContext) -> ParseResult:
    """Reference rates and gaps of one H.10 Data Download Program CSV.

    Raises:
        ParseError: header layout, multiplier, date order or value format.
    """
    try:
        rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"), newline="")))
    except (UnicodeDecodeError, csv.Error) as error:
        raise ParseError(f"not a CSV file: {error}") from error
    if len(rows) < len(_HEADERS):
        raise ParseError("fewer rows than the header")
    series = _series(rows[: len(_HEADERS)])
    result = ParseResult(availability=AvailabilityBasis.PUBLISHED_SCHEDULE)
    present: dict[SeriesId, list[datetime.date]] = {sid: [] for sid, _ in series}
    previous: datetime.date | None = None
    for line, row in enumerate(rows[len(_HEADERS) :], start=len(_HEADERS) + 1):
        where = f"line {line}"
        if len(row) != len(series) + 1:
            raise ParseError(f"{where}: expected {len(series) + 1} cells, got {len(row)}")
        try:
            day = datetime.date.fromisoformat(row[0])
        except ValueError as error:
            raise ParseError(f"{where}: date {row[0]!r}") from error
        if previous is not None and day <= previous:
            raise ParseError(f"{where}: date {day} not after {previous}")
        previous = day
        published = h10_available_ns(day)
        observed = new_york_ns(day, _NOON)
        for (series_id, unit), cell in zip(series, row[1:], strict=True):
            text = cell.strip()
            if text == NO_DATA:
                continue
            present[series_id].append(day)
            if published is None:
                result.unavailable += 1
                continue
            try:
                result.events.append(
                    ReferenceRate(
                        id=rate_id(context.source, str(series_id), observed),
                        source=context.source,
                        ts_event_ns=observed,
                        ts_ingest_ns=context.ingest_ns,
                        series_id=series_id,
                        value=Decimal(text),
                        unit=unit,
                        ts_published_ns=published,
                    )
                )
            except (InvalidOperation, ContractError) as error:
                raise ParseError(f"{where}: {series_id}: {error}") from error
    for series_id, days in present.items():
        result.gaps.extend(
            business_day_gaps(
                days, source=context.source, key=str(series_id), ingest_ns=context.ingest_ns
            )
        )
    return result


PARSER = ParserSpec("frb-h10-ddp-csv", "1", sys.modules[__name__], parse)
