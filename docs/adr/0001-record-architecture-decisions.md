<!--
SPDX-FileCopyrightText: 2026 Asadollah Boshra
SPDX-License-Identifier: MIT
-->

# 0001. Record architecture decisions

Date: 2026-10-16

## Status

Accepted

## Context

Architectural choices made in this project must stay traceable after the context that produced them is gone. Decisions hidden in code, commit messages or private notes cannot be reviewed or superseded deliberately.

## Decision

Significant architectural decisions are recorded as Architecture Decision Records in `docs/adr/`, in Michael Nygard's format: title, status, context, decision, consequences. Records are numbered sequentially and numbers are never reused. A decision that has been implemented is changed only by a new record that supersedes it; the old record stays, marked as superseded.

## Consequences

Each record is self-contained: it states its own context and does not depend on documents outside the repository. Reviewing a change to an architectural rule starts from its record.
