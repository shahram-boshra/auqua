<!--
SPDX-FileCopyrightText: 2026 Asadollah Boshra
SPDX-License-Identifier: MIT
-->

# 0015. Identifiers and time representation

Date: 2026-10-18

## Status

Accepted

## Context

Events, orders and fills need stable identifiers, and replay requires that no decision depends on when the code happens to run (ADR 0009). Python's `datetime` resolves only to one microsecond, while exchange timestamps can carry nanoseconds. Python 3.14 provides UUIDv7 (RFC 9562) in the standard library.

## Decision

Identifiers are UUIDv7 values from `uuid.uuid7()`. They are never used for ordering; order is the event log's sequence. Every time field is an integer of nanoseconds since the Unix epoch, UTC, and its name states the kind of time (event, ingestion, publication, effective, knowledge). Time is read through a `Clock` protocol: a log clock, advanced only by applied log records and never moved backwards, serves decision, order, risk and accounting code in live operation and replay; a wall clock is used only at the ingestion boundary. Ruff rule TID251 bans the wall-clock functions of `time` and `datetime` everywhere except the wall-clock module and `auqua.ingestion`. Instruments are identified internally by venue and venue symbol; a symbol change creates a new identifier.

## Consequences

Conversions to calendar text happen only at presentation edges. Storage formats limited to 64-bit integers cover nanosecond times until the year 2262; this limit is handled by the storage design.
