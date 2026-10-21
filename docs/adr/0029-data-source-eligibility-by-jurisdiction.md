<!--
SPDX-FileCopyrightText: 2026 Asadollah Boshra
SPDX-License-Identifier: MIT
-->

# 0029. Data-source eligibility by jurisdiction

Date: 2026-10-21

## Status

Accepted; amends 0027

## Context

Provider terms and sanctions law decide who may use a data source by where the user resides and accesses it from, independently of purpose, machine-learning use and sharing (ADR 0027). A platform that does not record this can be used against a provider's terms without anyone noticing.

## Decision

The operator declares one jurisdiction in the configuration: the ISO 3166-1 alpha-2 code of the country they reside in and access the providers from (configuration schema version 4). Each source profile lists the jurisdictions the project has assessed, each cleared or not, with the basis. Ingestion refuses a source, before anything is fetched, when no jurisdiction is declared, when the declared one has not been assessed for that source, or when it is not cleared. Every lineage record names the jurisdiction under which the source was cleared. The first assessment is for the United States: Coinbase market data, Federal Reserve Board and US Treasury data are cleared; Binance Vision is not, because its terms are subject to the Binance Terms of Use of a platform closed to US users.

## Consequences

Adding a jurisdiction or a source requires reading the provider's terms and recording the result; nothing is cleared by default. Binance Vision remains supported as a format for jurisdictions where it is cleared.
