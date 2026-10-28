<!--
SPDX-FileCopyrightText: 2026 Asadollah Boshra
SPDX-License-Identifier: MIT
-->

# auqua

Open-core, low-frequency multi-asset quantitative trading platform, written in Python.

**Status:** pre-alpha scaffold. No trading functionality exists yet; the package
structure marks the planned components only.

## Scope

- Low-frequency trading only. High-frequency and ultra-low-latency trading are
  permanent non-goals ([ADR 0002](docs/adr/0002-permanent-non-goals.md)).
- Six asset classes: equities and ETFs, futures, options, FX, crypto (spot and
  perpetual swaps), fixed income ([ADR 0005](docs/adr/0005-six-asset-classes.md)).
- Open core: this repository is MIT-licensed. Alpha models are loaded as plugins
  through the `auqua.alpha` entry-point group
  ([ADR 0003](docs/adr/0003-open-core-mit-and-alpha-plugin.md)).

## Development

Requirements: Linux, [uv](https://docs.astral.sh/uv/) 0.13.5 (CI installs exactly
this version; `required-version` refuses older ones), Git. uv installs Python 3.14.

```bash
uv sync --locked                                  # project environment
uv sync --locked --project tools                  # provenance scanner environment
uv run --locked pre-commit install                # hooks run on every commit and push
uv run --locked ruff check . && uv run --locked ruff format --check .
uv run --locked mypy && uv run --locked lint-imports
uv run --locked pytest -q
uv build --no-sources
```

Every commit and every push passes the provenance gate (SCANOSS and ScanCode);
CI repeats the ScanCode checks on every file
([ADR 0013](docs/adr/0013-provenance-matching-at-push.md)).

## Layout

| Path | Content |
|---|---|
| `src/auqua/` | The platform package; one subpackage per component |
| `tests/` | Test suite |
| `tools/` | Gate checkers and the locked scanner environment |
| `compliance/` | Licence policy |
| `docs/adr/` | Architecture decision records |

## Licence and security

MIT, see [LICENSE](LICENSE). Vulnerabilities: see [SECURITY.md](SECURITY.md).
Contributions: see [CONTRIBUTING.md](CONTRIBUTING.md).
