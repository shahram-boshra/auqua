# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Storage errors (BP3 section 4).

Messages name records by (``source``, ``id``, ``sequence``) and files by path;
they never contain event payloads.
"""


class StorageError(Exception):
    """Base of every storage error."""


class LogCorruptionError(StorageError):
    """A sequence gap, a hash-chain mismatch or an inconsistent record was read."""


class DuplicateConflictError(StorageError):
    """An event repeats an existing (``source``, ``id``) with different content."""


class WriterLockedError(StorageError):
    """Another writer holds the event log."""


class EncodingError(StorageError):
    """A value cannot be stored exactly in the declared representation."""


class SchemaMismatchError(StorageError):
    """A type or file does not match the declared dataset schema."""


class UnsupportedStoreVersionError(StorageError):
    """A database or manifest version has no path to the version the code supports."""
