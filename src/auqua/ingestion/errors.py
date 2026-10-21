# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Errors of the ingestion layer (BP4 section 7).

Messages name the source, the raw object's SHA-256 and the row; they never
contain credentials or whole payloads.
"""


class IngestionError(Exception):
    """Base of every ingestion error."""


class TermsNotAcknowledgedError(IngestionError):
    """The source is not enabled, or its terms version is not the acknowledged one."""


class PermitError(IngestionError):
    """The source's permitted-use profile excludes the requested use."""


class ChecksumError(IngestionError):
    """A raw object does not match its provider checksum or its stored hash."""


class ParseError(IngestionError):
    """A raw object does not follow its documented layout, grid or window."""


class FetchError(IngestionError):
    """A retrieval failed after the bounded attempts, or exceeded the size cap."""
