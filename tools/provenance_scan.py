# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Provenance gate (decision log a0 D1-D4; BP0 B5.1, B7.5).

Checks, on the selected files:

1. ``scanoss.json`` declares no components (a declaration would turn a match
   into a pass, contrary to D1/D2).
2. SCANOSS file and snippet matching (``scanoss-py`` against the OSSKB). Any
   pending match fails; any scan error fails, so an unavailable service
   refuses the commit (D3).
3. ScanCode licence and copyright detection under the first-party policy:
   licence texts, licence notices and SPDX licence-identifier declarations
   must name first-party licences, and every copyright holder
   must be a first-party holder (D1). Other matches (mentions of licence
   names, short identifiers in code) are references and pass.

Modes: ``--staged`` (pre-commit hook) and ``--all`` (CI backstop, D4).
Runs inside the locked scanner environment defined in ``tools/pyproject.toml``.
"""

import argparse
import importlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

DEFAULT_POLICY = Path("compliance/license-policy.toml")
SCANOSS_SETTINGS = Path("scanoss.json")
#: Match kinds that mean licence text or a licence statement is embedded.
BLOCKING_KINDS = frozenset({"text", "notice", "spdx-declaration"})
#: The SPDX tag name. Kept as a bare name so this file holds no tag declaration
#: of its own beyond its header (ScanCode would read "<tag>:" as one).
SPDX_TAG = "SPDX-License-Identifier"
_SPDX_DECLARATION = re.compile(re.escape(SPDX_TAG) + r"\s*:")
#: Placeholder directory for files ScanCode skips by name (for example ``.gitignore``).
_RENAMED = "_scanned_by_content"
_OPERATORS = frozenset({"AND", "OR", "WITH", "(", ")"})


@dataclass(frozen=True)
class FirstParty:
    """First-party licences (SPDX identifiers) and copyright holders."""

    licenses: frozenset[str]
    holders: frozenset[str]


def load_first_party(path: Path) -> FirstParty:
    """Load the ``[first_party]`` table of the policy file."""
    with path.open("rb") as handle:
        table = tomllib.load(handle)["first_party"]
    return FirstParty(frozenset(table["licenses"]), frozenset(table["holders"]))


def _git_paths(*args: str) -> list[str]:
    """Return NUL-separated paths printed by a git command."""
    result = subprocess.run(  # noqa: S603 - fixed argument list
        ["git", *args],  # noqa: S607 - git resolved from PATH by design
        check=True,
        capture_output=True,
    )
    return [p for p in result.stdout.decode("utf-8").split("\0") if p]


def select_files(mode: str) -> list[str]:
    """Return existing regular files: staged (added/changed) or all tracked."""
    if mode == "staged":
        paths = _git_paths("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")
    else:
        paths = _git_paths("ls-files", "-z")
    return sorted(p for p in paths if Path(p).is_file())


def settings_problems(settings: Mapping[str, Any]) -> list[str]:
    """Return problems for any component declared in SCANOSS settings."""
    bom = settings.get("bom", {})
    return [
        f"scanoss.json: bom.{key} declares components; declarations are not allowed (a0 D1/D2)"
        for key in ("include", "remove", "replace")
        if bom.get(key)
    ]


def _tool(name: str) -> str:
    """Path of an executable installed in this interpreter's environment."""
    path = Path(sys.executable).parent / name
    if not path.is_file():
        raise FileNotFoundError(f"{name} not found next to {sys.executable}")
    return str(path)


def run_scanoss(files: Sequence[str], workdir: Path) -> list[str]:
    """Scan with scanoss-py; return problems (pending matches or scan errors)."""
    scan = subprocess.run(  # noqa: S603 - fixed argument list
        [_tool("scanoss-py"), "scan", "--files", *files],
        check=False,
        capture_output=True,
        text=True,
    )
    if scan.returncode != 0:
        return [f"SCANOSS scan failed (exit {scan.returncode}): {scan.stderr.strip()}"]
    raw = scan.stdout.strip()
    try:
        results = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return ["SCANOSS scan output is not valid JSON"]
    if not results:
        return []
    out = workdir / "scanoss-results.json"
    out.write_text(scan.stdout, encoding="utf-8")
    pending = subprocess.run(  # noqa: S603 - fixed argument list
        [_tool("scanoss-py"), "results", str(out), "--has-pending", "--format", "json"],
        check=False,
        capture_output=True,
        text=True,
    )
    if pending.returncode == 0:
        return []
    if pending.returncode == 1:
        return [f"SCANOSS pending (undeclared) matches:\n{pending.stdout.strip()}"]
    return [f"SCANOSS results check failed (exit {pending.returncode}): {pending.stderr.strip()}"]


def license_ids(expression: str | None) -> set[str]:
    """Licence identifiers in an SPDX expression (operators removed)."""
    if not expression:
        return set()
    tokens = expression.replace("(", " ( ").replace(")", " ) ").split()
    return {t for t in tokens if t.upper() not in _OPERATORS}


def match_kind(match: Mapping[str, Any], rule_kind: Callable[[str], str | None]) -> str | None:
    """Kind of one ScanCode match.

    An SPDX-identifier match is a declaration only when its text is a
    line of the form ``<SPDX_TAG>: <expression>``; otherwise it is a mention. Other
    matches take the kind of their ScanCode rule; ``None`` if unknown.
    """
    if match.get("matcher") == "1-spdx-id":
        text = str(match.get("matched_text", ""))
        return "spdx-declaration" if _SPDX_DECLARATION.search(text) else "reference"
    return rule_kind(str(match.get("rule_identifier", "")))


