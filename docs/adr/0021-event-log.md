<!--
SPDX-FileCopyrightText: 2026 Asadollah Boshra
SPDX-License-Identifier: MIT
-->

# 0021. Event log

Date: 2026-10-18

## Status

Accepted

## Context

The event log is the authoritative record of canonical events; every other store is derived from it, and replaying it must reproduce state exactly. It must assign sequence numbers, handle repeated deliveries explicitly, survive crashes and power loss, and make tampering or loss detectable.

## Decision

Each environment has one SQLite database in WAL mode with `synchronous=FULL` (SQLite: durable in WAL mode only with FULL or EXTRA) and STRICT tables, on a local filesystem. Exactly one writer opens it, holding an exclusive file lock for its lifetime; readers open it read-only. An append runs in one `BEGIN IMMEDIATE` transaction, assigns contiguous sequence numbers starting at 0, stores the codec's bytes unchanged and returns after a durable commit. A repeated (`source`, `id`) with identical bytes is counted as a duplicate and not stored; with different bytes it is a conflict and the whole batch is rolled back. Each record carries `SHA-256(previous hash || sequence || payload)`; the hash of the last record identifies the log up to that record and is the data-snapshot identity used in lineage. Readers verify contiguity, the hash chain, decoding and the indexed columns before yielding a record, and replay drives the log clock from each record's ingestion time.

## Consequences

A crash loses at most the uncommitted batch, which the producer sends again as duplicates. Any edit, removal, insertion or reordering of a record stops replay. Size and retention of the log are an operations concern (BP11); segmenting the log would need a new decision.
