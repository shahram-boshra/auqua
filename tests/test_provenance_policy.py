# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Tests for the first-party ScanCode policy and SCANOSS settings check."""

from pathlib import Path
from typing import Any

from tools.provenance_scan import (
    SPDX_TAG,
    FirstParty,
    license_ids,
    scan_layout,
    scancode_command,
    scancode_problems,
    settings_problems,
)

FIRST_PARTY = FirstParty(frozenset({"MIT"}), frozenset({"Owner Name"}))
KINDS = {"ref.RULE": "reference", "notice.RULE": "notice", "text.LICENSE": "text"}


def _file(path: str, holders: list[str], matches: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "path": f"tree/{path}",
        "type": "file",
        "holders": [{"holder": h} for h in holders],
        "license_detections": [{"matches": matches}],
        "license_clues": [],
    }


def _check(files: list[dict[str, Any]], expected: list[str]) -> list[str]:
    mapping = {p: p for p in expected}
    return scancode_problems({"files": files}, mapping, KINDS.get, FIRST_PARTY)


def _spdx(expression: str, text: str) -> dict[str, str]:
    return {"license_expression_spdx": expression, "matcher": "1-spdx-id", "matched_text": text}


def test_first_party_header_passes() -> None:
    """The owner's SPDX declaration and copyright holder pass."""
    header = _spdx("MIT", f"# {SPDX_TAG}: MIT")
    assert _check([_file("a.py", ["Owner Name"], [header])], ["a.py"]) == []


def test_foreign_spdx_declaration_fails() -> None:
    """A foreign SPDX licence-identifier declaration is refused."""
    header = _spdx("Apache-2.0", f"// {SPDX_TAG}: Apache-2.0")
    assert _check([_file("a.c", [], [header])], ["a.c"]) == [
        "a.c: licence spdx-declaration 'Apache-2.0' is not first-party"
    ]


def test_spdx_mention_passes() -> None:
    """Prose naming the SPDX tag without declaring a licence is a mention."""
    mention = _spdx("LicenseRef-scancode-unknown-spdx", f"carries `{SPDX_TAG}` (REUSE)")
    assert _check([_file("c.md", [], [mention])], ["c.md"]) == []


def test_foreign_holder_fails() -> None:
    """Any other copyright holder is refused."""
    problems = _check([_file("a.py", ["Example Holder"], [])], ["a.py"])
    assert problems == ["a.py: copyright holder 'Example Holder' is not first-party"]


def test_foreign_notice_fails_reference_passes() -> None:
    """Licence notices must be first-party; mere references are allowed."""
    notice = {"license_expression_spdx": "Apache-2.0", "rule_identifier": "notice.RULE"}
    reference = {"license_expression_spdx": "Apache-2.0", "rule_identifier": "ref.RULE"}
    assert _check([_file("b.md", [], [reference])], ["b.md"]) == []
    assert _check([_file("b.md", [], [notice])], ["b.md"]) == [
        "b.md: licence notice 'Apache-2.0' is not first-party"
    ]


def test_unclassified_match_fails() -> None:
    """A rule the index cannot classify is refused (fail closed)."""
    match = {"license_expression_spdx": "LicenseRef-x", "rule_identifier": "unknown"}
    assert _check([_file("c.py", [], [match])], ["c.py"]) == [
        "c.py: unclassified licence match 'LicenseRef-x'"
    ]


def test_missing_file_fails() -> None:
    """A scanned file absent from the results is refused."""
    assert _check([], ["d.py"]) == ["d.py: missing from ScanCode results"]


def test_renamed_file_maps_back() -> None:
    """Files ScanCode skips by name are scanned under a neutral name and reported by real path."""
    layout = scan_layout([".gitignore", "a.py"], lambda rel: rel == ".gitignore")
    assert layout == {"_scanned_by_content/0": ".gitignore", "a.py": "a.py"}
    problems = scancode_problems(
        {"files": [_file("_scanned_by_content/0", ["Example Holder"], [])]},
        layout,
        KINDS.get,
        FIRST_PARTY,
    )
    assert problems == [
        ".gitignore: copyright holder 'Example Holder' is not first-party",
        "a.py: missing from ScanCode results",
    ]


def test_scancode_runs_in_one_process() -> None:
    """ScanCode is bounded to one process (memory on a 4 GB workstation)."""
    command = scancode_command("scancode", Path("out.json"), Path("tree"))
    assert command[command.index("--processes") + 1] == "0"
    assert command[-3:] == ["--json", "out.json", "tree"]


def test_license_ids() -> None:
    """Operators and parentheses are not identifiers."""
    assert license_ids("(MIT OR Apache-2.0) AND BSD-3-Clause") == {
        "MIT",
        "Apache-2.0",
        "BSD-3-Clause",
    }
    assert license_ids(None) == set()


def test_scanoss_declarations_refused() -> None:
    """Declared components in scanoss.json are refused; settings alone pass."""
    assert settings_problems({"settings": {}}) == []
    assert settings_problems({"bom": {"include": [{"purl": "pkg:x/y"}]}}) == [
        "scanoss.json: bom.include declares components; declarations are not allowed (a0 D1/D2)"
    ]
