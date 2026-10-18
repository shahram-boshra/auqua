<!--
SPDX-FileCopyrightText: 2026 Asadollah Boshra
SPDX-License-Identifier: MIT
-->

# 0024. Concluded licence for dependencies without a declared expression

Date: 2026-10-18

## Status

Accepted. Refines [0012](0012-gate-toolchain-and-licence-policy.md).

## Context

The runtime licence gate classifies each installed distribution's `License-Expression` (PEP 639). Some required dependencies declare none: `duckdb` 1.5.6 carries only a licence classifier and its licence file. SPDX distinguishes the declared licence (the licence stated in the package; `NOASSERTION` when absent) from the concluded licence, reached by analysing the package's licence information together with scanning results.

## Decision

A distribution without `License-Expression` passes the runtime gate only through a recorded concluded-licence entry in `compliance/license-policy.toml`: package, exact version, `declared = "NOASSERTION"`, the concluded SPDX expression, the basis of the conclusion, and the SHA-256 of every installed licence file under `*.dist-info/licenses/`. The gate accepts the distribution only if the version matches, the installed licence files are exactly those recorded with the same hashes, and the concluded expression is not denied. The first entry is `duckdb` 1.5.6, concluded as `MIT AND Apache-2.0 AND BSD-2-Clause AND BSD-3-Clause AND BSL-1.0 AND ICU AND Zlib` from its wheel, a ScanCode scan of the wheel and Debian's reviewed copyright file.

## Consequences

Every new version of such a dependency needs a new analysis and entry; a changed licence file fails the gate. Distributions that declare an expression are classified as before.
