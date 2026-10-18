# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Transactional store: one SQLite database per environment for derived state (ADR 0023).

Owners (BP6 portfolio, BP7 risk, BP8 orders) declare their tables through
registered migrations; the schema version is SQLite's ``user_version``. Each
step runs in its own ``BEGIN IMMEDIATE`` transaction together with the version
change, so a database is always at exactly one version. Every table must be
STRICT. A database newer than the code is refused.
"""

import sqlite3
from collections.abc import Callable
from pathlib import Path

from auqua.storage.errors import SchemaMismatchError, UnsupportedStoreVersionError
from auqua.storage.sqlite import connect_writer

#: One migration step: receives the connection inside the open transaction.
StoreStep = Callable[[sqlite3.Connection], None]


class StoreMigrations:
    """Registry of schema steps from version N to N+1, starting at the empty database (0)."""

    def __init__(self, current: int) -> None:
        """Create a registry whose current version is ``current`` (>= 1)."""
        if current < 1:
            raise ValueError(f"current must be >= 1, got {current}")
        self.current = current
        self._steps: dict[int, StoreStep] = {}

    def register(self, from_version: int, step: StoreStep) -> None:
        """Register the step from ``from_version`` to ``from_version + 1``.

        Raises:
            ValueError: outside ``0..current-1`` or already registered.
        """
        if not 0 <= from_version < self.current or from_version in self._steps:
            raise ValueError(f"invalid or duplicate store migration from {from_version}")
        self._steps[from_version] = step

    def upgrade(self, connection: sqlite3.Connection, path: Path) -> None:
        """Apply the missing steps, one transaction each.

        Raises:
            UnsupportedStoreVersionError: the database is newer than the code, or
                a step is missing.
        """
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version > self.current:
            raise UnsupportedStoreVersionError(f"{path}: version {version} > {self.current}")
        while version < self.current:
            step = self._steps.get(version)
            if step is None:
                raise UnsupportedStoreVersionError(f"{path}: no step from version {version}")
            connection.execute("BEGIN IMMEDIATE")
            try:
                step(connection)
                connection.execute(f"PRAGMA user_version = {version + 1}")
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            version += 1


def _require_strict(connection: sqlite3.Connection, path: Path) -> None:
    loose = [
        str(name)
        for _, name, kind, _, _, strict in connection.execute("PRAGMA main.table_list")
        if kind == "table" and not str(name).startswith("sqlite_") and not strict
    ]
    if loose:
        raise SchemaMismatchError(f"{path}: tables must be STRICT: {sorted(loose)}")


def open_store(
    path: Path, migrations: StoreMigrations, *, busy_timeout_ms: int
) -> sqlite3.Connection:
    """Open the store at ``path``, bring it to the current version and check it.

    Raises:
        UnsupportedStoreVersionError: see :meth:`StoreMigrations.upgrade`.
        SchemaMismatchError: a table is not STRICT.
    """
    connection = connect_writer(path, busy_timeout_ms)
    try:
        migrations.upgrade(connection, path)
        _require_strict(connection, path)
    except BaseException:
        connection.close()
        raise
    return connection
