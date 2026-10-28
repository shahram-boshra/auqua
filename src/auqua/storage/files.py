# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Private directories and files (least privilege; BP3 section 7, BP4 section 6, BP5 section 6).

Trading history, raw provider data and research outputs are created owner-only:
directories ``0700``, files ``0600``. ``Path.mkdir(mode, parents=True)``
applies ``mode`` to the last directory only: missing parents "are created with
the default permissions without taking *mode* into account" (Python
``pathlib``), so each missing level is created on its own; files are opened
with their mode at creation (``os.open``) rather than chmod-ed after a
default-mode create.
"""

import os
from pathlib import Path

PRIVATE_DIRECTORY = 0o700
PRIVATE_FILE = 0o600


def mkdir_private(path: Path) -> None:
    """Create ``path`` and each missing parent with mode ``0700`` (existing ones unchanged)."""
    missing = [p for p in (path, *path.parents) if not p.exists()]
    for directory in reversed(missing):
        directory.mkdir(mode=PRIVATE_DIRECTORY, exist_ok=True)


def write_private(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` (mode ``0600``), creating private parents.

    The bytes go to a ``0600`` temporary file beside ``path`` that replaces it
    atomically, so a reader never sees a partial file and an existing file of
    a wider mode is replaced by a private one.
    """
    mkdir_private(path.parent)
    temporary = path.with_name(f".{path.name}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, PRIVATE_FILE)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.chmod(PRIVATE_FILE)  # O_TRUNC keeps the mode of a leftover temporary file
    temporary.replace(path)
