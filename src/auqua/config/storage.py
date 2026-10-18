# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Storage section of the root configuration (BP3 section 4.6; ADRs 0021-0022).

The section is owned by BP3 and placed in ``auqua.config`` so that the root
model never imports ``auqua.storage`` (import-linter contract 3). Every bound
is explicit: the Arrow buffer, Parquet row groups and files, the number of
files per partition before compaction, the SQLite busy timeout and DuckDB's
memory and threads.
"""

from typing import Annotated

from pydantic import Field

from auqua.config.base import ConfigModel

PositiveInt = Annotated[int, Field(gt=0)]


class StorageConfig(ConfigModel):
    """Bounds of the storage layer; every value is a positive integer.

    Attributes:
        buffer_rows: Capacity of the bounded Arrow buffer before a flush.
        rows_per_group: Rows per Parquet row group (DuckDB: 100K-1M rows work best).
        rows_per_file: Maximum rows per Parquet file.
        max_files_per_partition: Files in one partition above which it is compacted.
        busy_timeout_ms: SQLite busy timeout in milliseconds.
        duckdb_memory_mib: DuckDB memory limit in MiB.
        duckdb_threads: DuckDB worker threads.
    """

    buffer_rows: PositiveInt = 262_144
    rows_per_group: PositiveInt = 262_144
    rows_per_file: PositiveInt = 8_388_608
    max_files_per_partition: PositiveInt = 16
    busy_timeout_ms: PositiveInt = 5_000
    duckdb_memory_mib: PositiveInt = 1_024
    duckdb_threads: PositiveInt = 2
