<!--
SPDX-FileCopyrightText: 2026 Asadollah Boshra
SPDX-License-Identifier: MIT
-->

# 0014. Canonical events with msgspec

Date: 2026-10-18

## Status

Accepted

## Context

Canonical events cross every component boundary and are replayed from the event log, so they must be immutable, strictly validated, versioned and fast to encode and decode. Measured on the same event schema with Python 3.14: msgspec 0.22.0 and Pydantic 2.14.1 gave equal correctness (unknown fields and types refused, immutable, exact round trip); msgspec decoded 2.6 times and encoded 6.3 times faster and retained half the memory per event.

## Decision

Canonical events are frozen `msgspec.Struct` types with unknown fields forbidden, forming tagged unions on the field `type`. The tag names the schema version (`<domain>.<name>.v<N>`). Every event carries `id`, `source`, `ts_event_ns` and `ts_ingest_ns`; the event log wraps each stored event in a record that carries its `sequence`, so an event is identical before and after it is stored. Adding an optional field with a default is compatible within a version; removing, renaming or re-typing a field creates the next version and a registered upcaster from the previous one. An unknown type or version is refused, never reinterpreted. Events are JSON on the wire with decimals as strings, and the JSON Schema of all event types is exported from the types. (`source`, `id`) is unique for each distinct event; a repeated pair is a duplicate. Pydantic remains the library for configuration models.

## Consequences

Two model libraries are used, each confined to its own package: events in `auqua.contracts`, configuration in `auqua.config`. Every new event type is placed in `auqua.contracts.events`, added to the canonical union and covered by round-trip tests, and a schema snapshot test makes every schema change deliberate.
