<!--
SPDX-FileCopyrightText: 2026 Asadollah Boshra
SPDX-License-Identifier: MIT
-->

# 0011. One distribution and import package in the src layout

Date: 2026-10-16

## Status

Accepted

## Context

The repository needs one importable package whose name matches the distribution, built by a standard backend, with tests run against the installed package.

## Decision

The distribution and import package are both `auqua`, in the `src` layout (`src/auqua/`), built with the `uv_build` backend. Each component is a subpackage owned by one design document; files inside it are added with that component's implementation. Architecture boundaries between subpackages are enforced with import-linter contracts.

## Consequences

No setuptools configuration files exist. A new top-level component is a new subpackage, never a second top-level package.
