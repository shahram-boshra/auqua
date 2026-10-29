# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Canonical event base type (ADR 0014).

Rules for every event type:

- a frozen, tagged ``msgspec.Struct`` subclass of :class:`Event`, whose tag
  ``<domain>.<name>.v<N>`` names its schema version;
- adding an optional field with a default is compatible within a version;
  removing, renaming or re-typing a field creates version ``N + 1`` plus a
  registered upcaster from version ``N`` (:mod:`auqua.contracts.codec`);
- decimals are finite and canonical; no field is a binary float;
- (``source``, ``id``) is unique for each distinct event.

The log ``sequence`` is not part of an event: the event log wraps each stored
event in a :class:`LogRecord`.
"""

import uuid

import msgspec

from auqua.contracts.errors import InvalidFieldError
from auqua.contracts.identity import require_source
from auqua.contracts.validation import require_time_ns


class Event(msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag_field="type"):
    """Fields common to every canonical event.

    Attributes:
        id: UUIDv7 of the event, unique per ``source``.
        source: Producer name, e.g. ``ingest/binance/spot``.
        ts_event_ns: When it happened at the origin (event or exchange time).
        ts_ingest_ns: When the platform observed it; for internally generated
            events, the log-clock time of the record that caused it.
    """

    id: uuid.UUID
    source: str
    ts_event_ns: int
    ts_ingest_ns: int

    def __post_init__(self) -> None:
        """Validate the source name and both times."""
        require_source(self.source)
        require_time_ns(self.ts_event_ns, "ts_event_ns")
        require_time_ns(self.ts_ingest_ns, "ts_ingest_ns")


class LogRecord[E: Event](msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """An event as stored in the event log (BP3 assigns ``sequence`` on append).

    Attributes:
        sequence: Position in the log; strictly increasing and contiguous.
        event: The canonical event, unchanged by the append.
    """

    sequence: int
    event: E

    def __post_init__(self) -> None:
        """Refuse negative sequence numbers."""
        if self.sequence < 0:
            raise InvalidFieldError(f"sequence must be >= 0, got {self.sequence}")
