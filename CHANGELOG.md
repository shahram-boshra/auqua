<!--
SPDX-FileCopyrightText: 2026 Asadollah Boshra
SPDX-License-Identifier: MIT
-->

# Changelog

All notable changes to this project are documented in this file.
The format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and the project uses [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Repository scaffold: `auqua` package skeleton with one subpackage per
  component, smoke tests, provenance and licence-policy checkers, pre-commit
  hooks, CI, supply-chain and release workflows, and ADRs 0001-0012.
- Provenance gate at push: a pre-push hook matches the pushed commits with
  SCANOSS and ScanCode (ADR 0013, superseding ADR 0007).
- Core domain contracts (BP1): exact decimal helpers with one explicit rounding
  path, UUIDv7 identifiers, nanosecond time fields, the log-driven and wall
  clocks, currencies, an instrument model for the six asset classes,
  market-data and lifecycle events with a versioned JSON codec and JSON Schema
  export (ADRs 0014-0017). Runtime dependency `msgspec`; development
  dependency `hypothesis`. Wall-clock calls outside ingestion fail the lint
  gate (ruff TID251).

### Changed

- The CI provenance check runs ScanCode and the SCANOSS settings check without
  SCANOSS matching, because the OSSKB rate-limits GitHub-hosted runners.
- The provenance gate scans file contents from Git objects (index or commit),
  not the working tree, and SCANOSS ignores settings files.
