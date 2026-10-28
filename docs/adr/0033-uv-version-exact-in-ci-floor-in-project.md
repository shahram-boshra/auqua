<!--
SPDX-FileCopyrightText: 2026 Asadollah Boshra
SPDX-License-Identifier: MIT
-->

# 0033. uv version: exact in CI, a floor in the project

Date: 2026-10-27

## Status

Accepted; supersedes the uv part of 0010

## Context

ADR 0010 pinned uv with `required-version = "==0.13.1"`, so any other uv refuses to run. Dependabot updates `uv.lock` with the uv bundled in its updater (0.13.5 on 2026-10-23) and stops with `tool_version_not_supported` when `required-version` excludes it, so no dependency update, including security updates, could be proposed. The scheduled supply-chain scan of 2026-10-26 then reported high-severity advisories in two development dependencies (virtualenv 21.14.8, filelock 4.2.0) that Dependabot could not fix. `astral-sh/setup-uv` installs the highest uv matching a range, so a range in `required-version` alone would make CI choose its own uv.

## Decision

The exact uv for CI is set once per workflow (`env.UV_VERSION`, passed to `setup-uv` as `version`), and the workstation uses the same version. `required-version` in `pyproject.toml` and `tools/pyproject.toml` is a floor (`>=0.13.5`): it refuses older uv versions and accepts the one Dependabot runs. The floor is raised only to a version that a Dependabot run has reported (`Package manager uv, Info : uv <version>`), so it never excludes Dependabot. Version upgrades, by Dependabot or by hand, take releases that are at least 7 days old (Dependabot `cooldown`). A security fix is exempt, as Dependabot security updates are: it takes the lowest release that fixes every reported advisory, whatever its age.

## Consequences

Dependabot can update both lock files again. CI stays reproducible: one uv version per run, changed by editing `UV_VERSION`. A newer local uv is accepted, so `uv.lock` changes are checked in CI (`uv sync --locked`) with the pinned version. Raising uv is a pull request that changes `UV_VERSION`, the floor and this record's successor.
