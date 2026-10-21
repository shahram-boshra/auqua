# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Kline (candlestick) CSV files to canonical bars — walking skeleton (thin); owned by BP4.

The column layout is the published spot kline layout of the
``binance/binance-public-data`` repository (MIT): open time, open, high, low,
close, volume, close time, quote asset volume, number of trades, taker buy
base volume, taker buy quote volume, ignore. Only the layout is used here; a
caller loads only files it is entitled to use (decision log a20). Times are
integers in the unit the caller states (that layout uses milliseconds before
2025 and microseconds from 2025 for spot data); nothing is guessed.

Every price must lie on the instrument's price grid and every volume on its
quantity grid; values off the grid are refused, never rounded, and values on
the grid are brought to the canonical scale (BP1 section 4.2). Event ids are
derived deterministically from the source, instrument and open time (BP1
section 4.3), and the ingestion time comes from the clock passed in (the wall
clock at the ingestion boundary, ADR 0009).
"""

import csv
import uuid
from collections.abc import Iterator
from decimal import Decimal, InvalidOperation
from enum import IntEnum
from pathlib import Path

from auqua.clock.base import Clock
from auqua.contracts.errors import ContractError
from auqua.contracts.events.market import Bar
from auqua.contracts.identity import derived_id
from auqua.contracts.instruments import AnyInstrument
from auqua.contracts.numeric import on_grid

#: Namespace of ingested bar ids, itself name-based so its origin is reproducible
#: (changing it changes every derived id).
BAR_NAMESPACE = derived_id(uuid.NAMESPACE_URL, b"https://github.com/shahram-boshra/auqua#bar")
_COLUMNS = 12


class TimeUnit(IntEnum):
    """Unit of the integer times in a file, as nanoseconds per unit."""

    MILLISECONDS = 1_000_000
    MICROSECONDS = 1_000


class KlineFormatError(ValueError):
    """A row does not follow the layout, the grid or the stated interval."""


def _decimal(text: str, increment: Decimal, where: str) -> Decimal:
    try:
        return on_grid(Decimal(text), increment)
    except (InvalidOperation, ContractError) as error:
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
    """Yield one :class:`Bar` per row of ``path``, in file order.

    Raises:
        KlineFormatError: wrong column count, non-integer time, a close time
            that does not match ``interval_ns``, or a value off the grid.
    """
    unit_ns = int(time_unit)
    if interval_ns <= 0 or interval_ns % unit_ns:
        raise ValueError(f"interval_ns must be a positive multiple of {unit_ns}")
    with path.open(newline="", encoding="ascii") as handle:
        for line, row in enumerate(csv.reader(handle), start=1):
            where = f"{path}:{line}"
            if len(row) != _COLUMNS:
                raise KlineFormatError(f"{where}: expected {_COLUMNS} columns, got {len(row)}")
            try:
                open_time, close_time = int(row[0]), int(row[6])
            except ValueError as error:
                raise KlineFormatError(f"{where}: times must be integers") from error
            start_ns = open_time * unit_ns
            if (close_time + 1) * unit_ns != start_ns + interval_ns:
                raise KlineFormatError(f"{where}: close time does not match the interval")
            price_step, quantity_step = instrument.price_increment, instrument.quantity_increment
            try:
                yield Bar(
                    id=derived_id(
                        BAR_NAMESPACE,
                        source.encode(),
                        str(instrument.id).encode(),
                        start_ns.to_bytes(8, "big"),
                    ),
                    source=source,
                    ts_event_ns=start_ns + interval_ns,
                    ts_ingest_ns=clock.now_ns(),
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
