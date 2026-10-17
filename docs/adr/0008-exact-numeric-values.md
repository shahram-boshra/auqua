<!--
SPDX-FileCopyrightText: 2026 Asadollah Boshra
SPDX-License-Identifier: MIT
-->

# 0008. Exact numeric values for money, prices and quantities

Date: 2026-10-16

## Status

Accepted

## Context

Binary floating point cannot represent many decimal values exactly; differences accumulate and break equality checks. Accounting needs exact results.

## Decision

Money, price, quantity, fees, P&L and margin in canonical events and in order, portfolio, accounting and risk state are exact decimal values with explicit per-instrument precision. Every rounding step is explicit and uses a declared rounding mode. Binary floating point is allowed only in research analytics and model computation, and enters trading state only through an explicit, validated conversion. Exactness is preserved in every store.

## Consequences

The concrete representation (for example `decimal.Decimal` or scaled integers) is chosen in the core-contracts design. Stores without a decimal type need an exact encoding.
