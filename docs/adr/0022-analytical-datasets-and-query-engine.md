<!--
SPDX-FileCopyrightText: 2026 Asadollah Boshra
SPDX-License-Identifier: MIT
-->

# 0022. Analytical datasets and query engine

Date: 2026-10-18

## Status

Accepted

## Context

Research and reports need columnar access to market history without making the analytical layer authoritative. DuckDB reads Parquet efficiently, but silently truncated decimals when files of one dataset differed in decimal scale (measured with 1.5.6), works best with row groups of 100K-1M rows and files of 100 MB-10 GB, and can read any file or download extensions unless restricted. Globbing directories gives readers no consistent view while files are being added.

## Decision

Datasets are exported from the event log by one application-owned writer through a bounded buffer that is flushed when full, never dropping rows. Each event type and version has its own dataset of Parquet files, partitioned by venue, symbol and UTC year, with fixed column types: decimals `decimal128(38, 18)`, times and integers `int64`, identifiers 16-byte binary; a value that does not fit exactly is refused. A manifest per dataset lists exactly the published files with their SHA-256 and the log position and head hash they cover; every change writes new files and replaces the manifest atomically, and readers use only the files of the manifest they loaded. Partitions with too many files are compacted into new files; files referenced by neither the current nor the previous manifest are deleted. Queries run in an in-memory DuckDB session that may read only the datasets root, loads no extensions automatically, has fixed memory and thread limits and a locked configuration, and checks every file's hash and schema before exposing a dataset.

## Consequences

Parquet values carry scale 18, so they are analytical values; replay always uses the event log. A file with a wrong schema or changed bytes stops the query before any row is read. Values beyond 20 integer digits or 18 decimal places need a new decision.
