# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""SQLite connections with explicit durability, busy and checkpoint policy (ADR 0021).

Writers use WAL with ``synchronous=FULL`` (SQLite: durable in WAL mode only
with FULL or EXTRA). Connections run in SQLite's own autocommit mode, so every
transaction is opened explicitly with ``BEGIN IMMEDIATE`` and pragmas never run
inside an implicit transaction. Readers open the file read-only. New
directories are created ``0700`` and new database files ``0600``; SQLite gives
its ``-wal`` and ``-shm`` files the mode of the database file.
"""

import os
import sqlite3
from enum import StrEnum
from pathlib import Path

from auqua.storage.errors import StorageError
from auqua.storage.files import PRIVATE_FILE, mkdir_private

_SYNCHRONOUS_FULL = 2


class Checkpoint(StrEnum):
    """WAL checkpoint modes used by the storage layer."""

    PASSIVE = "PASSIVE"
    TRUNCATE = "TRUNCATE"


def ensure_private_file(path: Path) -> None:
    """Create ``path`` (and missing parent directories, ``0700``) as an empty ``0600`` file.

    An existing file is left unchanged.
    """
    mkdir_private(path.parent)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, PRIVATE_FILE)
    except FileExistsError:
        return
    os.close(descriptor)


def _set_busy_timeout(connection: sqlite3.Connection, busy_timeout_ms: int) -> None:
    if busy_timeout_ms <= 0:
        raise ValueError(f"busy_timeout_ms must be > 0, got {busy_timeout_ms}")
    connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")


def connect_writer(path: Path, busy_timeout_ms: int) -> sqlite3.Connection:
    """Open ``path`` for writing: WAL, ``synchronous=FULL``, foreign keys on.

    Raises:
        StorageError: WAL or FULL could not be set (e.g. unsupported filesystem).
    """
    ensure_private_file(path)
    connection = sqlite3.connect(path, autocommit=True)
    try:
        _set_busy_timeout(connection, busy_timeout_ms)
        mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        connection.execute("PRAGMA synchronous = FULL")
        synchronous = connection.execute("PRAGMA synchronous").fetchone()[0]
        connection.execute("PRAGMA foreign_keys = ON")
        if mode != "wal" or synchronous != _SYNCHRONOUS_FULL:
            raise StorageError(f"{path}: journal_mode {mode!r}, synchronous {synchronous}")
    except BaseException:
        connection.close()
        raise
    return connection


def connect_reader(path: Path, busy_timeout_ms: int) -> sqlite3.Connection:
    """Open an existing database read-only.

    Raises:
        StorageError: the file does not exist.
    """
    if not path.is_file():
        raise StorageError(f"{path}: no such database")
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, autocommit=True)
    try:
        _set_busy_timeout(connection, busy_timeout_ms)
    except BaseException:
        connection.close()
        raise
    return connection


def checkpoint(connection: sqlite3.Connection, mode: Checkpoint) -> tuple[int, int, int]:
    """Run a WAL checkpoint; returns SQLite's (busy, log frames, checkpointed frames)."""
    busy, frames, done = connection.execute(f"PRAGMA wal_checkpoint({mode.value})").fetchone()
    return int(busy), int(frames), int(done)


def integrity_check(connection: sqlite3.Connection) -> list[str]:
    """Return SQLite's ``integrity_check`` result (``["ok"]`` when sound)."""
    return [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
