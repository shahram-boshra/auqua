# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Schema-version migrations (ADR 0020).

Each migration turns a version-N document into a version-N+1 document. A
document is migrated step by step to the current version; a version without a
path (including any version newer than the code) is refused.
"""

from collections.abc import Callable
from typing import Any

from auqua.config.errors import UnsupportedVersionError

Document = dict[str, Any]
Migration = Callable[[Document], Document]
VERSION_KEY = "schema_version"


class Migrations:
    """Registry of migrations for one kind of file."""

    def __init__(self, kind: str, current: int) -> None:
        """Create an empty registry for ``kind`` whose current version is ``current``."""
        self.kind = kind
        self.current = current
        self._steps: dict[int, Migration] = {}

    def register(self, from_version: int, migration: Migration) -> None:
        """Register the migration from ``from_version`` to ``from_version + 1``.

        Raises:
            ValueError: the step is outside ``1..current-1`` or already registered.
        """
        if not 1 <= from_version < self.current or from_version in self._steps:
            raise ValueError(f"{self.kind}: invalid or duplicate migration from {from_version}")
        self._steps[from_version] = migration

    def upgrade(self, document: Document) -> Document:
        """Return ``document`` migrated to the current version.

        Raises:
            UnsupportedVersionError: the version is missing, not an integer, or
                has no path to the current version.
        """
        version = document.get(VERSION_KEY)
        if type(version) is not int:
            raise UnsupportedVersionError(f"{self.kind}: {VERSION_KEY} must be an integer")
        while version != self.current:
            step = self._steps.get(version)
            if step is None:
                raise UnsupportedVersionError(
                    f"{self.kind}: no migration from version {version} to {self.current}"
                )
            document = step(document)
            version += 1
            if document.get(VERSION_KEY) != version:
                raise UnsupportedVersionError(
                    f"{self.kind}: migration from {version - 1} did not set version {version}"
                )
        return document
