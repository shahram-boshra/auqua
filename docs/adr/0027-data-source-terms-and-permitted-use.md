<!--
SPDX-FileCopyrightText: 2026 Asadollah Boshra
SPDX-License-Identifier: MIT
-->

# 0027. Data-source terms and permitted use

Date: 2026-10-21

## Status

Accepted

## Context

Free market data comes with terms that restrict purpose, machine-learning use and sharing of derived outputs (decision log a25, a28). An open-source platform must not distribute such data and must help its operator stay within the terms of the data they load.

## Decision

The repository ships adapters and source profiles, never third-party data; tests use first-party synthetic files in each provider's documented layout. A source profile records the provider's terms URL and version, the date read, the permitted purposes (research, backtest, paper, live), whether machine-learning use is allowed, whether derived outputs may be shared, and the required attribution. A source runs only when the operator's configuration names it with a declared use class and the terms version accepted; a profile with newer terms stops ingestion until the acknowledgement is updated, and nothing is fetched before this check. Ingestion refuses a source whose purposes exclude the environment's purpose; the alpha boundary refuses sources that prohibit machine-learning use; outputs carry the strictest sharing rule and the attributions of their inputs.

## Consequences

Binance Vision data stays in research and backtests; Coinbase data never reaches model training or validation; public-domain sources are unrestricted and cited. A change of use class or terms is visible in the configuration and in every lineage record.
