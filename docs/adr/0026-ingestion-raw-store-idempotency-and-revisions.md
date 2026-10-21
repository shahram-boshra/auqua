<!--
SPDX-FileCopyrightText: 2026 Asadollah Boshra
SPDX-License-Identifier: MIT
-->

# 0026. Ingestion: raw store, idempotency and revisions

Date: 2026-10-21

## Status

Accepted

## Context

Ingested market data must be traceable to the exact bytes it came from, re-running an ingestion must not duplicate or overwrite anything, and providers revise data after publishing it (the Federal Reserve Board corrected an H.10 release in August 2026; Coinbase states that historical rate data may be incomplete; Binance lists replaced archive files). The event log is append-only and refuses a known id with different content.

## Decision

Every raw file is stored once, immutable, under its SHA-256, before it is parsed; it is re-hashed when read. An ingestion run is keyed by the source profile, the raw file's SHA-256 and the SHA-256 of the parser module; a run whose key completed before is recorded as a no-op. Parsers map each row exactly or refuse the file. Record ids are name-based on the source, the instrument or series, the bar interval and the start or observation time, never on the ingestion time. A record already stored with the same content is a duplicate; with different content it is stored as the next revision under a name-based revision id, and readers choose the revision by knowledge time. Missing intervals are recorded as gap events and never filled. Each run ends with a lineage record naming the raw file, its retrieval, the provider checksum result, the parser, the source terms, the operator's declared use and the counts. Fetching uses HTTPS only, with rate limits, capped exponential backoff with jitter, Retry-After and a size cap.

## Consequences

Re-runs and crash recovery are safe: complete batches are found as duplicates and the run completes. Corrections never rewrite history used by an earlier experiment. Bar ids now include the interval, so bars of different intervals never collide.
