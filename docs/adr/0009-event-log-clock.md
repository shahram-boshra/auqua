<!--
SPDX-FileCopyrightText: 2026 Asadollah Boshra
SPDX-License-Identifier: MIT
-->

# 0009. Time from the sequenced event log

Date: 2026-10-16

## Status

Accepted

## Context

Replay must reproduce identical state and outputs. Components that read the system clock directly produce different results on every run.

## Decision

Components that affect decisions, orders, risk or accounting read time from an injected clock driven by the sequenced event log, never from the system clock. Wall-clock time is captured only where data enters the system and is stamped onto events. Timeouts that change state are recorded as events. Monotonic elapsed time is used for measurement only.

## Consequences

Replaying the same log, with recorded model outputs (ADR 0006), reproduces identical state and outputs.
