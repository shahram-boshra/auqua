<!--
SPDX-FileCopyrightText: 2026 Asadollah Boshra
SPDX-License-Identifier: MIT
-->

# 0028. Availability time

Date: 2026-10-21

## Status

Accepted

## Context

Point-in-time research must use the time data became available, not the time the platform downloaded it. Backfilled history is downloaded long after it was published, and publishers release on schedules (the H.10 daily rates are released weekly).

## Decision

Bars and reference rates carry an optional or required publication time; a record's availability time is its publication time when stated, otherwise its event time (a bar is available at its end). Reference rates take their publication time from the publisher's documented schedule (H.10: Monday 16:15 New York time for the previous week, the next business day after a federal holiday) or, where no time is documented, from a declared conservative rule (US Treasury par yields: 00:00 New York time of the next business day); rows without a known time are counted and not stored. Each run records the basis of its availability times. A point-in-time view resolves revisions by knowledge time and releases each record only from its availability time.

## Consequences

Strategies need no look-ahead safeguards of their own. Corrections made before the platform first ingested a series cannot be reconstructed; reports state the availability basis of their inputs.
