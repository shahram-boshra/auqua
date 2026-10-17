<!--
SPDX-FileCopyrightText: 2026 Asadollah Boshra
SPDX-License-Identifier: MIT
-->

# 0002. Permanent non-goals, including no high-frequency trading

Date: 2026-10-16

## Status

Accepted

## Context

Ultra-low-latency trading needs a different execution core: hardware acceleration, kernel bypass, exchange co-location and native binary order-entry protocols. Designing for it would distort every other part of a low-frequency platform.

## Decision

auqua is a low-frequency multi-asset platform. The following are permanently excluded, not deferred: high-frequency and ultra-low-latency trading and any sub-microsecond claim; sub-microsecond C++ or Rust execution paths; FPGA acceleration, kernel bypass and exchange co-location; native low-latency exchange protocols. Market access is through provider-neutral broker and venue adapters. Compiled acceleration of profiled analytical hot paths remains in scope.

## Consequences

No component may be justified by, or claim, ultra-low-latency performance. Reversing any item requires a superseding record and, for high-frequency trading, a new execution core.
