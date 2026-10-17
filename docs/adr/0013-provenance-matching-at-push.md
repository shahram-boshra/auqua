<!--
SPDX-FileCopyrightText: 2026 Asadollah Boshra
SPDX-License-Identifier: MIT
-->

# 0013. Provenance matching at commit and push; ScanCode in CI

Date: 2026-10-17

## Status

Accepted

Supersedes [0007](0007-provenance-controls.md)

## Context

Code may be written with AI assistance, and such tools can reproduce existing open-source code. Copied code keeps its original licence obligations, and copyleft code cannot be relicensed under MIT. No scanner proves the absence of copying.

ADR 0007 ran the whole provenance scan in CI as well as at commit. The free OSSKB service behind SCANOSS is anonymous and best-effort, and limits the number of requests per user; it refused GitHub-hosted runners for about 15 days ("Rate limit exceeded"). SCANOSS offers no free authenticated access, and self-hosted runners are unsuitable for public repositories. From the maintainer's workstation the service answers.

## Decision

Every commit and every push passes a fail-closed provenance gate on the workstation. SCANOSS matches files and snippets against the OSSKB; ScanCode detects licence and copyright text. A match, a foreign licence text or notice, or a foreign copyright holder refuses the commit or push, and so does a scan that cannot complete. The gate reads file contents from Git objects: the index at commit; at push, the files added or changed in the pushed commits, or every file when the push contains the root commit. SCANOSS ignores settings files, and matches are never declared away in them. Flagged code is rebuilt from its requirement, not edited cosmetically, and re-scanned.

CI runs ScanCode and the SCANOSS settings check on every file of every push and pull request, as a required check, because local hooks can be bypassed. CI does not run SCANOSS matching.

Code from competing engines is not used as a reference or as AI context.

## Consequences

Commits and pushes depend on the availability of the free OSSKB service. A change committed and pushed with `--no-verify` is not matched by SCANOSS; the maintainer never uses `--no-verify`. The pre-push hook checks the first ref of a push, so branches are pushed one at a time. SCANOSS matching returns to CI if the Software Transparency Foundation grants a usage agreement for this project. Findings are hints that need human assessment; passing the gate is evidence, not proof, of originality.
