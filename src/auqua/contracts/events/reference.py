# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Reference-data events (BP3 section 4.5; ADR 0023).

An instrument specification enters the platform as an event in the log, so
the point-in-time reference index is derived from the log and can be rebuilt
from it (Strategy section 4).
"""

from auqua.contracts.errors import InvalidFieldError
from auqua.contracts.events.base import Event
from auqua.contracts.instruments import AnyInstrument


class InstrumentDefined(Event, frozen=True, tag="reference.instrument.v1"):
    """One version of an instrument specification.

    ``instrument.valid_from_ns`` is when the version takes effect and
    ``instrument.knowledge_ns`` when it became available (historical
    availability, Strategy section 5). A later correction is a new event with a
    later ``knowledge_ns``; a version is never edited.
    """

    instrument: AnyInstrument

    def __post_init__(self) -> None:
        """Refuse a version known after the platform observed it."""
        super().__post_init__()
        if self.instrument.knowledge_ns > self.ts_ingest_ns:
            raise InvalidFieldError(
                f"knowledge_ns {self.instrument.knowledge_ns} is after "
                f"ts_ingest_ns {self.ts_ingest_ns}"
            )
