# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Reading and replaying the event log (ADR 0021; Strategy sections 5 and 9).

The reader opens the log read-only and yields :class:`LogRecord` objects in
sequence order. Every record is checked before it is yielded: the sequence is
the expected one, the hash chain holds, the payload decodes, and the indexed
columns agree with the decoded event. Any mismatch raises
:class:`LogCorruptionError`. Replay drives :class:`LogClock` from each record,
in live operation and in replay alike, and never writes to the log.
"""

from collections.abc import Callable, Iterator
from pathlib import Path
from types import TracebackType
from typing import Self

import msgspec

from auqua.clock.log import LogClock
from auqua.config.base import Environment
from auqua.contracts.codec import CANONICAL, EventCodec
from auqua.contracts.errors import EventDecodeError
from auqua.contracts.events.base import Event, LogRecord
from auqua.storage.chain import GENESIS, record_hash
from auqua.storage.errors import LogCorruptionError
from auqua.storage.eventlog import Head, check_meta, read_head
from auqua.storage.sqlite import connect_reader, integrity_check


class _Tag(msgspec.Struct):
    """The stored tag of a payload, before any upcast."""

    type: str


_TAG_DECODER = msgspec.json.Decoder(_Tag)


class EventLogReader:
    """Read-only access to one environment's event log."""

    def __init__(
        self,
        path: Path,
        environment: Environment,
        *,
        busy_timeout_ms: int,
        codec: EventCodec = CANONICAL,
    ) -> None:
        """Open the log at ``path`` read-only.

        Raises:
            StorageError: missing file, other environment or other format.
        """
        self._path = path
        self._codec = codec
        self._connection = connect_reader(path, busy_timeout_ms)
        try:
            check_meta(self._connection, path, environment)
        except BaseException:
            self._connection.close()
            raise

    def head(self) -> Head:
        """The head of the log as currently committed."""
        return read_head(self._connection)

    def hash_at(self, sequence: int) -> bytes:
        """The stored chain hash of record ``sequence``.

        Raises:
            LogCorruptionError: no such record.
        """
        row = self._connection.execute(
            "SELECT record_hash FROM log WHERE sequence = ?", (sequence,)
        ).fetchone()
        if row is None:
            raise LogCorruptionError(f"{self._path}: no record {sequence}")
        return bytes(row[0])

    def _previous_hash(self, from_sequence: int) -> bytes:
        return GENESIS if from_sequence == 0 else self.hash_at(from_sequence - 1)

    def records(self, from_sequence: int = 0) -> Iterator[LogRecord[Event]]:
        """Yield verified records from ``from_sequence`` to the current head.

        The records come from one read (one consistent snapshot). Starting
        after 0 verifies the chain from the stored hash of the preceding
        record; :meth:`verify` checks the whole log.

        Raises:
            LogCorruptionError: gap, hash mismatch, undecodable payload or
                inconsistent columns.
            ValueError: ``from_sequence`` is negative.
        """
        for record, _ in self._verified(from_sequence):
            yield record

    def _verified(self, from_sequence: int) -> Iterator[tuple[LogRecord[Event], bytes]]:
        if from_sequence < 0:
            raise ValueError(f"from_sequence must be >= 0, got {from_sequence}")
        previous = self._previous_hash(from_sequence)
        expected = from_sequence
        rows = self._connection.execute(
            "SELECT sequence, source, id, type, ts_event_ns, ts_ingest_ns, payload, record_hash "
            "FROM log WHERE sequence >= ? ORDER BY sequence",
            (from_sequence,),
        )
        for sequence, source, raw_id, tag, ts_event, ts_ingest, payload, stored in rows:
            where = f"{self._path}: sequence {sequence}"
            if sequence != expected:
                raise LogCorruptionError(f"{where}: expected sequence {expected}")
            digest = record_hash(previous, sequence, payload)
            if digest != stored:
                raise LogCorruptionError(f"{where}: hash chain broken")
            try:
                event = self._codec.decode(payload)
                stored_tag = _TAG_DECODER.decode(payload).type
            except (EventDecodeError, msgspec.ValidationError) as error:
                raise LogCorruptionError(f"{where}: payload does not decode") from error
            decoded = (
                stored_tag,
                event.source,
                event.id.bytes,
                event.ts_event_ns,
                event.ts_ingest_ns,
            )
            if decoded != (tag, source, raw_id, ts_event, ts_ingest):
                raise LogCorruptionError(f"{where}: columns disagree with the payload")
            yield LogRecord(sequence, event), digest
            previous = digest
            expected += 1

    def verify(self) -> Head:
        """Check SQLite integrity and every record from 0; return the recomputed head.

        Raises:
            LogCorruptionError: any check fails.
        """
        problems = integrity_check(self._connection)
        if problems != ["ok"]:
            raise LogCorruptionError(f"{self._path}: integrity_check {problems[:3]}")
        head = Head(0, GENESIS)
        for record, digest in self._verified(0):
            head = Head(record.sequence + 1, digest)
        return head

    def close(self) -> None:
        """Close the database."""
        self._connection.close()

    def __enter__(self) -> Self:
        """Use as a context manager."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close on exit."""
        self.close()


def replay(
    reader: EventLogReader,
    clock: LogClock,
    handler: Callable[[LogRecord[Event]], None],
    *,
    from_sequence: int = 0,
) -> int:
    """Feed verified records to ``handler`` in order, advancing ``clock`` first.

    The clock takes each record's ``ts_ingest_ns`` (BP1 ``LogClock`` rule, ADR 0009).

    Returns:
        The number of records replayed.
    """
    count = 0
    for record in reader.records(from_sequence):
        clock.advance(record.event.ts_ingest_ns)
        handler(record)
        count += 1
    return count