def scancode_problems(
    result: Mapping[str, Any],
    mapping: Mapping[str, str],
    rule_kind: Callable[[str], str | None],
    first_party: FirstParty,
) -> list[str]:
    """Apply the first-party policy to ScanCode JSON output.

    Args:
        result: Parsed ScanCode JSON; paths are ``<root>/<scanned path>``.
        mapping: Scanned path -> repository path; every key must appear.
        rule_kind: Maps a rule identifier to ``text``, ``notice``, ``tag``,
            ``reference``, ``intro`` or ``clue``; ``None`` if unknown.
        first_party: Accepted licences and holders.

    Returns:
        One message per violation.
    """
    problems: list[str] = []
    seen: set[str] = set()
    for entry in result.get("files", []):
        if entry.get("type") != "file":
            continue
        scanned = entry["path"].split("/", 1)[1]
        seen.add(scanned)
        rel = mapping.get(scanned, scanned)
        problems.extend(
            f"{rel}: copyright holder {h['holder']!r} is not first-party"
            for h in entry.get("holders", [])
            if h["holder"] not in first_party.holders
        )
        matches = [m for d in entry.get("license_detections", []) for m in d.get("matches", [])]
        matches.extend(entry.get("license_clues", []))
        for match in matches:
            spdx = match.get("license_expression_spdx")
            kind = match_kind(match, rule_kind)
            if kind is None:
                problems.append(f"{rel}: unclassified licence match {spdx!r}")
            elif kind in BLOCKING_KINDS and not license_ids(spdx) <= first_party.licenses:
                problems.append(f"{rel}: licence {kind} {spdx!r} is not first-party")
    problems.extend(
        f"{mapping[s]}: missing from ScanCode results" for s in sorted(set(mapping) - seen)
    )
    return problems


def _scancode_rule_kinds() -> Callable[[str], str | None]:
    """Build a rule-identifier -> kind lookup from ScanCode's licence index."""
    cache = importlib.import_module("licensedcode.cache")
    index = cast("Any", cache).get_index()
    flags = (
        ("is_license_text", "text"),
        ("is_license_notice", "notice"),
        ("is_license_tag", "tag"),
        ("is_license_reference", "reference"),
        ("is_license_intro", "intro"),
        ("is_license_clue", "clue"),
    )
    kinds: dict[str, str] = {}
    for rule in index.rules_by_rid:
        for attribute, kind in flags:
            if getattr(rule, attribute):
                kinds[str(rule.identifier)] = kind
                break
    return kinds.get


def _default_ignored() -> Callable[[str], bool]:
    """ScanCode's own name-based ignore test (for example VCS files)."""
    ignore = cast("Any", importlib.import_module("commoncode.ignore"))
    return lambda rel: bool(ignore.is_ignored(rel, ignore.default_ignores, {}))


def scan_layout(files: Sequence[str], ignored: Callable[[str], bool]) -> dict[str, str]:
    """Scanned path -> repository path.

    Files ScanCode would skip by name are placed under a neutral name so their
    content is still scanned.
    """
    layout: dict[str, str] = {}
    for index, rel in enumerate(files):
        scanned = f"{_RENAMED}/{index}" if ignored(rel) else rel
        layout[scanned] = rel
    return layout


def scancode_command(scancode: str, out: Path, root: Path) -> list[str]:
    """ScanCode command line.

    ``--processes 0`` scans in one process. ScanCode's default starts
    (number of CPUs - 1) worker processes, each loading its own licence index:
    on the 54 scaffold files the process tree peaked at 2.5 GB with one worker
    and 1.4 GB with none (measured 2026-10-16), which exhausts a 4 GB
    workstation.
    """
    return [
        scancode,
        "--license",
        "--license-text",
        "--copyright",
        "--processes",
        "0",
        "--quiet",
        "--json",
        str(out),
        str(root),
    ]


def run_scancode(files: Sequence[str], workdir: Path, first_party: FirstParty) -> list[str]:
    """Copy files into one tree, scan it with ScanCode, apply the policy.

    ScanCode is given a single directory: given several file paths, version
    32.5.0 omitted files from its results. Completeness is checked.
    """
    root = workdir / "tree"
    layout = scan_layout(files, _default_ignored())
    for scanned, rel in layout.items():
        target = root / scanned
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(rel, target)
    out = workdir / "scancode.json"
    scan = subprocess.run(  # noqa: S603 - fixed argument list
        scancode_command(_tool("scancode"), out, root),
        check=False,
        capture_output=True,
        text=True,
    )
    if scan.returncode != 0:
        return [f"ScanCode failed (exit {scan.returncode}): {scan.stderr.strip()}"]
    result = json.loads(out.read_text(encoding="utf-8"))
    return scancode_problems(result, layout, _scancode_rule_kinds(), first_party)


def main(argv: Sequence[str] | None = None) -> int:
    """Run all checks; exit 1 on any problem."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--staged", action="store_const", const="staged", dest="mode")
    mode.add_argument("--all", action="store_const", const="all", dest="mode")
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    args = parser.parse_args(argv)

    problems: list[str] = []
    if SCANOSS_SETTINGS.is_file():
        problems += settings_problems(json.loads(SCANOSS_SETTINGS.read_text(encoding="utf-8")))
    files = select_files(args.mode)
    if files:
        with tempfile.TemporaryDirectory(prefix="auqua-provenance-") as tmp:
            problems += run_scanoss(files, Path(tmp))
            problems += run_scancode(files, Path(tmp), load_first_party(args.policy))
    for problem in problems:
        sys.stderr.write(f"PROVENANCE: {problem}\n")
    sys.stdout.write(f"provenance: {len(files)} files, {len(problems)} problems\n")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
