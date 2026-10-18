<!--
SPDX-FileCopyrightText: 2026 Asadollah Boshra
SPDX-License-Identifier: MIT
-->

# 0019. Secrets and risk limits

Date: 2026-10-18

## Status

Accepted

## Context

Environment variables are visible to other processes and can appear in logs and dumps. Risk limits protect capital and must not change through ordinary configuration paths.

## Decision

Secrets are read only from files, one file per secret, in a per-environment secrets directory outside the repository. The directory and each file must be owned by the running user and grant no group or other access; otherwise the load fails. Secrets are never read from configuration files or environment variables, are held as masked values, and never appear in error messages. Risk limits live in a separate file per environment, are immutable after loading and cannot be overridden from the environment; the SHA-256 of the file is returned with the configuration and recorded with each decision. Accepting a changed risk-limits file is an operator-authorized action. The permission checks are POSIX checks; the platform targets Linux.

## Consequences

A secrets file with loose permissions stops the platform instead of being used. Every order can be traced to the exact risk-limits file in force.
