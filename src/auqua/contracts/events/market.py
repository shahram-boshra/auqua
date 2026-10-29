# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Market-data events (BP1 section 4.4).

Prices may be negative (some futures and spreads trade below zero); sizes and
volumes may not. Values are quantized to the instrument grid at ingestion (BP4).

Ingested records carry a ``revision`` (BP4 section 4.3): a provider's later
correction of a record is stored as the next revision under a new id, never as
an edit, and readers choose the revision by knowledge time. A record is
available for point-in-time use at ``ts_published_ns`` when the source states
a publication time, otherwise at ``ts_event_ns`` (BP4 section 4.6).
"""

import datetime
from decimal import Decimal
from enum import StrEnum

from auqua.contracts.errors import InvalidFieldError
from auqua.contracts.events.base import Event, require_decimals
from auqua.contracts.identity import InstrumentId, SeriesId
from auqua.contracts.validation import (
    require_non_negative,
    require_positive,
    require_positive_int,
    require_time_ns,
)


def _require_revision(revision: int) -> None:
    if revision < 0:
        raise InvalidFieldError(f"revision must be >= 0, got {revision}")


def _require_published(ts_published_ns: int, ts_event_ns: int) -> None:
    require_time_ns(ts_published_ns, "ts_published_ns")
    if ts_published_ns < ts_event_ns:
        raise InvalidFieldError("ts_published_ns must not precede ts_event_ns")


class Aggressor(StrEnum):
    """Side that initiated a trade, when the venue reports it."""

    BUY = "buy"
    SELL = "sell"


class Trade(Event, frozen=True, tag="market.trade.v1"):
    """One trade print."""

    instrument_id: InstrumentId
    price: Decimal
    quantity: Decimal
    aggressor: Aggressor | None = None
    venue_trade_id: str | None = None

    def __post_init__(self) -> None:
        """Validate price and quantity."""
        super().__post_init__()
        require_decimals(price=self.price)
        require_positive(self.quantity, "quantity")


class Quote(Event, frozen=True, tag="market.quote.v1"):
    """Best bid and offer."""

    instrument_id: InstrumentId
    bid_price: Decimal
    bid_size: Decimal
    ask_price: Decimal
    ask_size: Decimal

    def __post_init__(self) -> None:
        """Validate prices, sizes and the bid/ask order."""
        super().__post_init__()
        require_decimals(bid_price=self.bid_price, ask_price=self.ask_price)
        require_non_negative(self.bid_size, "bid_size")
        require_non_negative(self.ask_size, "ask_size")
        if self.bid_price > self.ask_price:
            raise InvalidFieldError(f"bid {self.bid_price} above ask {self.ask_price}")


class Bar(Event, frozen=True, tag="market.bar.v1"):
    """Aggregated bar over ``[start_ns, start_ns + interval_ns)``.

    ``ts_event_ns`` is the end of the interval, when the bar became complete.
    ``revision`` and ``ts_published_ns`` follow the module rules.
    """

    instrument_id: InstrumentId
    start_ns: int
    interval_ns: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    revision: int = 0
    ts_published_ns: int | None = None

    def __post_init__(self) -> None:
        """Validate the interval, the price range, the volume and the revision."""
        super().__post_init__()
        _require_revision(self.revision)
        if self.ts_published_ns is not None:
            _require_published(self.ts_published_ns, self.ts_event_ns)
        require_time_ns(self.start_ns, "start_ns")
        require_positive_int(self.interval_ns, "interval_ns")
        if self.ts_event_ns != self.start_ns + self.interval_ns:
            raise InvalidFieldError("ts_event_ns must equal start_ns + interval_ns")
        require_decimals(open=self.open, high=self.high, low=self.low, close=self.close)
        require_non_negative(self.volume, "volume")
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise InvalidFieldError("bar prices must satisfy low <= open, close <= high")


class ReferenceRate(Event, frozen=True, tag="market.reference_rate.v1"):
    """One published observation of a reference series (BP4 section 4.4).

    ``ts_event_ns`` is the observation time stated by the publisher (e.g. noon
    in New York for the H.10 rates); ``ts_published_ns`` is when the value
    became available (required: every reference source states or implies a
    publication time). ``unit`` states the quote convention as published, e.g.
    ``USD per EUR`` or ``percent``; the value keeps the published digits.
    """

    series_id: SeriesId
    value: Decimal
    unit: str
    ts_published_ns: int
    revision: int = 0

    def __post_init__(self) -> None:
        """Validate the value, the unit, the publication time and the revision."""
        super().__post_init__()
        require_decimals(value=self.value)
        if not self.unit.strip() or self.unit != self.unit.strip():
            raise InvalidFieldError(f"unit {self.unit!r} must be non-empty and trimmed")
        _require_published(self.ts_published_ns, self.ts_event_ns)
        _require_revision(self.revision)


class SettlementPrice(Event, frozen=True, tag="market.settlement_price.v1"):
    """Official daily settlement price of a future for one trading date (BP6 section 4.1).

    ``ts_published_ns`` is when the exchange published it: its availability
    for point-in-time use (ADR 0028).
    """

    instrument_id: InstrumentId
    trading_date: datetime.date
    price: Decimal
    ts_published_ns: int
    revision: int = 0

    def __post_init__(self) -> None:
        """Validate the price, the publication time and the revision."""
        super().__post_init__()
        require_decimals(price=self.price)
        _require_published(self.ts_published_ns, self.ts_event_ns)
        _require_revision(self.revision)
