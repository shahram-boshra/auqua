# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Identifiers (ADR 0015).

Events, commands, orders and fills are identified by UUIDv7 values (RFC 9562).
Identifiers never order anything: order is the event log's ``sequence``.
Identifiers that must be reproducible (ingested records without a source id,
events derived by a run) are name-based UUIDv8 values (:func:`derived_id`;
ADR 0025).
"""

import hashlib
import re
import uuid

import msgspec

from auqua.contracts.errors import InvalidFieldError
from auqua.contracts.validation import require_match

#: Producer names: lower-case path segments, e.g. ``ingest/binance/spot``.
SOURCE_PATTERN = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*")
#: Venue codes, e.g. ``BINANCE``, ``XNAS``.
VENUE_PATTERN = re.compile(r"[A-Z0-9][A-Z0-9_]*")
#: Venue symbols: printable, no whitespace and no ``:`` (the text separator).
SYMBOL_PATTERN = re.compile(r"[^\s:]+")
_SEPARATOR = ":"


def new_id() -> uuid.UUID:
    """A new UUIDv7 (time-ordered; monotonic within a millisecond)."""
    return uuid.uuid7()


_UUID_BYTES = 16
_LENGTH_BYTES = 4


def derived_id(namespace: uuid.UUID, *parts: bytes) -> uuid.UUID:
    """A deterministic name-based UUIDv8 from SHA-256 (RFC 9562 sections 5.5 and 5.8).

    The name is the parts, each prefixed with its 4-byte big-endian length so
    that different part lists never produce the same name. As for UUIDv5, the
    hash covers the namespace followed by the name and the leftmost 128 bits
    are kept; SHA-256 replaces SHA-1, so the value lies in the UUIDv8 space
    (section 5.5) with the version and variant bits set (section 5.8). Equal
    inputs always give equal identifiers (section 6.5).
    """
    digest = hashlib.sha256(namespace.bytes)
    for part in parts:
        digest.update(len(part).to_bytes(_LENGTH_BYTES, "big"))
        digest.update(part)
    raw = bytearray(digest.digest()[:_UUID_BYTES])
    raw[6] = (raw[6] & 0x0F) | 0x80  # version 8
    raw[8] = (raw[8] & 0x3F) | 0x80  # variant 0b10
    return uuid.UUID(bytes=bytes(raw))


def require_source(source: str) -> None:
    """Refuse producer names outside :data:`SOURCE_PATTERN`."""
    require_match(source, SOURCE_PATTERN, "source")


class InstrumentId(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Stable internal instrument identifier: a venue and the venue's symbol.

    A symbol change creates a new identifier linked by a lifecycle event; an
    identifier is never renamed in place.
    """

    venue: str
    symbol: str

    def __post_init__(self) -> None:
        """Validate the venue code and symbol."""
        require_match(self.venue, VENUE_PATTERN, "venue")
        require_match(self.symbol, SYMBOL_PATTERN, "symbol")

    def __str__(self) -> str:
        """Canonical text form ``VENUE:SYMBOL``."""
        return f"{self.venue}{_SEPARATOR}{self.symbol}"

    @classmethod
    def parse(cls, text: str) -> InstrumentId:
        """Parse the canonical text form ``VENUE:SYMBOL``.

        Raises:
            InvalidFieldError: ``text`` is not in canonical form.
        """
        venue, separator, symbol = text.partition(_SEPARATOR)
        if not separator:
            raise InvalidFieldError(f"instrument id {text!r} has no {_SEPARATOR!r}")
        return cls(venue, symbol)


class SeriesId(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Identifier of a published reference series (BP4 section 4): a publisher and its code.

    Reference rates (central-bank exchange rates, par yields) are observations
    of a series, not tradable instruments. Publisher codes follow
    :data:`VENUE_PATTERN` and series codes :data:`SYMBOL_PATTERN`, e.g.
    ``FRB:H10/RXI$US_N.B.EU``.
    """

    publisher: str
    code: str

    def __post_init__(self) -> None:
        """Validate the publisher and series codes."""
        require_match(self.publisher, VENUE_PATTERN, "publisher")
        require_match(self.code, SYMBOL_PATTERN, "code")

    def __str__(self) -> str:
        """Canonical text form ``PUBLISHER:CODE``."""
        return f"{self.publisher}{_SEPARATOR}{self.code}"

    @classmethod
    def parse(cls, text: str) -> SeriesId:
        """Parse the canonical text form ``PUBLISHER:CODE``.

        Raises:
            InvalidFieldError: ``text`` is not in canonical form.
        """
        publisher, separator, code = text.partition(_SEPARATOR)
        if not separator:
            raise InvalidFieldError(f"series id {text!r} has no {_SEPARATOR!r}")
        return cls(publisher, code)
