<!--
SPDX-FileCopyrightText: 2026 Asadollah Boshra
SPDX-License-Identifier: MIT
-->

# 0017. Instrument model and CDM alignment

Date: 2026-10-18

## Status

Accepted

## Context

All six asset classes need one instrument model (ADR 0005). External identifiers have incomplete coverage: FIGIs may be used and redistributed freely under their issuer's terms, but crypto perpetuals are not confirmed to have them, and CFI (ISO 10962) is a paid standard. The FINOS Common Domain Model (CDM) defines a vocabulary for lifecycle events; its Python package declares support up to Python 3.13, depends on a separate runtime, and its model is published under the Community Specification License 1.0, which is outside this project's licence allow list.

## Decision

Each instrument has an internal identifier (venue and venue symbol), a quote currency, a price increment, a quantity increment and a multiplier, and is versioned by validity and knowledge time: a changed specification is a new version. One type per class covers equities and funds, futures, options, FX pairs, crypto spot, crypto perpetual swaps (linear or inverse) and bonds. FIGI, ISIN, CFI and DTI are optional attributes recorded when a source supplies them, never keys. Lifecycle events (corporate actions, futures expiry, option exercise, assignment and expiry, FX settlement, funding payments, coupon payments) take their names and meaning from the CDM primitives and intents, without depending on the CDM package. Currency and instrument data enter as reference data with provenance; the repository bundles no reference lists.

## Consequences

Day-count and accrual arithmetic, and the effect of lifecycle events on positions and cash, belong to the portfolio design. Each asset-class feature states its verification level (ADR 0005).
