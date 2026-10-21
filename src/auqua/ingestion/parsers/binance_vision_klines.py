# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Binance Vision kline archives to canonical bars (BP4 section 4.4; ADR 0026).

The column layout is the published spot kline layout of the
``binance/binance-public-data`` repository (MIT): open time, open, high, low,
close, volume, close time, quote asset volume, number of trades, taker buy
base volume, taker buy quote volume, ignore. Only the layout is used here; the
data is governed by the Binance Vision Terms of Use (profile
``binance/vision/klines``: research and backtest only; decision log a28).
Times are integers in the unit the caller states (the repository: "The
timestamp for SPOT Data from January 1st 2025 onwards will be in
microseconds"); nothing is guessed. Rows must be in strictly ascending,
contiguous-or-gapped order of open time; repeated or decreasing open times are
refused, and missing intervals become :class:`DataGap` events.

Every price must lie on the instrument's price grid and every volume on its
quantity grid; values off the grid are refused, never rounded, and values on
the grid are brought to the canonical scale (BP1 section 4.2). Event ids are
derived deterministically from the source, instrument, interval and open time
(BP1 section 4.3; BP4 section 4.3 adds the interval, so bars of different
intervals never collide).
"""

import csv
import io
import sys
from collections.abc import Callable, Iterable, Iterator
from decimal import Decimal, InvalidOperation
from pathlib import Path

from auqua.clock.base import Clock
from auqua.contracts.errors import ContractError
from auqua.contracts.events.ingest import AvailabilityBasis, GapReason
from auqua.contracts.events.market import Bar
from auqua.contracts.instruments import AnyInstrument
from auqua.contracts.numeric import on_grid
from auqua.ingestion.errors import ParseError
from auqua.ingestion.parsers import ParseContext, ParseResult, ParserSpec, TimeUnit, bar_id
from auqua.ingestion.quality import interval_gaps

__all__ = ["PARSER", "KlineFormatError", "TimeUnit", "parse", "read_klines"]

_COLUMNS = 12


class KlineFormatError(ParseError):
    """A row does not follow the layout, the order, the grid or the stated interval."""


def _decimal(text: str, increment: Decimal, where: str) -> Decimal:
    try:
        return on_grid(Decimal(text), increment)
    except (InvalidOperation, ContractError) as error:
        raise KlineFormatError(f"{where}: {error}") from error


def _bars(  # noqa: PLR0913 - every argument states a fact about the rows
    rows: Iterable[list[str]],
    name: str,
    instrument: AnyInstrument,
    *,
    source: str,
    interval_ns: int,
    time_unit: TimeUnit,
    ingest_ns: Callable[[], int],
) -> Iterator[Bar]:
    unit_ns = int(time_unit)
    if interval_ns <= 0 or interval_ns % unit_ns:
        raise ValueError(f"interval_ns must be a positive multiple of {unit_ns}")
    price_step, quantity_step = instrument.price_increment, instrument.quantity_increment
    previous: int | None = None
    for line, row in enumerate(rows, start=1):
        where = f"{name}:{line}"
        if len(row) != _COLUMNS:
            raise KlineFormatError(f"{where}: expected {_COLUMNS} columns, got {len(row)}")
        try:
            open_time, close_time = int(row[0]), int(row[6])
        except ValueError as error:
            raise KlineFormatError(f"{where}: times must be integers") from error
        start_ns = open_time * unit_ns
        if (close_time + 1) * unit_ns != start_ns + interval_ns:
            raise KlineFormatError(f"{where}: close time does not match the interval")
        if previous is not None and start_ns <= previous:
            raise KlineFormatError(f"{where}: open time not after the previous row")
        if start_ns % interval_ns:
            raise KlineFormatError(f"{where}: open time not on the interval grid")
        previous = start_ns
        try:
            yield Bar(
                id=bar_id(source, str(instrument.id), interval_ns, start_ns),
                source=source,
                ts_event_ns=start_ns + interval_ns,
                ts_ingest_ns=ingest_ns(),
                instrument_id=instrument.id,
                start_ns=start_ns,
                interval_ns=interval_ns,
                open=_decimal(row[1], price_step, where),
                high=_decimal(row[2], price_step, where),
                low=_decimal(row[3], price_step, where),
                close=_decimal(row[4], price_step, where),
                volume=_decimal(row[5], quantity_step, where),
            )
        except ContractError as error:
            raise KlineFormatError(f"{where}: {error}") from error


def read_klines(  # noqa: PLR0913 - every argument states a fact about the file
    path: Path,
    instrument: AnyInstrument,
    *,
    source: str,
    interval_ns: int,
    time_unit: TimeUnit,
    clock: Clock,
) -> Iterator[Bar]:
    """Yield one :class:`Bar` per row of the CSV file ``path``, in file order.

    Raises:
        KlineFormatError: wrong column count, non-integer time, a close time
            that does not match ``interval_ns``, rows out of order, or a value
            off the grid.
    """
    with path.open(newline="", encoding="ascii") as handle:
        yield from _bars(
            csv.reader(handle),
            str(path),
            instrument,
            source=source,
            interval_ns=interval_ns,
            time_unit=time_unit,
            ingest_ns=clock.now_ns,
        )


def parse(data: bytes, context: ParseContext) -> ParseResult:
    """Bars and gaps of one kline CSV (the archive's single member).

    Raises:
        KlineFormatError: as :func:`read_klines`, or not ASCII.
        ParseError: the context lacks the instrument, interval or time unit.
    """
    instrument, interval_ns, unit = context.instrument, context.interval_ns, context.time_unit
    if instrument is None or interval_ns is None or unit is None:
        raise ParseError("kline parsing needs the instrument, the interval and the time unit")
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError as error:
        raise KlineFormatError(f"not ASCII: {error}") from error
    bars = list(
        _bars(
            csv.reader(io.StringIO(text, newline="")),
            "klines",
            instrument,
            source=context.source,
            interval_ns=interval_ns,
            time_unit=unit,
            ingest_ns=lambda: context.ingest_ns,
        )
    )
    result = ParseResult(events=list(bars), availability=AvailabilityBasis.EVENT_END)
    if bars or context.window:
        first, end = context.window or (bars[0].start_ns, bars[-1].start_ns + interval_ns)
        result.gaps = list(
            interval_gaps(
                (bar.start_ns for bar in bars),
                first_ns=first,
                end_ns=end,
                interval_ns=interval_ns,
                source=context.source,
                key=str(instrument.id),
                reason=GapReason.MISSING,
                ingest_ns=context.ingest_ns,
            )
        )
    return result


PARSER = ParserSpec("binance-vision-klines", "1", sys.modules[__name__], parse)
