<!--
SPDX-FileCopyrightText: 2026 Asadollah Boshra
SPDX-License-Identifier: MIT
-->

# 0032. Robustness and the Research Integrity Report

Date: 2026-10-21

## Status

Accepted

## Context

A configuration ranked by its best historical objective is usually the most overfitted one, and a return curve alone says nothing about the assumptions and data behind it.

## Decision

Every configuration is re-evaluated on the validation period with costs halved and doubled, one more bar of latency (orders still in flight count towards the position), a seeded share of bars dropped, by halves of the validation period, and against its grid neighbours. A configuration is robust when its validation Sharpe ratio and every sweep's are positive and its Deflated Sharpe Ratio meets the threshold; configurations are ranked by robustness, then by their worst Sharpe ratio across sweeps, and the first is selected. Execution costs are a cost model: fees and slippage in basis points, with fill prices rounded to the price grid against the trader; market impact is left to the execution blueprint. Each run writes a Research Integrity Report as JSON with sorted keys, validated against the published report schema before it is written, and as static HTML without scripts, both owner-only (`0600`): hypothesis, lineage, inputs and their terms, assumptions, every check as pass, warn or fail with its value, the selected configuration's statistics, the ranking with all sweeps, the overfitting estimate and the validation of the selection procedure. Reports carry the strictest sharing rule and the attributions of their inputs and say "Not for redistribution" when an input prohibits sharing.

## Consequences

Fragile configurations rank below robust ones even with a higher peak. Reports are reproducible byte for byte and can be reviewed without the code.
