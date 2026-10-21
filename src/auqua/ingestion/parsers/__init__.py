# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Parsers from provider layouts to canonical events (BP4 section 4.4; ADR 0026).

Each parser module exposes one :class:`ParserSpec`. A parser maps every row
exactly or refuses the file (:class:`auqua.ingestion.errors.ParseError`):
values are never rounded, guessed or filled. Ids are name-based (BP1 section
4.3) and do not depend on the ingestion time, so a re-run of the same file
finds the records it stored before. ``parser_id`` is the SHA-256 of the
parser module's source, so a changed parser is a different run key.
"""

import hashlib
import inspect
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from types import ModuleType

from auqua.contracts.events.base import Event
from auqua.contracts.events.ingest import AvailabilityBasis, DataGap
from auqua.contracts.identity import derived_id
from auqua.contracts.instruments import AnyInstrument

#: Namespace of ingested bar ids; changing it changes every derived id.
BAR_NAMESPACE = derived_id(uuid.NAMESPACE_URL, b"https://github.com/shahram-boshra/auqua#bar")
#: Namespace of ingested reference-rate ids.
RATE_NAMESPACE = derived_id(uuid.NAMESPACE_URL, b"https://github.com/shahram-boshra/auqua#rate")
#: Namespace of instrument-version ids.
INSTRUMENT_NAMESPACE = derived_id(
    uuid.NAMESPACE_URL, b"https://github.com/shahram-boshra/auqua#instrument"
)


class TimeUnit(IntEnum):
    """Unit of integer times in a file, as nanoseconds per unit."""

    SECONDS = 1_000_000_000
    MILLISECONDS = 1_000_000
    MICROSECONDS = 1_000


@dataclass(frozen=True)
class ParseContext:
    """Facts about one raw file that the file itself does not state.

    Attributes:
        source: Producer name of the events, e.g. ``ingest/coinbase/exchange``.
        ingest_ns: Ingestion time stamped on every event of the run (wall
            clock read once by the runner; ADR 0009).
        instrument: Instrument version the bars belong to (bar sources).
        interval_ns: Bar interval (bar sources).
        window: Requested ``[start_ns, end_ns)`` (paged API sources).
        time_unit: Unit of integer times (archive sources).
        business_days: Declared Treasury availability rule (BP4 section 4.6).
    """

    source: str
    ingest_ns: int
    instrument: AnyInstrument | None = None
    interval_ns: int | None = None
    window: tuple[int, int] | None = None
    time_unit: TimeUnit | None = None
    business_days: int = 1


@dataclass
class ParseResult:
    """Events of one raw file, its gaps, how availability was established, and notes."""

    events: list[Event] = field(default_factory=list)
    gaps: list[DataGap] = field(default_factory=list)
    availability: AvailabilityBasis = AvailabilityBasis.EVENT_END
    unavailable: int = 0
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ParserSpec:
    """A parser: its name, version, module and entry point."""

    name: str
    version: str
    module: ModuleType
    parse: Callable[[bytes, ParseContext], ParseResult]

    @property
    def parser_id(self) -> str:
        """SHA-256 (hex) of the parser module's source file."""
        path = inspect.getsourcefile(self.module)
        if path is None:  # pragma: no cover - parsers are source modules
            raise RuntimeError(f"{self.module.__name__} has no source file")
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def bar_id(source: str, instrument: str, interval_ns: int, start_ns: int) -> uuid.UUID:
    """Id of revision 0 of a bar: source, instrument, interval and start (BP4 section 4.3)."""
    return derived_id(
        BAR_NAMESPACE,
        source.encode(),
        instrument.encode(),
        interval_ns.to_bytes(8, "big"),
        start_ns.to_bytes(8, "big"),
    )


def rate_id(source: str, series: str, observation_ns: int) -> uuid.UUID:
    """Id of revision 0 of a reference-rate observation."""
    return derived_id(
        RATE_NAMESPACE, source.encode(), series.encode(), observation_ns.to_bytes(8, "big")
    )
