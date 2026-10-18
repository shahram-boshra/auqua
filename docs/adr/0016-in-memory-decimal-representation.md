<!--
SPDX-FileCopyrightText: 2026 Asadollah Boshra
SPDX-License-Identifier: MIT
-->

# 0016. In-memory decimal representation

Date: 2026-10-18

## Status

Accepted

## Context

ADR 0008 requires exact decimal values with explicit rounding and left the concrete representation to the core-contracts design. Increments are not always powers of ten (for example 0.25 index points), and the JSON decoder accepts `NaN`, infinities and negative zero as decimal values.

## Decision

Money, prices, quantities, fees, P&L and margin are `decimal.Decimal` values in memory. Arithmetic on trading state runs under one context that traps float mixing, invalid operations, division by zero and overflow, with 50 significant digits. Every rounding to an increment goes through one function that takes an explicit rounding mode, computes on exact rationals and returns the value in the increment's scale. A value that must already be on its grid is checked exactly and refused if it is not. Binary floats enter trading state only through one conversion function that converts exactly and then rounds. Every contract type refuses non-finite decimals and negative zero. Values are rounded to the instrument grid when they enter the platform, so equal values have equal text. ADR 0008 remains in force; this record completes its deferred choice.

## Consequences

Storage without a decimal type uses an exact encoding chosen in the storage design. Code outside `auqua.contracts` rounds only through the shared function.
