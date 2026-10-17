# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Licence policy check for an installed Python environment.

Classifies the ``License-Expression`` core-metadata field (PEP 639) of every
distribution installed in the interpreter that runs this module against
``compliance/license-policy.toml`` (Strategy section 18; BP0 B7.4).

Scopes:
    runtime: gate. A denied licence, a review licence without a recorded
        approval, or a missing or unparsable expression fails (exit code 1).
    development: report only. Development tools are run unmodified and never
        distributed (decision log a7, L-1).
"""

import argparse
import fnmatch
import importlib.metadata
import re
import sys
import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

DEFAULT_POLICY = Path("compliance/license-policy.toml")
_TOKEN = re.compile(r"\(|\)|[^\s()]+")


class Verdict(IntEnum):
    """Policy outcome, ordered from best to worst."""

    ALLOW = 0
    REVIEW = 1
    UNKNOWN = 2
    DENY = 3


@dataclass(frozen=True)
class Approval:
    """A recorded human decision for one package version and expression."""

    package: str
    version: str
    expression: str


@dataclass(frozen=True)
class Policy:
    """Allow, review and deny patterns (``fnmatch``, case-sensitive)."""

    allow: tuple[str, ...]
    review: tuple[str, ...]
    deny: tuple[str, ...]
    approvals: tuple[Approval, ...] = ()

    def classify_id(self, license_id: str) -> Verdict:
        """Classify one SPDX licence identifier; deny takes precedence."""
        if any(fnmatch.fnmatchcase(license_id, p) for p in self.deny):
            return Verdict.DENY
        if any(fnmatch.fnmatchcase(license_id, p) for p in self.allow):
            return Verdict.ALLOW
        if any(fnmatch.fnmatchcase(license_id, p) for p in self.review):
            return Verdict.REVIEW
        return Verdict.UNKNOWN

    def is_approved(self, package: str, version: str, expression: str) -> bool:
        """Return whether a recorded approval covers this exact case."""
        return Approval(package, version, expression) in self.approvals


class _Parser:
    """Recursive-descent evaluator for SPDX licence expressions.

    ``OR`` yields the best operand, ``AND`` the worst, and ``WITH`` an
    exception is at least ``REVIEW``.
    """

    def __init__(self, tokens: Sequence[str], policy: Policy) -> None:
        self._tokens = tokens
        self._pos = 0
        self._policy = policy

    def parse(self) -> Verdict:
        verdict = self._or()
        if self._pos != len(self._tokens):
            raise ValueError("unexpected token")
        return verdict

    def _peek(self) -> str | None:
        return self._tokens[self._pos] if self._pos < len(self._tokens) else None

    def _take(self) -> str:
        token = self._peek()
        if token is None:
            raise ValueError("unexpected end of expression")
        self._pos += 1
        return token

    def _or(self) -> Verdict:
        verdict = self._and()
        while (token := self._peek()) is not None and token.upper() == "OR":
            self._take()
            verdict = min(verdict, self._and())
        return verdict

    def _and(self) -> Verdict:
        verdict = self._factor()
        while (token := self._peek()) is not None and token.upper() == "AND":
            self._take()
            verdict = max(verdict, self._factor())
        return verdict

    def _factor(self) -> Verdict:
        symbol = self._take()
        if symbol == "(":
            verdict = self._or()
            if self._take() != ")":
                raise ValueError("missing closing parenthesis")
            return verdict
        if symbol == ")" or symbol.upper() in {"AND", "OR", "WITH"}:
            raise ValueError(f"unexpected {symbol!r}")
        verdict = self._policy.classify_id(symbol)
        if (nxt := self._peek()) is not None and nxt.upper() == "WITH":
            self._take()
            self._take()
            verdict = max(verdict, Verdict.REVIEW)
        return verdict


def evaluate(expression: str | None, policy: Policy) -> Verdict:
    """Classify a whole SPDX expression; missing or malformed is UNKNOWN."""
    if expression is None or not expression.strip():
        return Verdict.UNKNOWN
    try:
        return _Parser(_TOKEN.findall(expression), policy).parse()
    except ValueError:
        return Verdict.UNKNOWN


def load_policy(path: Path, scope: str) -> Policy:
    """Load one scope table from the policy file."""
    with path.open("rb") as handle:
        table = tomllib.load(handle)[scope]
    approvals = tuple(
        Approval(a["package"], a["version"], a["expression"]) for a in table.get("approvals", [])
    )
    return Policy(
        allow=tuple(table["allow"]),
        review=tuple(table["review"]),
        deny=tuple(table["deny"]),
        approvals=approvals,
    )


def canonical_name(name: str) -> str:
    """Normalise a distribution name (PEP 503)."""
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass(frozen=True)
class Finding:
    """Result for one installed distribution."""

    package: str
    version: str
    expression: str | None
    verdict: Verdict
    approved: bool

    @property
    def blocking(self) -> bool:
        """Whether this finding fails the runtime gate."""
        if self.verdict is Verdict.DENY:
            return True
        return self.verdict is not Verdict.ALLOW and not self.approved


def check(
    distributions: Iterable[importlib.metadata.Distribution], policy: Policy
) -> list[Finding]:
    """Evaluate every distribution, sorted by package name."""
    findings = []
    for dist in distributions:
        name = canonical_name(dist.metadata["Name"])
        version = dist.version
        expression = dist.metadata.get("License-Expression")
        verdict = evaluate(expression, policy)
        approved = expression is not None and policy.is_approved(name, version, expression)
        findings.append(Finding(name, version, expression, verdict, approved))
    return sorted(findings, key=lambda f: f.package)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the check and print one line per distribution."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scope", choices=("runtime", "development"), required=True)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    args = parser.parse_args(argv)
    findings = check(importlib.metadata.distributions(), load_policy(args.policy, args.scope))
    for f in findings:
        state = "APPROVED" if f.approved else f.verdict.name
        sys.stdout.write(f"{state:8} {f.package}=={f.version}  {f.expression or '<none>'}\n")
    blocking = [f for f in findings if f.blocking]
    sys.stdout.write(f"{len(findings)} distributions, {len(blocking)} not cleared ({args.scope})\n")
    if args.scope == "runtime" and blocking:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
