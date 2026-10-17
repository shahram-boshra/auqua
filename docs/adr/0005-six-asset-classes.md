<!--
SPDX-FileCopyrightText: 2026 Asadollah Boshra
SPDX-License-Identifier: MIT
-->

# 0005. Six asset classes with explicit verification levels

Date: 2026-10-16

## Status

Accepted

## Context

The platform must support equities and ETFs, futures, options, FX, crypto (spot and perpetual swaps) and fixed income. Free historical data does not exist at the same quality for every class.

## Decision

All six asset classes are fully built; none is reduced to a schema. Each is verified at the highest available level: V1, tested against real historical data; or V2, tested with deterministic fixtures derived from the instruments' contract specifications. A V2 class is upgraded to V1 when real data is acquired.

## Consequences

Every asset-class feature states its verification level. A V2 result is evidence of specification conformance, not of behaviour on real market data.
