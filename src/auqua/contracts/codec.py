# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""JSON codecs and JSON Schema export for canonical events (ADR 0014).

A codec decodes the current event types and any registered legacy versions;
a legacy event is upcast, one registered step at a time, until it is current.
An unknown tag (unknown type or version) is refused, never reinterpreted.
"""

import functools
import operator
from collections.abc import Callable, Mapping
from typing import Any

import msgspec

from auqua.contracts.errors import EventDecodeError
from auqua.contracts.events import CanonicalEvent
from auqua.contracts.events.base import Event, LogRecord
from auqua.contracts.events.research import upcast_run_started
from auqua.contracts.events.trading import RunStarted

#: Upcaster: converts one legacy event version into the next version.
Upcaster = Callable[[Any], Event]

_ENCODER = msgspec.json.Encoder()


def encode(value: Event | LogRecord[Any]) -> bytes:
    """Encode an event or log record as JSON (decimals as strings)."""
    return _ENCODER.encode(value)


def _union(types: tuple[type[Event], ...]) -> Any:  # noqa: ANN401 - a runtime union of types
    return functools.reduce(operator.or_, types)


class EventCodec:
    """Decoder for one set of current event types plus legacy versions."""

    def __init__(
        self,
        current: tuple[type[Event], ...],
        upcasters: Mapping[type[Event], Upcaster] | None = None,
    ) -> None:
        """Build decoders for ``current`` types and the keys of ``upcasters``.

        Args:
            current: The current event types.
            upcasters: Legacy type -> function returning the next version.

        Raises:
            ValueError: a legacy type is also current, or no type is given.
        """
        self._upcasters = dict(upcasters or {})
        if not current:
            raise ValueError("at least one current event type is required")
        overlap = set(current) & set(self._upcasters)
        if overlap:
            raise ValueError(
                f"types both current and legacy: {sorted(t.__name__ for t in overlap)}"
            )
        self._current = frozenset(current)
        accepted = _union((*current, *self._upcasters))
        self._event_decoder = msgspec.json.Decoder(accepted)
        self._record_decoder = msgspec.json.Decoder(LogRecord[accepted])  # type: ignore[valid-type]

    def _upcast(self, event: Event) -> Event:
        while type(event) not in self._current:
            event = self._upcasters[type(event)](event)
        return event

    def decode(self, data: bytes) -> Event:
        """Decode one event, upcast to its current version.

        Raises:
            EventDecodeError: unknown type or version, unknown or missing field,
                or a contract violation.
        """
        try:
            event: Event = self._event_decoder.decode(data)
        except msgspec.ValidationError as error:
            raise EventDecodeError(str(error)) from error
        return self._upcast(event)

    def decode_record(self, data: bytes) -> LogRecord[Event]:
        """Decode one log record; its event is upcast to the current version.

        Raises:
            EventDecodeError: as :meth:`decode`, or an invalid record envelope.
        """
        try:
            record: LogRecord[Event] = self._record_decoder.decode(data)
        except msgspec.ValidationError as error:
            raise EventDecodeError(str(error)) from error
        event = self._upcast(record.event)
        return record if event is record.event else LogRecord(record.sequence, event)

    def json_schema(self) -> dict[str, Any]:
        """JSON Schema (2020-12) of the current event types."""
        return msgspec.json.schema(_union(tuple(sorted(self._current, key=lambda t: t.__name__))))


#: Codec for :data:`auqua.contracts.events.CanonicalEvent`; legacy versions are upcast.
CANONICAL = EventCodec(CanonicalEvent.__args__, {RunStarted: upcast_run_started})
