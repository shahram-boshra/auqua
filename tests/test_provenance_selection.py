# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Tests for provenance file selection from Git objects (decision log a10)."""

import os
import subprocess
from pathlib import Path

import pytest

from tools.provenance_scan import (
    FROM_REF,
    LOCAL_BRANCH,
    TO_REF,
    PushScope,
    export_files,
    main,
    parse_blobs,
    push_scope,
    scanoss_command,
    select_snapshot,
)

#: Exit status of ``argparse.ArgumentParser.error``.
ARGPARSE_USAGE_ERROR = 2


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603 - fixed argument list
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],  # noqa: S607
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit(repo: Path, files: dict[str, str], message: str) -> str:
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated Git repository as the working directory."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    _git(tmp_path, "init", "-q", "-b", "main")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_parse_blobs_keeps_regular_merged_files() -> None:
    """Symlinks, submodules and unmerged index entries are not scanned."""
    tree = (
        b"100644 blob aaa\ta.py\x00100755 blob bbb\tbin/run\x00"
        b"120000 blob ccc\tlink\x00160000 commit ddd\tsub\x00"
    )
    assert parse_blobs(tree, index=False) == {"a.py": "aaa", "bin/run": "bbb"}
    index = b"100644 aaa 0\ta.py\x00100644 bbb 2\tc.py\x00120000 ccc 0\tlink\x00"
    assert parse_blobs(index, index=True) == {"a.py": "aaa"}


def test_push_scope_from_pre_commit_environment() -> None:
    """A ref range wins; the pushed ref alone means the push contains the root commit."""
    assert push_scope({FROM_REF: "a", TO_REF: "b", LOCAL_BRANCH: "x"}) == PushScope("a", "b")
    assert push_scope({LOCAL_BRANCH: "refs/heads/main"}) == PushScope(None, "refs/heads/main")
    with pytest.raises(ValueError, match="--pushed needs"):
        push_scope({FROM_REF: "a"})


def test_staged_reads_index_not_working_tree(repo: Path, tmp_path: Path) -> None:
    """Staged contents are scanned even when the working tree differs."""
    _commit(repo, {"a.py": "one\n"}, "first")
    (repo / "a.py").write_text("staged\n", encoding="utf-8")
    _git(repo, "add", "a.py")
    (repo / "a.py").write_text("unstaged\n", encoding="utf-8")
    snapshot = select_snapshot("staged", {})
    assert snapshot.selected == ("a.py",)
    export_files(snapshot, tmp_path / "out")
    assert (tmp_path / "out" / "a.py").read_text(encoding="utf-8") == "staged\n"


def test_pushed_range_selects_changed_files_of_the_tip(repo: Path, tmp_path: Path) -> None:
    """Only files added or changed in the pushed commits, with the tip's contents."""
    base = _commit(repo, {"a.py": "a\n", "b.py": "b\n", "gone.py": "g\n"}, "base")
    (repo / "gone.py").unlink()
    tip = _commit(repo, {"b.py": "b2\n", "c.py": "c\n"}, "tip")
    (repo / "c.py").write_text("not pushed\n", encoding="utf-8")
    snapshot = select_snapshot("pushed", {FROM_REF: base, TO_REF: tip})
    assert snapshot.selected == ("b.py", "c.py")
    export_files(snapshot, tmp_path / "out")
    assert (tmp_path / "out" / "c.py").read_text(encoding="utf-8") == "c\n"


def test_pushed_root_commit_selects_every_file(repo: Path) -> None:
    """A push containing the root commit scans the whole pushed ref."""
    _commit(repo, {"a.py": "a\n", "d/b.py": "b\n"}, "root")
    assert select_snapshot("pushed", {LOCAL_BRANCH: "refs/heads/main"}).selected == (
        "a.py",
        "d/b.py",
    )


def test_pushed_unrelated_history_selects_every_file(repo: Path) -> None:
    """Without a merge base the whole tip is scanned, not a tree difference."""
    old = _commit(repo, {"a.py": "a\n"}, "old")
    _git(repo, "checkout", "-q", "--orphan", "new")
    tip = _commit(repo, {"a.py": "a\n", "b.py": "b\n"}, "new")
    assert select_snapshot("pushed", {FROM_REF: old, TO_REF: tip}).selected == ("a.py", "b.py")


def test_pushed_without_refs_fails(repo: Path) -> None:
    """--pushed outside a pre-push hook is refused (fail closed)."""
    _commit(repo, {"a.py": "a\n"}, "root")
    assert main(["--pushed"]) == 1


def test_no_scanoss_only_with_all() -> None:
    """SCANOSS may be skipped only by the CI backstop (--all)."""
    with pytest.raises(SystemExit) as exit_info:
        main(["--staged", "--no-scanoss"])
    assert exit_info.value.code == ARGPARSE_USAGE_ERROR


def test_scanoss_ignores_settings_files() -> None:
    """No settings file can change the SCANOSS scan."""
    assert scanoss_command("scanoss-py", ["a.py"]) == [
        "scanoss-py",
        "scan",
        "--skip-settings-file",
        "--files",
        "a.py",
    ]
