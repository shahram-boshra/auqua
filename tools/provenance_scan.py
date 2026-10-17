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

Modes (decision log a10):

- ``--staged`` (pre-commit hook): files added or changed in the index.
- ``--pushed`` (pre-push hook): files added or changed in the pushed commits,
  from the refs pre-commit exports; every file of the pushed ref when the push
  contains the root commit; refused when pre-commit exports neither.
- ``--all``: every file of ``HEAD``. With ``--no-scanoss`` (CI backstop, D4)
  checks 1 and 3 only: GitHub-hosted runners are rate-limited by the OSSKB.

File contents are read from Git objects (the index, or the scanned commit),
never from the working tree, so the scan covers exactly what is committed or
pushed. Runs inside the locked scanner environment defined in
``tools/pyproject.toml``.
"""

import argparse
import importlib
import json
import os
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
#: Git modes of regular files; symlinks (120000) and submodules (160000) hold no content to scan.
_REGULAR_MODES = frozenset({"100644", "100755"})
#: Environment variables set by pre-commit for pre-push hooks (pre-commit 4.7.0 ``run.py``).
FROM_REF = "PRE_COMMIT_FROM_REF"
TO_REF = "PRE_COMMIT_TO_REF"
LOCAL_BRANCH = "PRE_COMMIT_LOCAL_BRANCH"


@dataclass(frozen=True)
class PushScope:
    """Commits of one push: ``base`` is ``None`` when the push contains the root commit."""

    base: str | None
    tip: str


@dataclass(frozen=True)
class Snapshot:
    """A committed or staged tree and the files selected from it.

    Attributes:
        blobs: Repository path -> Git blob id for every regular file of the tree.
        selected: Paths to scan, all present in ``blobs``, sorted.
    """

    blobs: Mapping[str, str]
    selected: tuple[str, ...]


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


def _git(*args: str) -> bytes:
    """Standard output of a git command; a failing command raises."""
    result = subprocess.run(  # noqa: S603 - fixed argument list
        ["git", *args],  # noqa: S607 - git resolved from PATH by design
        check=True,
        capture_output=True,
    )
    return result.stdout


def _git_paths(*args: str) -> list[str]:
    """Return NUL-separated paths printed by a git command."""
    return [p for p in _git(*args).decode("utf-8").split("\0") if p]


def parse_blobs(raw: bytes, *, index: bool) -> dict[str, str]:
    """Regular files in ``git ls-files -s -z`` (index) or ``git ls-tree -r -z`` output.

    Records are NUL-terminated; metadata and path are separated by a tab.
    Index metadata is ``<mode> <blob> <stage>``; only stage 0 (merged) entries
    are kept. Tree metadata is ``<mode> <type> <blob>``.
    """
    blobs: dict[str, str] = {}
    for record in raw.decode("utf-8").split("\0"):
        if not record:
            continue
        meta, path = record.split("\t", 1)
        fields = meta.split()
        if index:
            mode, blob, stage = fields
            if stage != "0":
                continue
        else:
            mode, _, blob = fields
        if mode in _REGULAR_MODES:
            blobs[path] = blob
    return blobs


def push_scope(env: Mapping[str, str]) -> PushScope:
    """Commits to scan for a pre-push hook, from the variables pre-commit exports.

    Raises:
        ValueError: pre-commit exported neither a ref range nor the pushed ref.
    """
    base, tip = env.get(FROM_REF), env.get(TO_REF)
    if base and tip:
        return PushScope(base, tip)
    branch = env.get(LOCAL_BRANCH)
    if branch:
        return PushScope(None, branch)
    raise ValueError(f"--pushed needs {FROM_REF} and {TO_REF}, or {LOCAL_BRANCH} (pre-push hook)")


def _tree_blobs(rev: str) -> dict[str, str]:
    """Regular files of a commit."""
    return parse_blobs(_git("ls-tree", "-r", "-z", f"{rev}^{{commit}}"), index=False)


def _has_merge_base(base: str, tip: str) -> bool:
    """Whether two commits share history (``git merge-base`` exits 1 when not)."""
    result = subprocess.run(  # noqa: S603 - fixed argument list
        ["git", "merge-base", base, tip],  # noqa: S607 - git resolved from PATH by design
        check=False,
        capture_output=True,
    )
    if result.returncode not in (0, 1):
        raise subprocess.CalledProcessError(result.returncode, result.args, stderr=result.stderr)
    return result.returncode == 0


def _snapshot(blobs: Mapping[str, str], paths: Sequence[str] | None) -> Snapshot:
    """Snapshot selecting ``paths`` (all files when ``None``); non-regular paths are dropped."""
    chosen = blobs if paths is None else (p for p in paths if p in blobs)
    return Snapshot(blobs, tuple(sorted(chosen)))


def select_snapshot(mode: str, env: Mapping[str, str]) -> Snapshot:
    """Tree and files for a mode: ``staged``, ``pushed`` or ``all``."""
    if mode == "staged":
        changed = _git_paths("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")
        return _snapshot(parse_blobs(_git("ls-files", "-s", "-z"), index=True), changed)
    if mode == "all":
        return _snapshot(_tree_blobs("HEAD"), None)
    scope = push_scope(env)
    blobs = _tree_blobs(scope.tip)
    # Without shared history a range diff is undefined: scan every file of the tip.
    if scope.base is None or not _has_merge_base(scope.base, scope.tip):
        return _snapshot(blobs, None)
    changed = _git_paths(
        "diff",
        "--name-only",
        "--no-ext-diff",
        "--diff-filter=ACMR",
        "-z",
        f"{scope.base}...{scope.tip}",
    )
    return _snapshot(blobs, changed)


def export_files(snapshot: Snapshot, root: Path) -> None:
    """Write the selected files' Git contents under ``root`` at their repository paths."""
    for rel in snapshot.selected:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_git("cat-file", "blob", snapshot.blobs[rel]))


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


