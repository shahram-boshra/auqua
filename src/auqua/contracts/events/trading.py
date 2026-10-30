# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Decision and trading events of the walking skeleton, version 1 (ADR 0025).

Every derived event names the event that caused it (``causation_id``), so an
order traces back through target and signal to the market data, and a run
record ties all of them to the data snapshot, configuration and code that
produced them (Strategy section 9). The owning Blueprints (BP5 run, BP6
target, BP7 risk, BP8 order and fill, BP9 signal) extend these types through
new versions with upcasters (ADR 0014).
"""

import uuid
from decimal import Decimal
from enum import StrEnum

from auqua.contracts.errors import InvalidFieldError
from auqua.contracts.events.base import Event
from auqua.contracts.identity import InstrumentId
from auqua.contracts.instruments import CURRENCY_CODE
from auqua.contracts.validation import (
    require_decimals,
    require_match,
    require_non_negative,
    require_positive,
)

_SHA256_HEX = 64


class Side(StrEnum):
    """Order or fill side."""

    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    """Order types of the skeleton (market only)."""

    MARKET = "market"


class RunStarted(Event, frozen=True, tag="research.run.v1"):
    """Identity of one run: what it read, how it was configured, which code ran.

    The event ``id`` is the run id. Hashes are lower-case hex SHA-256;
    ``instrument_sha256`` hashes the JSON encoding of the instrument version
    the run traded.
    """

    input_head_hash: str
    input_next_sequence: int
    instrument_id: InstrumentId
    instrument_sha256: str
    config_id: str
    spec_id: str
    risk_limits_sha256: str
    code_id: str
    code_version: str

    def __post_init__(self) -> None:
        """Validate the hashes and the input position."""
        super().__post_init__()
        for name in (
            "input_head_hash",
            "instrument_sha256",
            "config_id",
            "spec_id",
            "risk_limits_sha256",
            "code_id",
        ):
            value = getattr(self, name)
            if len(value) != _SHA256_HEX or value.strip("0123456789abcdef"):
                raise InvalidFieldError(f"{name} must be 64 lower-case hex digits")
        if self.input_next_sequence < 0:
            raise InvalidFieldError("input_next_sequence must be >= 0")


class Signal(Event, frozen=True, tag="alpha.signal.v1"):
    """Direction proposed by a signal provider for one instrument (-1, 0 or +1)."""

    causation_id: uuid.UUID
    instrument_id: InstrumentId
    direction: int
    provider: str
    provider_version: str

    def __post_init__(self) -> None:
        """Validate the direction."""
        super().__post_init__()
        if self.direction not in {-1, 0, 1}:
            raise InvalidFieldError(f"direction must be -1, 0 or 1, got {self.direction}")


class Target(Event, frozen=True, tag="portfolio.target.v1"):
    """Target position (signed quantity) for one instrument."""

    causation_id: uuid.UUID
    instrument_id: InstrumentId
    quantity: Decimal

    def __post_init__(self) -> None:
        """Validate the quantity."""
        super().__post_init__()
        require_decimals(quantity=self.quantity)


class OrderSubmitted(Event, frozen=True, tag="order.submitted.v1"):
    """An order sent towards the venue; the event id is the order id."""

    causation_id: uuid.UUID
    instrument_id: InstrumentId
    side: Side
    quantity: Decimal
    order_type: OrderType

    def __post_init__(self) -> None:
        """Validate the quantity."""
        super().__post_init__()
        require_positive(self.quantity, "quantity")


class RiskDecision(Event, frozen=True, tag="risk.decision.v1"):
    """Pre-trade risk outcome for one order (``causation_id`` is the order id)."""

    causation_id: uuid.UUID
    instrument_id: InstrumentId
    accepted: bool
    rule: str
    limit: Decimal | None = None
    value: Decimal | None = None

    def __post_init__(self) -> None:
        """A rejection names its rule, limit and value."""
        super().__post_init__()
        require_decimals(limit=self.limit, value=self.value)
        if not self.accepted and (not self.rule or self.limit is None or self.value is None):
            raise InvalidFieldError("a rejection needs rule, limit and value")


class Fill(Event, frozen=True, tag="execution.fill.v1", omit_defaults=True):
    """Execution of an order (``causation_id`` is the order id).

    ``accrued_interest`` is the accrued interest the buyer pays the seller, in
    the quote currency, as the venue reports it (bonds). Added within version 1
    under Rule W (BP6 amendment 2): absent, it is not encoded, so stored fills
    keep their bytes.
    """

    causation_id: uuid.UUID
    instrument_id: InstrumentId
    side: Side
    price: Decimal
    quantity: Decimal
    fee: Decimal
    fee_currency: str
    accrued_interest: Decimal | None = None

    def __post_init__(self) -> None:
        """Validate price, quantity, fee, currency and accrued interest."""
        super().__post_init__()
        require_decimals(price=self.price, accrued_interest=self.accrued_interest)
        require_positive(self.quantity, "quantity")
        require_non_negative(self.fee, "fee")
        require_match(self.fee_currency, CURRENCY_CODE, "fee_currency")


class AllocationUnavailableReason(StrEnum):
    """Why an allocator made no proposal for an instrument (BP6 section 4.9, amendments 2 and 10).

    - ``RATE_UNAVAILABLE``: no usable rate for the quote currency.
    - ``EQUITY_INCOMPLETE``: the account's ``equity_base`` is unavailable.
    - ``VOLATILITY_WARMUP``: fewer daily returns than the warm-up.
    - ``VOLATILITY_INVALID``: sigma_hat zero, non-finite or undefined (a non-positive
      close in the window).
    - ``PRICE_UNAVAILABLE``: no positive latest price.
    - ``EQUITY_NOT_POSITIVE``: ``equity_base`` is zero or negative (amendment
      10, F-W; added within version 1: no stored event used the closed set).
    """

    RATE_UNAVAILABLE = "rate_unavailable"
    EQUITY_INCOMPLETE = "equity_incomplete"
    VOLATILITY_WARMUP = "volatility_warmup"
    VOLATILITY_INVALID = "volatility_invalid"
    PRICE_UNAVAILABLE = "price_unavailable"
    EQUITY_NOT_POSITIVE = "equity_not_positive"


class AllocationUnavailable(Event, frozen=True, tag="portfolio.allocation_unavailable.v1"):
    """No target proposed for one instrument, and why (``causation_id``: the signal)."""

    causation_id: uuid.UUID
    instrument_id: InstrumentId
    reason: AllocationUnavailableReason
