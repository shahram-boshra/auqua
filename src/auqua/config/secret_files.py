# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Secrets from files (ADR 0019).

One file per secret in a per-environment directory. The directory must be
owned by the running user with no group or other permissions, and so must each
file (the rule OpenSSH applies to private keys). Secrets are never read from
TOML or from environment variables.
"""

import os
import re
import stat
from pathlib import Path

from pydantic import SecretStr

from auqua.config.errors import SecretError

#: Secret names: upper-case letters, digits and underscores.
SECRET_NAME = re.compile(r"[A-Z][A-Z0-9_]*")
_GROUP_OR_OTHER = 0o077


def _check_private(path: Path, *, directory: bool) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as error:
        raise SecretError(f"{path}: missing") from error
    kind_ok = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not kind_ok:
        raise SecretError(f"{path}: not a {'directory' if directory else 'regular file'}")
    if info.st_uid != os.geteuid():
        raise SecretError(f"{path}: not owned by the running user")
    if info.st_mode & _GROUP_OR_OTHER:
        mode = stat.S_IMODE(info.st_mode)
        raise SecretError(f"{path}: mode {mode:o} gives group or other access")


class SecretsDirectory:
    """Read named secrets from one environment's secrets directory."""

    def __init__(self, root: Path) -> None:
        """Check the directory itself.

        Raises:
            SecretError: the directory is missing or not private.
        """
        _check_private(root, directory=True)
        self._root = root

    def read(self, name: str) -> SecretStr:
        """Return the secret ``name``; one trailing newline is removed.

        Raises:
            SecretError: the name is invalid, or the file is missing or not private.
        """
        if SECRET_NAME.fullmatch(name) is None:
            raise SecretError(f"invalid secret name {name!r}")
        path = self._root / name
        _check_private(path, directory=False)
        value = path.read_text(encoding="utf-8")
        return SecretStr(value.removesuffix("\n"))
