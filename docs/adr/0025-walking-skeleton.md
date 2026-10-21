<!--
SPDX-FileCopyrightText: 2026 Asadollah Boshra
SPDX-License-Identifier: MIT
-->

# 0025. Walking skeleton

Date: 2026-10-18

## Status

Accepted

## Context

Before any component is built in full, one thin path must link them all, so that architecture and functionality evolve together: one historical dataset, canonical events in the event log, a public baseline signal, a portfolio target, the pre-trade risk gate, a simulated venue fill, the portfolio update and a static report. Replaying the same log must reproduce identical orders and P&L, an order breaching a risk limit must be rejected, and every order must trace to its signal, data, configuration and code version. Third-party market data carries terms that may forbid commercial use or redistribution.

## Decision

The skeleton runs on synthetic, deterministic first-party bars generated in tests (a seeded random walk in whole ticks, written in the published kline CSV layout); no third-party data is used or committed. Each component is a thin module in its owning subpackage, marked as such, and is extended by its Blueprint. Six canonical event types are added at version 1: run started, signal, target, order submitted, risk decision and fill; every derived event names the event that caused it. Identifiers that must be reproducible (ingested bars, events derived by a run) are name-based UUIDv8 values over SHA-256. A backtest reads the input log without writing to it and appends its events to its own run log; the run id is derived from the input log's head hash, the configuration, the run specification, the risk limits and a code identity hashed over the imported package source, so the same run produces byte-identical records. Orders fill at the next bar's open, the risk gate checks order notional and resulting position from the risk-limits file, and the static report is one HTML file without scripts. Import contracts keep risk independent of the components it checks, keep signals away from orders and the venue, and keep components from importing the runner.

## Consequences

The three exit criteria are automated tests on every commit. Real-data verification moves to the ingestion Blueprint, with a licence review per source. Each later Blueprint replaces a thin module and evolves its event types through new versions.
