<!--
SPDX-FileCopyrightText: 2026 Asadollah Boshra
SPDX-License-Identifier: MIT
-->

# 0023. Transactional store and point-in-time reference data

Date: 2026-10-18

## Status

Accepted

## Context

Portfolio, risk and order state need a transactional store with exact numbers. Research must see reference data as it was known at the time, not as corrected later, and corrections must not overwrite history.

## Decision

The transactional store is a separate SQLite database per environment with the same durability settings as the event log and STRICT tables only. Decimal columns are 64-bit integers scaled by a fixed number of decimal places declared per column; a value finer than the scale or outside 64 bits is refused, never rounded. Owners declare their tables through numbered migrations; the schema version is SQLite's `user_version`, each step runs in its own transaction, and a database newer than the code is refused. Instrument specifications enter the event log as `reference.instrument.v1` events carrying an effective time and a knowledge time, which may not be later than the event's ingestion time. A derived index answers "which version applied at a given time, as known at a given time": among versions known by then, the latest effective one, a later correction winning over an earlier version with the same effective time.

## Consequences

Rounding is always an explicit step of the owning component before storage. Reference data can be rebuilt from the log, and backtests can reproduce the specifications that were known at each point in time.
