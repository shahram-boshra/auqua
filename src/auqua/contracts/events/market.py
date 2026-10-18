# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Market-data events (BP1 section 4.4).

Prices may be negative (some futures and spreads trade below zero); sizes and
volumes may not. Values are quantized to the instrument grid at ingestion (BP4).
"""

from decimal import Decimal
from enum import StrEnum

from auqua.contracts.errors import InvalidFieldError
from auqua.contracts.events.base import Event, require_decimals
from auqua.contracts.identity import InstrumentId
from auqua.contracts.validation import (
    require_non_negative,
    require_positive,
    require_positive_int,
    require_time_ns,
)


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
    """

    instrument_id: InstrumentId
    start_ns: int
    interval_ns: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def __post_init__(self) -> None:
        """Validate the interval, the price range and the volume."""
        super().__post_init__()
        require_time_ns(self.start_ns, "start_ns")
        require_positive_int(self.interval_ns, "interval_ns")
        if self.ts_event_ns != self.start_ns + self.interval_ns:
            raise InvalidFieldError("ts_event_ns must equal start_ns + interval_ns")
        require_decimals(open=self.open, high=self.high, low=self.low, close=self.close)
        require_non_negative(self.volume, "volume")
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise InvalidFieldError("bar prices must satisfy low <= open, close <= high")
