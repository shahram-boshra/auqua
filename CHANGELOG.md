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
- Configuration (BP2): TOML files with exact decimals, immutable strict
  Pydantic models, `AUQUA_` overrides for marked fields only, secrets from
  permission-checked files, a separate hashed risk-limits file, configuration
  identity, schema migrations and five isolated environments (ADRs
  0018-0020). Runtime dependency `pydantic`; mypy runs the Pydantic plugin.
- Storage and event log (BP3): a durable, hash-chained SQLite event log with
  one writer, duplicate and conflict handling and verified replay; Parquet
  datasets with fixed column types, manifests and compaction; a sandboxed
  read-only DuckDB query session; scaled-integer decimals and migrations for
  the transactional store; a point-in-time index of instrument versions and the
  `reference.instrument.v1` event (ADRs 0021-0023). Runtime dependencies
  `pyarrow` and `duckdb`; configuration schema version 2 adds the `storage`
  section.
- Walking skeleton (ADR 0025): kline CSV ingestion, a moving-average crossover
  baseline signal, portfolio target and book, a pre-trade risk gate, a
  simulated venue and a static HTML report, joined by a backtest runner that
  logs every decision in its own event log. Six canonical event types
  (`research.run.v1`, `alpha.signal.v1`, `portfolio.target.v1`,
  `order.submitted.v1`, `risk.decision.v1`, `execution.fill.v1`) and
  name-based UUIDv8 identifiers for reproducible records. Tests use synthetic
  first-party data only.
- The runtime licence gate accepts a dependency without a declared licence
  expression only through a recorded concluded licence bound to its version and
  licence-file hashes (ADR 0024).
- Ingestion (BP4): an immutable raw store addressed by SHA-256; HTTPS
  retrieval with rate limits, jittered backoff and `Retry-After`; source
  profiles of the data providers' terms with operator acknowledgement and
  permitted-use checks; exact parsers for Coinbase Exchange candles and
  products, Binance Vision klines, Federal Reserve H.10 rates and US Treasury
  par yields; gap events; idempotent runs with revisions and a lineage record;
  publication-time availability and a point-in-time market view; the
  `python -m auqua.ingestion` command line (ADRs 0026-0028). Event types
  `market.reference_rate.v1`, `ingest.run.v1` and `data.gap.v1`; configuration
  schema version 3 adds the `ingestion` section. No third-party data is
  included; tests use synthetic files in the providers' layouts.

### Changed

- Data sources are cleared per jurisdiction: the configuration declares the
  operator's country of residence and access (`ingestion.jurisdiction`,
  schema version 4), each source profile records the jurisdictions assessed,
  and ingestion refuses a source not cleared for it before fetching; lineage
  records name the jurisdiction (ADR 0029). For the United States, Binance
  Vision is not cleared.
- Bars carry an optional `revision` and publication time, and their ids now
  include the interval, so bars of different intervals never collide (BP4).
  The skeleton's kline reader moved to `auqua.ingestion.parsers`.
- The CI provenance check runs ScanCode and the SCANOSS settings check without
  SCANOSS matching, because the OSSKB rate-limits GitHub-hosted runners.
- The provenance gate scans file contents from Git objects (index or commit),
  not the working tree, and SCANOSS ignores settings files.

### Security

- `certifi` 2026.7.22 → 2026.10.19 in the development and scanner lock files
  (GHSA-6q48-6c7w-9gw2, critical: bundled emSign root CAs with compromised
  intermediate keys). `certifi` is not a runtime dependency.
