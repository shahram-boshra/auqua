<!--
SPDX-FileCopyrightText: 2026 Asadollah Boshra
SPDX-License-Identifier: MIT
-->

# 0004. Python core orchestrating compiled libraries

Date: 2026-10-16

## Status

Accepted

## Context

The platform targets low-frequency trading (ADR 0002). Python is the dominant language for quantitative research, and mature compiled libraries cover numerical hot paths.

## Decision

The platform is written in Python. Performance-critical work is delegated to compiled libraries (for example Polars, NumPy and Numba). Pure-Python loops are not used on measured hot paths, and performance claims are made only against measured budgets.

## Consequences

One language for research and production code. Any native acceleration is limited to profiled analytical paths (ADR 0002).
