# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Instrument lifecycle events, aligned with the CDM event vocabulary (ADR 0017).

Each type names its CDM reference (primitive instruction and intent). These are
instrument-level facts; their effect on positions and cash is computed by BP6.
"""

from decimal import Decimal
from enum import StrEnum

from auqua.contracts.errors import InvalidFieldError
from auqua.contracts.events.base import Event, require_decimals
from auqua.contracts.identity import InstrumentId
from auqua.contracts.instruments import CURRENCY_CODE, Settlement
from auqua.contracts.validation import (
    require_match,
    require_non_negative,
    require_positive,
    require_time_ns,
)


class CorporateActionKind(StrEnum):
    """Corporate actions handled in phase 1."""

    SPLIT = "split"
    CASH_DIVIDEND = "cash_dividend"
    STOCK_DIVIDEND = "stock_dividend"
    SYMBOL_CHANGE = "symbol_change"
    DELISTING = "delisting"


class CorporateAction(Event, frozen=True, tag="lifecycle.corporate_action.v1"):
    """Corporate action.

    CDM reference: ``reset`` / ``split`` / ``termsChange``; intent
    ``CorporateActionAdjustment``. ``ts_event_ns`` is the announcement;
    ``effective_ns`` is when it applies. ``ratio`` is new units per old unit
    (split: 2 for 2-for-1; reverse split: 0.5; stock dividend: additional units
    per unit held).
    """

    instrument_id: InstrumentId
    kind: CorporateActionKind
    effective_ns: int
    ratio: Decimal | None = None
    cash_amount: Decimal | None = None
    currency: str | None = None
    new_instrument_id: InstrumentId | None = None

    def __post_init__(self) -> None:
        """Validate the fields each kind requires and forbids."""
        super().__post_init__()
        require_time_ns(self.effective_ns, "effective_ns")
        require_decimals(ratio=self.ratio, cash_amount=self.cash_amount)
        needs_ratio = self.kind in {CorporateActionKind.SPLIT, CorporateActionKind.STOCK_DIVIDEND}
        needs_cash = self.kind is CorporateActionKind.CASH_DIVIDEND
        needs_new_id = self.kind is CorporateActionKind.SYMBOL_CHANGE
        if needs_ratio != (self.ratio is not None):
            raise InvalidFieldError(f"{self.kind} {'needs' if needs_ratio else 'forbids'} ratio")
        if needs_cash != (self.cash_amount is not None and self.currency is not None):
            raise InvalidFieldError(
                f"{self.kind} {'needs' if needs_cash else 'forbids'} cash terms"
            )
        if not needs_cash and self.currency is not None:
            raise InvalidFieldError(f"{self.kind} forbids currency")
        if needs_new_id != (self.new_instrument_id is not None):
            raise InvalidFieldError(
                f"{self.kind} {'needs' if needs_new_id else 'forbids'} new_instrument_id"
            )
        if self.ratio is not None:
            require_positive(self.ratio, "ratio")
        if self.cash_amount is not None:
            require_positive(self.cash_amount, "cash_amount")
        if self.currency is not None:
            require_match(self.currency, CURRENCY_CODE, "currency")


class FuturesExpiry(Event, frozen=True, tag="lifecycle.futures_expiry.v1"):
    """Final settlement of a future (CDM: ``transfer``, cash or deliverable)."""

    instrument_id: InstrumentId
    settlement: Settlement
    settlement_price: Decimal

    def __post_init__(self) -> None:
        """Validate the settlement price."""
        super().__post_init__()
        require_decimals(settlement_price=self.settlement_price)


class OptionExercise(Event, frozen=True, tag="lifecycle.option_exercise.v1"):
    """Exercise of option contracts by the holder (CDM: ``exercise``; intent ``OptionExercise``)."""

    instrument_id: InstrumentId
    quantity: Decimal

    def __post_init__(self) -> None:
        """Validate the exercised quantity."""
        super().__post_init__()
        require_positive(self.quantity, "quantity")


class OptionAssignment(Event, frozen=True, tag="lifecycle.option_assignment.v1"):
    """Assignment to the writer: the counterparty side of an exercise (CDM: ``exercise``)."""

    instrument_id: InstrumentId
    quantity: Decimal

    def __post_init__(self) -> None:
        """Validate the assigned quantity."""
        super().__post_init__()
        require_positive(self.quantity, "quantity")


class OptionExpiry(Event, frozen=True, tag="lifecycle.option_expiry.v1"):
    """Expiry of option contracts; ``underlying_price`` decides moneyness (CDM: ``exercise``)."""

    instrument_id: InstrumentId
    underlying_price: Decimal

    def __post_init__(self) -> None:
        """Validate the underlying price."""
        super().__post_init__()
        require_decimals(underlying_price=self.underlying_price)


class FxSettlement(Event, frozen=True, tag="lifecycle.fx_settlement.v1"):
    """Exchange of the two currency amounts on the value date (CDM: ``transfer``)."""

    instrument_id: InstrumentId
    value_date_ns: int
    base_amount: Decimal
    quote_amount: Decimal

    def __post_init__(self) -> None:
        """Validate the value date and amounts."""
        super().__post_init__()
        require_time_ns(self.value_date_ns, "value_date_ns")
        require_positive(self.base_amount, "base_amount")
        require_positive(self.quote_amount, "quote_amount")


class FundingPayment(Event, frozen=True, tag="lifecycle.funding_payment.v1"):
    """Perpetual-swap funding at ``ts_event_ns`` (CDM: ``transfer``; intent ``CashFlow``).

    Positive ``funding_rate``: longs pay shorts.
    """

    instrument_id: InstrumentId
    funding_rate: Decimal
    mark_price: Decimal

    def __post_init__(self) -> None:
        """Validate the rate and mark price."""
        super().__post_init__()
        require_decimals(funding_rate=self.funding_rate)
        require_positive(self.mark_price, "mark_price")


class CouponPayment(Event, frozen=True, tag="lifecycle.coupon_payment.v1"):
    """Bond coupon paid per unit of face value held (CDM: ``transfer``; intent ``CashFlow``)."""

    instrument_id: InstrumentId
    payment_ns: int
    amount_per_unit: Decimal
    currency: str

    def __post_init__(self) -> None:
        """Validate the payment date, amount and currency."""
        super().__post_init__()
        require_time_ns(self.payment_ns, "payment_ns")
        require_non_negative(self.amount_per_unit, "amount_per_unit")
        require_match(self.currency, CURRENCY_CODE, "currency")
