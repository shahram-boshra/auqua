<!--
SPDX-FileCopyrightText: 2026 Asadollah Boshra
SPDX-License-Identifier: MIT
-->

# 0018. Configuration loading and precedence

Date: 2026-10-18

## Status

Accepted

## Context

Configuration must be typed, validated before any component starts, exact for decimal values, and unambiguous about where each value comes from. The TOML source of pydantic-settings reads files without a decimal parser, and each additional dependency widens the supply-chain surface.

## Decision

Configuration files are TOML, read with the standard library parser configured to return decimal values for TOML floats. Every configuration model derives from one base that is immutable, refuses unknown keys and does not coerce between types. Values are taken, highest first, from explicit arguments (tests only), then from `AUQUA_` environment variables, then from the environment's configuration file, then from model defaults. An environment variable may override only a field marked as overridable; any other `AUQUA_` variable stops the load. Values from environment variables are validated from strings for the field's type. The loader is first-party code on Pydantic; pydantic-settings is not used.

## Consequences

A configuration either loads completely and validated, or the load fails with the file and key that caused it. Each component adds its own typed section to the root model and marks which of its fields may be overridden.
