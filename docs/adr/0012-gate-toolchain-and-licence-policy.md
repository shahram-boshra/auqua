<!--
SPDX-FileCopyrightText: 2026 Asadollah Boshra
SPDX-License-Identifier: MIT
-->

# 0012. Gate toolchain and licence policy

Date: 2026-10-16

## Status

Accepted

## Context

The same checks must run locally before every commit and in CI, from locked environments, and dependency licences must be classified explicitly rather than assumed.

## Decision

The gate runs ruff (lint and format), mypy (strict), import-linter, pytest and `uv build`; pre-commit runs file hygiene, ruff, detect-secrets, REUSE lint and the provenance gate (ADR 0007). Scanner tools run from a separate locked environment (`tools/`) so they never constrain the platform's dependencies. GitHub Actions and hook repositories are pinned to full commit SHAs. CI also produces an SPDX SBOM and runs Grype, Trivy and a licence-policy check. Runtime dependencies are classified allow (MIT, BSD-2-Clause, BSD-3-Clause, Apache-2.0, ISC, PSF-2.0, Zlib), review (MPL-2.0, LGPL, EPL-2.0, any licence exception) or deny (GPL, AGPL, SSPL, Commons Clause); unknown or missing licences are quarantined until a recorded decision. Development tools are inventoried and reported; they are run unmodified and never distributed.

## Consequences

Adding a runtime dependency with a review or unknown licence needs a recorded approval in `compliance/license-policy.toml`.
