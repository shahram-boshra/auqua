<!--
SPDX-FileCopyrightText: 2026 Asadollah Boshra
SPDX-License-Identifier: MIT
-->

# 0010. Python 3.14 and uv 0.13.1

Date: 2026-10-16

## Status

Accepted

## Context

The core dependencies publish binary wheels per Python version. On 2026-10-16, Python 3.14 is the newest version with PyPI wheels for torch, DuckDB, NumPy, SciPy, PyArrow, Numba and Polars; torch has no Python 3.15 wheel on PyPI and DuckDB has none at all. Local and CI tool versions must match for the gate to give the same result.

## Decision

The project requires Python 3.14 (`.python-version`; `requires-python = ">=3.14"`), installed by uv rather than taken from the operating system. uv is pinned to 0.13.1 with `required-version`, so any other uv version refuses to run; CI installs the same version.

## Consequences

Upgrading Python or uv is a deliberate change made through a pull request, with this record superseded.
