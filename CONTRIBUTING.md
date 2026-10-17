<!--
SPDX-FileCopyrightText: 2026 Asadollah Boshra
SPDX-License-Identifier: MIT
-->

# Contributing

## Contribution process

auqua has a single maintainer. **Pull requests from other people are not
accepted at this time**, because every outside contribution requires a
contributor agreement, which is not yet in place.

- Bug reports and questions: open a GitHub issue.
- Security vulnerabilities: follow [SECURITY.md](SECURITY.md); never a public issue.

## Requirements for changes (maintainer)

Every change reaches `main` only through a pull request whose checks pass:

- Formatting and lint: `ruff format` and `ruff check`.
- Types: `mypy` in strict mode.
- Architecture boundaries: `lint-imports`.
- Tests: `pytest`; new behaviour comes with tests.
- Licensing metadata: every file carries `SPDX-FileCopyrightText` and
  `SPDX-License-Identifier` (REUSE).
- Provenance: the SCANOSS and ScanCode gate passes; flagged code is rewritten,
  never declared away ([ADR 0007](docs/adr/0007-provenance-controls.md)).
