# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Tests for the licence policy evaluator (tools/license_policy.py)."""

import hashlib
import importlib.metadata
from pathlib import Path

import pytest

from tools.license_policy import (
    Approval,
    ConcludedApproval,
    Finding,
    Policy,
    Verdict,
    check,
    evaluate,
    installed_license_files,
    load_policy,
)

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
    assert [(c.package, c.version) for c in runtime.concluded] == [("duckdb", "1.5.6")]
    assert evaluate(runtime.concluded[0].concluded, runtime) is not Verdict.DENY
    assert evaluate("MIT", runtime) is Verdict.ALLOW
    assert evaluate("GPL-3.0-or-later", load_policy(path, "development")) is Verdict.DENY


def test_approval_matches_exact_case() -> None:
    """An approval applies to one package, version and expression only."""
    policy = Policy((), ("MPL-2.0",), (), (Approval("pkg", "1.0", "MPL-2.0"),))
    assert policy.is_approved("pkg", "1.0", "MPL-2.0")
    assert not policy.is_approved("pkg", "1.1", "MPL-2.0")


LICENSE_TEXT = b"Permission is hereby granted, free of charge\n"
LICENSE_PATH = "pkg-1.0.dist-info/licenses/LICENSE"


def _distribution(site: Path, version: str = "1.0") -> importlib.metadata.Distribution:
    """An installed distribution without ``License-Expression``, with one licence file."""
    info = site / "pkg-1.0.dist-info"
    (info / "licenses").mkdir(parents=True)
    (info / "licenses" / "LICENSE").write_bytes(LICENSE_TEXT)
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.4\nName: pkg\nVersion: {version}\n", encoding="utf-8"
    )
    (info / "RECORD").write_text(
        f"{LICENSE_PATH},,\npkg-1.0.dist-info/METADATA,,\npkg-1.0.dist-info/RECORD,,\n",
        encoding="utf-8",
    )
    return importlib.metadata.PathDistribution(info)


def _concluded(expression: str, digest: str) -> Policy:
    approval = ConcludedApproval("pkg", "1.0", expression, ((LICENSE_PATH, digest),), "test")
    return Policy(("MIT",), ("MPL-2.0",), ("GPL-*",), concluded=(approval,))


def test_concluded_approval_clears_only_the_recorded_files(tmp_path: Path) -> None:
    """A concluded approval needs the same version and unchanged licence files (BP3 Z14)."""
    dist = _distribution(tmp_path)
    digest = hashlib.sha256(LICENSE_TEXT).hexdigest()
    (finding,) = check([dist], _concluded("MIT AND BSL-1.0", digest))
    assert (finding.approved, finding.blocking, finding.concluded) == (
        True,
        False,
        "MIT AND BSL-1.0",
    )
    assert installed_license_files(dist) == ((LICENSE_PATH, digest),)
    (changed,) = check([dist], _concluded("MIT", "0" * 64))
    assert changed.blocking
    (denied,) = check([dist], _concluded("GPL-2.0-only", digest))
    assert denied.blocking
    other = _distribution(tmp_path / "other", version="1.1")
    (unrecorded,) = check([other], _concluded("MIT", digest))
    assert (unrecorded.verdict, unrecorded.blocking) == (Verdict.UNKNOWN, True)


def test_concluded_entries_are_validated(tmp_path: Path) -> None:
    """An entry must declare NOASSERTION and name dist-info licence files."""
    base = (
        '[runtime]\nallow = ["MIT"]\nreview = []\ndeny = []\n'
        '[[runtime.concluded]]\npackage = "pkg"\nversion = "1.0"\nconcluded = "MIT"\n'
        'basis = "b"\n'
    )
    for extra, match in (
        (
            f'declared = "MIT"\nlicense_files = {{ "{LICENSE_PATH}" = "x" }}\n',
            "declared",
        ),
        ('declared = "NOASSERTION"\nlicense_files = { "pkg/LICENSE" = "x" }\n', "license_files"),
    ):
        path = tmp_path / "policy.toml"
        path.write_text(base + extra, encoding="utf-8")
        with pytest.raises(ValueError, match=match):
            load_policy(path, "runtime")
