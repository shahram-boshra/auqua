# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Identifiers (ADR 0015).

Events, commands, orders and fills are identified by UUIDv7 values (RFC 9562).
Identifiers never order anything: order is the event log's ``sequence``.
"""

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
