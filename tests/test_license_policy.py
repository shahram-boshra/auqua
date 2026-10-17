# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Tests for the licence policy evaluator (tools/license_policy.py)."""

from pathlib import Path

import pytest

from tools.license_policy import Approval, Finding, Policy, Verdict, evaluate, load_policy

POLICY = Policy(
    allow=("MIT", "Apache-2.0", "BSD-3-Clause"),
    review=("MPL-2.0",),
    deny=("GPL-*", "AGPL-*"),
)


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("MIT", Verdict.ALLOW),
        ("Apache-2.0 OR BSD-3-Clause", Verdict.ALLOW),
        ("MIT AND MPL-2.0", Verdict.REVIEW),
        ("MIT OR GPL-3.0-only", Verdict.ALLOW),
        ("MIT AND GPL-3.0-only", Verdict.DENY),
        ("(MIT OR MPL-2.0) AND Apache-2.0", Verdict.ALLOW),
        ("Apache-2.0 WITH LLVM-exception", Verdict.REVIEW),
        ("LGPL-3.0-or-later", Verdict.UNKNOWN),
        ("AGPL-3.0-or-later OR MPL-2.0", Verdict.REVIEW),
        ("MIT and Apache-2.0", Verdict.ALLOW),
        (None, Verdict.UNKNOWN),
        ("", Verdict.UNKNOWN),
        ("MIT AND", Verdict.UNKNOWN),
        ("(MIT", Verdict.UNKNOWN),
    ],
)
def test_evaluate(expression: str | None, expected: Verdict) -> None:
    """OR takes the best operand, AND the worst; malformed is UNKNOWN."""
    assert evaluate(expression, POLICY) is expected


def test_deny_precedes_allow() -> None:
    """An identifier matching both lists is denied."""
    policy = Policy(allow=("*",), review=(), deny=("GPL-*",))
    assert evaluate("GPL-2.0-only", policy) is Verdict.DENY


def test_blocking_rules() -> None:
    """Only allow, or an approved review/unknown, clears the gate; deny never does."""
    assert not Finding("a", "1", "MIT", Verdict.ALLOW, approved=False).blocking
    assert Finding("a", "1", "MPL-2.0", Verdict.REVIEW, approved=False).blocking
    assert not Finding("a", "1", "MPL-2.0", Verdict.REVIEW, approved=True).blocking
    assert Finding("a", "1", "GPL-3.0-only", Verdict.DENY, approved=True).blocking


def test_repository_policy_loads() -> None:
    """The committed policy file defines both scopes with no runtime approvals."""
    path = Path("compliance/license-policy.toml")
    runtime = load_policy(path, "runtime")
    assert runtime.approvals == ()
    assert evaluate("MIT", runtime) is Verdict.ALLOW
    assert evaluate("GPL-3.0-or-later", load_policy(path, "development")) is Verdict.DENY


def test_approval_matches_exact_case() -> None:
    """An approval applies to one package, version and expression only."""
    policy = Policy((), ("MPL-2.0",), (), (Approval("pkg", "1.0", "MPL-2.0"),))
    assert policy.is_approved("pkg", "1.0", "MPL-2.0")
    assert not policy.is_approved("pkg", "1.1", "MPL-2.0")
