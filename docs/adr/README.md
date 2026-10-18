<!--
SPDX-FileCopyrightText: 2026 Asadollah Boshra
SPDX-License-Identifier: MIT
-->

# Architecture decision records

Format: Michael Nygard (title, status, context, decision, consequences). Numbers are sequential and never reused.

| No. | Decision | Status |
|---|---|---|
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | Accepted |
| [0002](0002-permanent-non-goals.md) | Permanent non-goals, including no high-frequency trading | Accepted |
| [0003](0003-open-core-mit-and-alpha-plugin.md) | Open core under MIT with a private alpha plugin | Accepted |
| [0004](0004-python-core.md) | Python core orchestrating compiled libraries | Accepted |
| [0005](0005-six-asset-classes.md) | Six asset classes with explicit verification levels | Accepted |
| [0006](0006-gpu-cpu-compute-fallback.md) | GPU to CPU compute fallback | Accepted |
| [0007](0007-provenance-controls.md) | Code provenance controls from the first commit | Superseded by [0013](0013-provenance-matching-at-push.md) |
| [0008](0008-exact-numeric-values.md) | Exact numeric values for money, prices and quantities | Accepted |
| [0009](0009-event-log-clock.md) | Time from the sequenced event log | Accepted |
| [0010](0010-python-and-uv-versions.md) | Python 3.14 and uv 0.13.1 | Accepted |
| [0011](0011-single-package-src-layout.md) | One distribution and import package in the src layout | Accepted |
| [0012](0012-gate-toolchain-and-licence-policy.md) | Gate toolchain and licence policy | Accepted |
| [0013](0013-provenance-matching-at-push.md) | Provenance matching at commit and push; ScanCode in CI | Accepted |
| [0014](0014-canonical-events-with-msgspec.md) | Canonical events with msgspec | Accepted |
| [0015](0015-identifiers-and-time-representation.md) | Identifiers and time representation | Accepted |
| [0016](0016-in-memory-decimal-representation.md) | In-memory decimal representation | Accepted |
| [0017](0017-instrument-model-and-cdm-alignment.md) | Instrument model and CDM alignment | Accepted |
| [0018](0018-configuration-loading-and-precedence.md) | Configuration loading and precedence | Accepted |
| [0019](0019-secrets-and-risk-limits.md) | Secrets and risk limits | Accepted |
| [0020](0020-configuration-identity-versioning-and-environments.md) | Configuration identity, versioning and environments | Accepted |
| [0021](0021-event-log.md) | Event log | Accepted |
| [0022](0022-analytical-datasets-and-query-engine.md) | Analytical datasets and query engine | Accepted |
| [0023](0023-transactional-store-and-point-in-time-reference-data.md) | Transactional store and point-in-time reference data | Accepted |
| [0024](0024-concluded-licence-for-dependencies-without-a-declared-expression.md) | Concluded licence for dependencies without a declared expression | Accepted |