def scanoss_command(scanoss: str, files: Sequence[str]) -> list[str]:
    """scanoss-py scan command line.

    ``--skip-settings-file``: no settings file may change what is scanned or
    how matches are reported (D1/D2); declarations are refused separately.
    """
    return [scanoss, "scan", "--skip-settings-file", "--files", *files]


def run_scanoss(files: Sequence[str], source: Path, workdir: Path) -> list[str]:
    """Scan exported files with scanoss-py; return problems (pending matches or scan errors).

    Runs in ``source`` with repository-relative paths, so results name repository paths.
    """
    scan = subprocess.run(  # noqa: S603 - fixed argument list
        scanoss_command(_tool("scanoss-py"), files),
        check=False,
        capture_output=True,
        text=True,
        cwd=source,
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


def run_scancode(
    files: Sequence[str], source: Path, workdir: Path, first_party: FirstParty
) -> list[str]:
    """Copy exported files into one tree, scan it with ScanCode, apply the policy.

    ScanCode is given a single directory: given several file paths, version
    32.5.0 omitted files from its results. Completeness is checked.
    """
    root = workdir / "tree"
    layout = scan_layout(files, _default_ignored())
    for scanned, rel in layout.items():
        target = root / scanned
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / rel, target)
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
    mode.add_argument("--pushed", action="store_const", const="pushed", dest="mode")
    mode.add_argument("--all", action="store_const", const="all", dest="mode")
    parser.add_argument(
        "--no-scanoss",
        action="store_true",
        help="skip SCANOSS matching (only with --all: CI backstop, decision log a10)",
    )
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    args = parser.parse_args(argv)
    if args.no_scanoss and args.mode != "all":
        parser.error("--no-scanoss is allowed only with --all")

    try:
        snapshot = select_snapshot(args.mode, os.environ)
    except ValueError as error:
        sys.stderr.write(f"PROVENANCE: {error}\n")
        return 1
    problems: list[str] = []
    settings = snapshot.blobs.get(SCANOSS_SETTINGS.as_posix())
    if settings is not None:
        problems += settings_problems(json.loads(_git("cat-file", "blob", settings)))
    files = snapshot.selected
    if files:
        with tempfile.TemporaryDirectory(prefix="auqua-provenance-") as tmp:
            source = Path(tmp) / "source"
            export_files(snapshot, source)
            if not args.no_scanoss:
                problems += run_scanoss(files, source, Path(tmp))
            problems += run_scancode(files, source, Path(tmp), load_first_party(args.policy))
    for problem in problems:
        sys.stderr.write(f"PROVENANCE: {problem}\n")
    skipped = " (SCANOSS not run: --no-scanoss)" if args.no_scanoss else ""
    sys.stdout.write(f"provenance: {len(files)} files, {len(problems)} problems{skipped}\n")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
