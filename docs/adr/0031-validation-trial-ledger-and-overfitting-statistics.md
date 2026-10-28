<!--
SPDX-FileCopyrightText: 2026 Asadollah Boshra
SPDX-License-Identifier: MIT
-->

# 0031. Validation, trial ledger and overfitting statistics

Date: 2026-10-21

## Status

Accepted

## Context

Searching many configurations and reusing evaluation data inflates backtest results: the best of many noisy trials looks good by chance, and a test set consulted repeatedly stops being out of sample.

## Decision

An experiment declares train, validation and test periods, ordered and disjoint. Configurations are selected on train and validation only; the selection procedure itself is validated by walk-forward or by purged k-fold with embargo. The test period is read only by an explicit test evaluation; the first evaluation of a hypothesis is out of sample, any later one is reported as in sample. Every configuration evaluated is a trial in an append-only ledger per environment that spans runs; the Deflated Sharpe Ratio uses the ledger's number of configurations and the variance of their validation Sharpe ratios on the per-bar scale, with 0.95 as the default threshold; the Probability of Backtest Overfitting is estimated by combinatorially symmetric cross-validation over the run's configurations. Accounting stays exact; statistics are computed in floating point from the exact equity at one conversion boundary and never enter trading state; annualised Sharpe ratios are reported from daily returns with Lo's adjustment, autocorrelations beyond the Newey-West bandwidth taken as zero. Only the Python standard library is used.

## Consequences

N cannot be reduced by splitting a search into several runs; renaming a hypothesis is disclosed in its report. Statistics match published values (the Deflated Sharpe Ratio paper's example is a test).
