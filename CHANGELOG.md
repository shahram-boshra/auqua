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

- Research and backtest (BP5): experiment specifications with train,
  validation and test periods; point-in-time input snapshots with permitted,
  cleared inputs; an event-driven engine; walk-forward and purged k-fold
  validation; a trial ledger across runs; Deflated Sharpe Ratio, probabilistic
  Sharpe ratio, PBO by CSCV and Lo-adjusted annualised Sharpe ratios (standard
  library only); robustness sweeps with robust ranking; a cost model; the
  Research Integrity Report as JSON (validated against a published schema)
  and static HTML; reruns pinned to an earlier input; the
  `python -m auqua.analytics` command line, which prints only the run id, the
  input record count, the report hash and check statuses (ADRs 0030-0032).
  Event types `research.run.v2` (upcast from v1) and `research.trial.v1`.
- Portfolio and capital (BP6, in progress): `numeric.divide` rounds an exact
  quotient once, through the same rounding path as `numeric.quantize`. Event
  types `account.transfer.v1`, `account.statement.v1`,
  `portfolio.reconciliation.v1`, `market.settlement_price.v1`,
  `lifecycle.corporate_action.v2` (ex, record and pay dates of a cash dividend)
  and `lifecycle.funding_payment.v2` (funding period, venue-reported amount);
  a stored version 1 that cannot be upcast (a cash dividend, any funding
  payment) is refused with its log sequence, never guessed. The account view
  read by risk (`auqua.contracts.portfolio`: postings, balances with
  receivables, positions with filled and working quantities kept apart,
  margin, base-currency conversions, explicit "unavailable" markers) and the
  `portfolio.snapshot.v1` event; `numeric.exact` runs arithmetic that must not
  round. Configuration schema version 5 adds the `portfolio` section: account
  id, base currency USD, and the operator's currencies, USD rate sources, margin
  requirements and funding-rate periods, each with its source and the date it
  was read; a version-4 file migrates with an empty section. The portfolio
  ledger (postings and exact balances per currency, receivables apart from
  cash) and first-in, first-out lots (economic cost without fees, splits that
  keep a lot's total cost, and one rounding rule after a non-terminating split
  whose parts sum exactly to the lot's cost). Day counts from their primary
  texts (MSRB G-33 30/360; US Treasury accrued interest per 31 CFR 356,
  Appendix B, rounded once, an exact tie refused; other conventions refused)
  and conversion into USD at the decision time from the configured rate
  source, following the rate's published unit, recorded with rate, source and
  availability, and reported unavailable when no rate is usable. BP6
  amendment 2: optional fields added within a version without changing stored
  bytes (`omit_defaults`): `exercise_settlement_value` on option exercise and
  assignment, `accrued_interest` on fills; day count `act_act_treasury`
  (semiannual coupons only); posting kind `accrued`; event
  `portfolio.allocation_unavailable.v1` with closed reasons. Per-class
  effects: futures daily variation from the day's trades and the start-of-day
  position, each contract's value rounded to the settlement currency (ties
  away from zero, CME), physical delivery refused; spot pairs post base and
  quote on the trade date and the fee in its own currency (long or flat, FX
  forwards refused); inverse-perpetual profit in the coin, rounded once;
  funding posted as reported, or computed only for a position sized in the
  settlement currency. Margin requirements per contract or as a fraction of
  notional from the configured values, exact; a position without a usable
  requirement is reported as unmargined, never as zero. BP6 amendment 3: an FX
  settlement may name the fill it settles (`causation_id`, added within
  version 1 without changing stored bytes) and is matched by that reference,
  else by its amounts before the value date; a difference, no candidate, or
  candidates on both sides is a break, never a guess. Funding without a
  reported amount is refused in phase 1.

### Changed

- The backtest runner reads bars through the point-in-time snapshot and the
  log clock follows availability time; the simulated venue prices fills with
  a cost model and supports latency, and decisions count orders still in
  flight; source profiles name the event source of
  their data, and first-party synthetic data has its own profile.
- Every missing parent directory of an event log, dataset or report is
  created `0700` (not only the last one), and dataset files, manifests and
  reports are written `0600` (`auqua.storage.files`).
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
- uv 0.13.1 → 0.13.5. CI installs the exact version from `UV_VERSION`;
  `required-version` is now a floor (`>=0.13.5`), so Dependabot can update the
  lock files again (ADR 0033, superseding the uv part of ADR 0010). Dependabot
  proposes version updates only for releases at least 7 days old
  (`cooldown`); security fixes are exempt. The supply-chain workflow runs
  Trivy even when grype has failed, so a run reports both scanners' findings.

### Fixed

- The scanner environment (`tools/`) now pins Python 3.14 with its own
  `.python-version`. uv does not apply the root file to the separate `tools`
  project, so a runner without Python 3.14 resolved its `requires-python`
  (`>=3.14`) to the newest download (3.15.1 in the CI `provenance` job), and
  local and CI scanner environments could differ (ADR 0010).

### Security

- `certifi` 2026.7.22 → 2026.10.19 in the development and scanner lock files
  (GHSA-6q48-6c7w-9gw2, critical: bundled emSign root CAs with compromised
  intermediate keys). `certifi` is not a runtime dependency.
- `virtualenv` 21.14.8 → 21.14.13 and `filelock` 4.2.0 → 4.2.4 in the
  development lock file (high: GHSA-c978-59m8-c77v, GHSA-394h-qwmc-48pf,
  GHSA-6w3r-xr62-4xcp, GHSA-cx6f-vjhg-g3cc, GHSA-v4x8-3wr5-3wq8; with medium
  and low advisories fixed in the same releases). Neither is a runtime
  dependency.
