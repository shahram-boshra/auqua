# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Bitemporal point-in-time index of instrument versions (ADR 0023; Strategy section 5).

Built only from :class:`InstrumentDefined` records of the event log, so it is
derived state that can always be rebuilt. ``as_of(id, valid_ns, known_ns)``
answers "which specification applied at ``valid_ns``, as known at
``known_ns``": among versions with ``knowledge_ns <= known_ns``, the one with
the greatest ``valid_from_ns <= valid_ns``; between versions with the same
``valid_from_ns``, the later-known one (the correction) wins.
"""

import bisect
from collections.abc import Iterable

from auqua.contracts.events.base import Event, LogRecord
from auqua.contracts.events.reference import InstrumentDefined
from auqua.contracts.identity import InstrumentId
from auqua.contracts.instruments import AnyInstrument


class ReferenceIndex:
    """Instrument versions by id, ordered by (``valid_from_ns``, ``knowledge_ns``)."""

    def __init__(self) -> None:
        """Create an empty index."""
        self._versions: dict[InstrumentId, list[tuple[int, int, int, AnyInstrument]]] = {}

    @classmethod
    def from_records(cls, records: Iterable[LogRecord[Event]]) -> ReferenceIndex:
        """Build the index from log records (other event types are skipped)."""
        index = cls()
        for record in records:
            index.apply(record)
        return index

    def apply(self, record: LogRecord[Event]) -> None:
        """Add the version carried by ``record`` if it is an :class:`InstrumentDefined`."""
        event = record.event
        if not isinstance(event, InstrumentDefined):
            return
        instrument = event.instrument
        key = (instrument.valid_from_ns, instrument.knowledge_ns, record.sequence)
        versions = self._versions.setdefault(instrument.id, [])
        bisect.insort(versions, (*key, instrument), key=lambda v: v[:3])

    def as_of(
        self, instrument_id: InstrumentId, valid_ns: int, known_ns: int
    ) -> AnyInstrument | None:
        """The version in effect at ``valid_ns`` as known at ``known_ns``, if any."""
        best: tuple[int, int, int, AnyInstrument] | None = None
        for version in self._versions.get(instrument_id, ()):
            valid_from, knowledge, _, _ = version
            if valid_from > valid_ns:
                break
            if knowledge <= known_ns:
                best = version
        return None if best is None else best[3]
