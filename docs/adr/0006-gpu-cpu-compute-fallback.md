<!--
SPDX-FileCopyrightText: 2026 Asadollah Boshra
SPDX-License-Identifier: MIT
-->

# 0006. GPU to CPU compute fallback

Date: 2026-10-16

## Status

Accepted

## Context

Machine-learning workloads may run on a GPU or a CPU depending on the machine. Results may not be reproducible between CPU and GPU executions, even with identical seeds.

## Decision

Accelerator availability is detected at start-up and per job; a GPU is used when available, otherwise the CPU. A GPU failure reruns the job on the CPU, logged and exposed as a metric. The device is recorded with every experiment, and results are compared only between runs on the same device type. Model outputs used by trading decisions are recorded, and replay reads the recorded outputs rather than recomputing them. A compute failure marks alpha as unavailable and never blocks risk or execution.

## Consequences

Replay stays deterministic across machines with different hardware.
