# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Time fields (Strategy section 5; ADR 0015).

Every time in a contract is an ``int`` of nanoseconds since the Unix epoch,
UTC. Field names state the kind of time:

- ``ts_event_ns``: when it happened at the origin (event or exchange time);
- ``ts_ingest_ns``: when the platform observed it (ingestion/observation time);
- ``ts_published_ns``: when it became available (publication time);
- ``effective_ns``: when reference or corporate-action data takes effect;
- ``knowledge_ns``: when the platform learned a reference-data version.

The helpers below convert to and from RFC 3339 text for presentation edges
only (reports, configuration files); they never read a clock.
"""

import re
from datetime import UTC, datetime, timedelta

from auqua.contracts.errors import InvalidFieldError
from auqua.contracts.validation import require_time_ns

NS_PER_SECOND = 1_000_000_000
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_RFC3339 = re.compile(
    r"(?P<base>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<fraction>\d{1,9}))?"
    r"(?P<offset>Z|[+-]\d{2}:\d{2})",
)


def to_rfc3339(ns: int) -> str:
    """Format ``ns`` as RFC 3339 UTC text with nine fractional digits."""
    require_time_ns(ns, "ns")
    seconds, nanos = divmod(ns, NS_PER_SECOND)
    moment = _EPOCH + timedelta(seconds=seconds)
    return f"{moment:%Y-%m-%dT%H:%M:%S}.{nanos:09d}Z"


def from_rfc3339(text: str) -> int:
    """Parse RFC 3339 text with an explicit offset into nanoseconds since the epoch.

    Raises:
        InvalidFieldError: ``text`` is not RFC 3339 with an offset, or is before the epoch.
    """
    match = _RFC3339.fullmatch(text)
    if match is None:
        raise InvalidFieldError(f"not RFC 3339 with an offset: {text!r}")
    offset = "+00:00" if match["offset"] == "Z" else match["offset"]
    try:
        moment = datetime.fromisoformat(match["base"] + offset)
    except ValueError as error:
        raise InvalidFieldError(f"not a valid time: {text!r}") from error
    seconds = (moment - _EPOCH) // timedelta(seconds=1)
    nanos = int((match["fraction"] or "").ljust(9, "0"))
    ns = seconds * NS_PER_SECOND + nanos
    require_time_ns(ns, "time")
    return ns
