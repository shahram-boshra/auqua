# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Read-only DuckDB query session over the analytical datasets (ADR 0022).

The session is an in-memory DuckDB database that may read only the datasets
root: extensions are neither installed nor loaded automatically, external
access is disabled after ``allowed_directories`` is set, and the configuration
is locked. Each dataset is exposed as a view over exactly the files listed in
its manifest, after every file's SHA-256 and Arrow schema were checked against
the manifest and the dataset schema; a mismatch raises before DuckDB reads a
row (DuckDB silently truncates across mismatched decimal scales).
"""

import hashlib
import re
from collections.abc import Sequence
from pathlib import Path
from types import TracebackType
from typing import Self

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from auqua.config.storage import StorageConfig
from auqua.contracts.events.base import Event
from auqua.storage.datasets import dataset_schema, event_tag, load_manifest
from auqua.storage.errors import SchemaMismatchError, StorageError

_VIEW_NAME = re.compile(r"[a-z][a-z0-9_]*")
_HIVE_TYPES = "{'venue': 'VARCHAR', 'symbol': 'VARCHAR', 'year': 'INTEGER'}"


def _sql_string(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


class QuerySession:
    """Sandboxed, read-only SQL over the datasets under one root."""

    def __init__(self, root: Path, config: StorageConfig) -> None:
        """Open an in-memory DuckDB limited to ``root``, memory and threads from ``config``."""
        self._root = root.resolve()
        self._connection = duckdb.connect(
            ":memory:",
            config={
                "autoinstall_known_extensions": False,
                "autoload_known_extensions": False,
                "memory_limit": f"{config.duckdb_memory_mib}MiB",
                "threads": config.duckdb_threads,
            },
        )
        try:
            directory = _sql_string(f"{self._root.as_posix()}/")
            self._connection.execute(f"SET allowed_directories = [{directory}]")
            self._connection.execute("SET enable_external_access = false")
            self._connection.execute("SET lock_configuration = true")
        except BaseException:
            self._connection.close()
            raise

    def register(self, event_type: type[Event], view: str) -> int:
        """Expose ``event_type``'s dataset as view ``view``; return its row count.

        Raises:
            StorageError: the dataset has no manifest or no files.
            SchemaMismatchError: a listed file is missing, changed, or has another schema.
            ValueError: ``view`` is not a plain lower-case identifier.
        """
        if _VIEW_NAME.fullmatch(view) is None:
            raise ValueError(f"invalid view name {view!r}")
        directory = self._root / event_tag(event_type)
        manifest = load_manifest(directory, event_type)
        if manifest is None or not manifest.files:
            raise StorageError(f"{directory}: no published files")
        expected = dataset_schema(event_type)
        paths: list[str] = []
        for entry in manifest.files:
            path = directory / entry.path
            if not path.is_file() or _sha256(path) != entry.sha256:
                raise SchemaMismatchError(f"{path}: missing or changed since the manifest")
            if not pq.read_schema(path).remove_metadata().equals(expected):
                raise SchemaMismatchError(f"{path}: schema differs from the dataset schema")
            paths.append(_sql_string(path.as_posix()))
        self._connection.execute(
            f"CREATE OR REPLACE VIEW {view} AS SELECT * FROM read_parquet("  # noqa: S608
            f"[{', '.join(paths)}], hive_partitioning = true, hive_types = {_HIVE_TYPES})"
        )
        return sum(entry.rows for entry in manifest.files)

    def sql(self, query: str, parameters: Sequence[object] = ()) -> pa.Table:
        """Run ``query`` with positional ``parameters``; return an Arrow table."""
        result = self._connection.execute(query, list(parameters))
        table: pa.Table = result.to_arrow_table()
        return table

    def close(self) -> None:
        """Close the session."""
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
