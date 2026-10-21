# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Ingestion lineage and data-quality events (BP4 sections 4.1, 4.5 and 4.7; ADR 0026).

Every ingestion run ends with one :class:`IngestRun` record naming what was
read (raw SHA-256, retrieval, provider checksum), how (parser and its source
hash), under which terms (source profile and the operator's declared use) and
what came out (counts). Missing intervals are recorded as :class:`DataGap`
events and never filled.
"""

import re
from enum import StrEnum

from auqua.contracts.errors import InvalidFieldError
from auqua.contracts.events.base import Event
from auqua.contracts.validation import require_match, require_positive_int, require_time_ns

_SHA256_HEX = 64
#: ISO 3166-1 alpha-2 country codes.
COUNTRY_CODE = re.compile(r"[A-Z]{2}")


def require_sha256_hex(value: str, name: str) -> None:
    """Refuse anything but 64 lower-case hex digits."""
    if len(value) != _SHA256_HEX or value.strip("0123456789abcdef"):
        raise InvalidFieldError(f"{name} must be 64 lower-case hex digits")


class UseClass(StrEnum):
    """Use the operator declares for data from a source (decision log a25, a28).

    ``unrestricted`` is for data whose terms impose no use restriction (for
    example works of the US Government, 17 U.S.C. 105).
    """

    PERSONAL_RESEARCH = "personal_research"
    INTERNAL_COMMERCIAL = "internal_commercial"
    UNRESTRICTED = "unrestricted"
    LICENSED = "licensed"


class ChecksumResult(StrEnum):
    """Outcome of the provider checksum check."""

    VERIFIED = "verified"
    NOT_PUBLISHED = "not_published"
    MISMATCH = "mismatch"


class RunOutcome(StrEnum):
    """How an ingestion run ended."""

    COMPLETED = "completed"
    ALREADY_INGESTED = "already_ingested"
    REJECTED = "rejected"


class AvailabilityBasis(StrEnum):
    """How the availability time of the run's records was established (BP4 section 4.6)."""

    EVENT_END = "event_end"
    PUBLISHED_SCHEDULE = "published_schedule"
    DECLARED = "declared"
    UNKNOWN = "unknown"


class GapReason(StrEnum):
    """Why an expected interval has no record."""

    NO_DATA_PUBLISHED = "no_data_published"
    MISSING = "missing"


class IngestRun(Event, frozen=True, tag="ingest.run.v1"):
    """Lineage record of one ingestion run, appended after the run's records.

    The run key is (``profile``, ``raw_sha256``, ``parser_id``). Counts:
    ``appended`` new records, ``duplicates`` records already stored with the
    same content, ``revisions`` records stored as a new revision, ``gaps``
    :class:`DataGap` events, ``unavailable`` records without a known
    availability time. ``jurisdiction`` is the operator's declared country of
    residence and access under which the source was cleared (ADR 0029).
    """

    profile: str
    terms: str
    use_class: UseClass
    raw_sha256: str
    raw_size: int
    retrieval: str
    checksum: ChecksumResult
    parser: str
    parser_version: str
    parser_id: str
    outcome: RunOutcome
    availability: AvailabilityBasis
    appended: int = 0
    duplicates: int = 0
    revisions: int = 0
    gaps: int = 0
    unavailable: int = 0
    message: str | None = None
    jurisdiction: str | None = None

    def __post_init__(self) -> None:
        """Validate hashes and counts."""
        super().__post_init__()
        require_sha256_hex(self.raw_sha256, "raw_sha256")
        require_sha256_hex(self.parser_id, "parser_id")
        for name in ("raw_size", "appended", "duplicates", "revisions", "gaps", "unavailable"):
            if getattr(self, name) < 0:
                raise InvalidFieldError(f"{name} must be >= 0")
        for name in ("profile", "terms", "retrieval", "parser", "parser_version"):
            if not getattr(self, name):
                raise InvalidFieldError(f"{name} must not be empty")
        if self.jurisdiction is not None:
            require_match(self.jurisdiction, COUNTRY_CODE, "jurisdiction")


class DataGap(Event, frozen=True, tag="data.gap.v1"):
    """An expected interval ``[start_ns, end_ns)`` of ``key`` without a record.

    ``key`` is the canonical text of the instrument or series id;
    ``interval_ns`` is the bar interval, or ``None`` for business-day series.
    ``ts_event_ns`` equals ``start_ns``.
    """

    key: str
    start_ns: int
    end_ns: int
    reason: GapReason
    interval_ns: int | None = None

    def __post_init__(self) -> None:
        """Validate the interval."""
        super().__post_init__()
        require_time_ns(self.start_ns, "start_ns")
        if self.end_ns <= self.start_ns:
            raise InvalidFieldError("end_ns must be after start_ns")
        if self.ts_event_ns != self.start_ns:
            raise InvalidFieldError("ts_event_ns must equal start_ns")
        if self.interval_ns is not None:
            require_positive_int(self.interval_ns, "interval_ns")
        if not self.key:
            raise InvalidFieldError("key must not be empty")
