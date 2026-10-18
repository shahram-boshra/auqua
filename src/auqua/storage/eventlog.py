# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""The event log: the authoritative record of canonical events (ADR 0021).

One SQLite database per environment, written by exactly one :class:`EventLog`
(an exclusive ``flock`` on ``<file>.lock`` is held for the writer's lifetime).
Each append assigns contiguous sequence numbers inside one ``BEGIN IMMEDIATE``
transaction, stores the codec's bytes unchanged, extends the hash chain and
returns only after a durable commit. A repeated (``source``, ``id``) with
identical bytes is counted as a duplicate; with different bytes it is a
conflict and the whole batch is rolled back.
"""

import fcntl
import os
import sqlite3
import typing
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Self

from auqua.config.base import Environment
from auqua.contracts.codec import encode
from auqua.contracts.events import CanonicalEvent
from auqua.contracts.events.base import Event
from auqua.storage.chain import GENESIS, record_hash
from auqua.storage.errors import (
    DuplicateConflictError,
    EncodingError,
    StorageError,
    UnsupportedStoreVersionError,
    WriterLockedError,
)
from auqua.storage.sqlite import (
    Checkpoint,
    checkpoint,
    connect_writer,
    ensure_private_file,
)

#: Version of the event-log database layout.
FORMAT_VERSION = 1
#: Event types the log accepts: those its reader can decode.
CANONICAL_TYPES: frozenset[type[Event]] = frozenset(typing.get_args(CanonicalEvent))

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS log (
        sequence     INTEGER PRIMARY KEY,
        source       TEXT    NOT NULL,
        id           BLOB    NOT NULL CHECK (length(id) = 16),
        type         TEXT    NOT NULL,
        ts_event_ns  INTEGER NOT NULL,
        ts_ingest_ns INTEGER NOT NULL,
        payload      BLOB    NOT NULL,
        record_hash  BLOB    NOT NULL CHECK (length(record_hash) = 32),
        UNIQUE (source, id)
    ) STRICT""",
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT",
)


@dataclass(frozen=True)
class Head:
    """End of the log: the next sequence number and the hash of the last record.

    For an empty log ``next_sequence`` is 0 and ``hash`` is the genesis value.
    """

    next_sequence: int
    hash: bytes


@dataclass(frozen=True)
class AppendResult:
    """Outcome of one append.

    Attributes:
        appended: Records stored.
        duplicates: Events dropped because an identical copy is already stored.
        head: The log head after the commit.
    """

    appended: int
    duplicates: int
    head: Head


def lock_path(path: Path) -> Path:
    """The writer lock file of the event log at ``path``."""
    return path.with_name(path.name + ".lock")


def check_meta(connection: sqlite3.Connection, path: Path, environment: Environment) -> None:
    """Refuse a log of another format version or another environment.

    Raises:
        UnsupportedStoreVersionError: unknown format version.
        StorageError: the log belongs to another environment or has no metadata.
    """
    meta = dict(connection.execute("SELECT key, value FROM meta").fetchall())
    if meta.get("format") != str(FORMAT_VERSION):
        raise UnsupportedStoreVersionError(f"{path}: event-log format {meta.get('format')!r}")
    if meta.get("environment") != environment.value:
        raise StorageError(
            f"{path}: environment {meta.get('environment')!r}, requested {environment.value!r}"
        )


def read_head(connection: sqlite3.Connection) -> Head:
    """Read the head from the last stored record."""
    row = connection.execute(
        "SELECT sequence, record_hash FROM log ORDER BY sequence DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return Head(0, GENESIS)
    return Head(int(row[0]) + 1, bytes(row[1]))


def _tag(event: Event) -> str:
    tag = type(event).__struct_config__.tag
    if not isinstance(tag, str):
        raise EncodingError(f"{type(event).__name__} has no string tag")
    return tag


class EventLog:
    """The single writer of one environment's event log."""

    def __init__(
        self,
        path: Path,
        environment: Environment,
        *,
        busy_timeout_ms: int,
        event_types: frozenset[type[Event]] = CANONICAL_TYPES,
    ) -> None:
        """Open (creating if needed) the log at ``path`` for ``environment``.

        Raises:
            WriterLockedError: another writer holds the log.
            StorageError: the log belongs to another environment or format.
        """
        self._path = path
        self._event_types = event_types
        lock = lock_path(path)
        ensure_private_file(lock)
        self._lock_fd = os.open(lock, os.O_RDWR)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self._lock_fd)
            raise WriterLockedError(f"{path}: another writer holds {lock}") from None
        try:
            self._connection = connect_writer(path, busy_timeout_ms)
        except BaseException:
            os.close(self._lock_fd)
            raise
        try:
            self._initialize(environment)
            self._head = read_head(self._connection)
        except BaseException:
            self._connection.close()
            os.close(self._lock_fd)
            raise

    def _initialize(self, environment: Environment) -> None:
        connection = self._connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            for statement in _SCHEMA:
                connection.execute(statement)
            connection.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES ('format', ?), ('environment', ?)",
                (str(FORMAT_VERSION), environment.value),
            )
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        check_meta(connection, self._path, environment)

    @property
    def head(self) -> Head:
        """The head after the last committed append."""
        return self._head

    def append(self, events: Iterable[Event]) -> AppendResult:
        """Append ``events`` in one durable transaction.

        Raises:
            DuplicateConflictError: an event repeats a stored (``source``, ``id``)
                with different content; nothing from the batch is stored.
            EncodingError: an event type the log cannot read back, or a time
                outside the 64-bit range.
        """
        connection = self._connection
        head = self._head
        appended = duplicates = 0
        connection.execute("BEGIN IMMEDIATE")
        try:
            for event in events:
                if type(event) not in self._event_types:
                    raise EncodingError(f"{type(event).__name__} is not a canonical event type")
                payload = encode(event)
                key = (event.source, event.id.bytes)
                stored = connection.execute(
                    "SELECT sequence, payload FROM log WHERE source = ? AND id = ?", key
                ).fetchone()
                if stored is not None:
                    if bytes(stored[1]) != payload:
                        raise DuplicateConflictError(
                            f"source {event.source!r} id {event.id} conflicts with "
                            f"sequence {stored[0]}"
                        )
                    duplicates += 1
                    continue
                digest = record_hash(head.hash, head.next_sequence, payload)
                try:
                    connection.execute(
                        "INSERT INTO log VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            head.next_sequence,
                            *key,
                            _tag(event),
                            event.ts_event_ns,
                            event.ts_ingest_ns,
                            payload,
                            digest,
                        ),
                    )
                except OverflowError as error:
                    raise EncodingError(
                        f"source {event.source!r} id {event.id}: time outside 64 bits"
                    ) from error
                head = Head(head.next_sequence + 1, digest)
                appended += 1
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        self._head = head
        checkpoint(connection, Checkpoint.PASSIVE)
        return AppendResult(appended, duplicates, head)

    def close(self) -> None:
        """Truncate the WAL, close the database and release the writer lock."""
        try:
            checkpoint(self._connection, Checkpoint.TRUNCATE)
            self._connection.close()
        finally:
            os.close(self._lock_fd)

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
