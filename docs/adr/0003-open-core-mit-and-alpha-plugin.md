<!--
SPDX-FileCopyrightText: 2026 Asadollah Boshra
SPDX-License-Identifier: MIT
-->

# 0003. Open core under MIT with a private alpha plugin

Date: 2026-10-16

## Status

Accepted

## Context

The platform is developed for its owner's own use. Most of it benefits from public development and free public CI; the alpha models are the owner's private work.

## Decision

This repository is published under the MIT License and contains the whole platform plus a public baseline alpha plugin. Alpha engines are separate distributions that depend on `auqua` and register through the `auqua.alpha` entry-point group. `auqua` never imports a private alpha package; an import-linter contract enforces this in CI.

## Consequences

The platform runs and is tested without any private code. MIT grants no explicit patent licence; this is accepted for own use. Copied third-party code keeps its own licence obligations regardless of this project's licence.
