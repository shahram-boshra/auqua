<!--
SPDX-FileCopyrightText: 2026 Asadollah Boshra
SPDX-License-Identifier: MIT
-->

# 0007. Code provenance controls from the first commit

Date: 2026-10-16

## Status

Superseded by [0013](0013-provenance-matching-at-push.md)

## Context

Code may be written with AI assistance, and such tools can reproduce existing open-source code. Copied code keeps its original licence obligations, and copyleft code cannot be relicensed under MIT. No scanner proves the absence of copying.

## Decision

Every commit passes a fail-closed provenance gate. SCANOSS matches files and snippets against the OSSKB; ScanCode detects licence and copyright text. A match, a foreign licence text or notice, or a foreign copyright holder refuses the commit, and so does a scan that cannot complete. Flagged code is rebuilt from its requirement, not edited cosmetically, and re-scanned. Matches are never declared away in SCANOSS settings. The same scan runs in CI on every push because local hooks can be bypassed. Code from competing engines is not used as a reference or as AI context.

## Consequences

Commits depend on the availability of the free OSSKB service. Findings are hints that need human assessment; passing the gate is evidence, not proof, of originality.
