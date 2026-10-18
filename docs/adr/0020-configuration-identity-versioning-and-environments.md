<!--
SPDX-FileCopyrightText: 2026 Asadollah Boshra
SPDX-License-Identifier: MIT
-->

# 0020. Configuration identity, versioning and environments

Date: 2026-10-18

## Status

Accepted

## Context

Research and operational decisions must be reproducible from the configuration that produced them, configuration formats evolve, and environments must not share configuration, credentials or limits.

## Decision

The identity of a configuration is the SHA-256 of its canonical JSON form after validation, with keys sorted and secrets excluded. Every configuration file carries a schema version; older versions are migrated step by step through registered, tested migrations, and a version without a migration path, including any version newer than the code, is refused. The environments are `dev`, `test`, `research`, `paper` and `live`; each has its own configuration file, risk-limits file and secrets directory, nothing is inherited between them, and a file whose environment differs from the one requested is refused. No deployable configuration is kept in the repository; tests create their configuration in temporary directories.

## Consequences

Equal configurations have equal identities on any machine. Adding or renaming a configuration field requires a new schema version and a migration.
