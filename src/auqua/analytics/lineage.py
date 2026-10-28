# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Code and run identity — walking skeleton (thin); owned and extended by BP5 and BP10.

``code_id`` hashes the source that is actually imported: the relative path and
SHA-256 of every file in the ``auqua`` package directory (``__pycache__``
excluded), so editable installs (``uv sync`` installs the project editable)
and wheel installs of the same source have the same identity. A run id is a
name-based UUIDv8 over everything that determines the run's output.
"""

import hashlib
import importlib.metadata
import platform
import uuid
from dataclasses import dataclass
from pathlib import Path

import auqua
from auqua.contracts.identity import derived_id

#: Namespace of run ids.
RUN_NAMESPACE = derived_id(uuid.NAMESPACE_URL, b"https://github.com/shahram-boshra/auqua#run")


def code_id(package_dir: Path | None = None) -> str:
    """SHA-256 over the sorted (relative path, file SHA-256) pairs of the package source."""
    root = package_dir or Path(auqua.__file__).parent
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        relative = path.relative_to(root).as_posix()
        if "__pycache__" in relative.split("/"):
            continue
        digest.update(relative.encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


@dataclass(frozen=True)
class Environment:
    """Interpreter version and the lock file's SHA-256 (``None`` outside a project checkout)."""

    python_version: str
    uv_lock_sha256: str | None


def environment(package_dir: Path | None = None) -> Environment:
    """The run environment (Strategy Module 3: "dependency/environment information").

    ``uv.lock`` is looked up in the source checkout that contains the package
    (``<root>/src/auqua``); a wheel installation has none.
    """
    root = (package_dir or Path(auqua.__file__).parent).resolve()
    lock = root.parent.parent / "uv.lock"
    digest = hashlib.sha256(lock.read_bytes()).hexdigest() if lock.is_file() else None
    return Environment(platform.python_version(), digest)


def code_version() -> str:
    """The installed ``auqua`` distribution version."""
    return importlib.metadata.version("auqua")


def run_id(*hex_digests: str) -> uuid.UUID:
    """Identity of a run from the hex SHA-256 digests of everything that determines it.

    The backtest passes, in this order: input log head hash, ``config_id``,
    ``spec_id``, risk-limits SHA-256 and ``code_id``.
    """
    return derived_id(RUN_NAMESPACE, *(bytes.fromhex(d) for d in hex_digests))
