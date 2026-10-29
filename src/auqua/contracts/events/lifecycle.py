# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Instrument lifecycle events, aligned with the CDM event vocabulary (ADR 0017).

Each type names its CDM reference (primitive instruction and intent). These are
instrument-level facts; their effect on positions and cash is computed by BP6.
"""

import uuid
from decimal import Decimal
from enum import StrEnum

from auqua.contracts.errors import InvalidFieldError, UpcastRefusedError
from auqua.contracts.events.base import Event
from auqua.contracts.identity import InstrumentId
from auqua.contracts.instruments import CURRENCY_CODE, Settlement
from auqua.contracts.validation import (
    require_decimals,
    require_match,
    require_non_negative,
    require_positive,
    require_positive_int,
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
        _require_terms(self)


def _require_terms(action: CorporateAction | CorporateActionV2) -> None:
    """The terms each kind requires and forbids (both versions)."""
    require_time_ns(action.effective_ns, "effective_ns")
    require_decimals(ratio=action.ratio, cash_amount=action.cash_amount)
    kind = action.kind
    needs_ratio = kind in {CorporateActionKind.SPLIT, CorporateActionKind.STOCK_DIVIDEND}
    needs_cash = kind is CorporateActionKind.CASH_DIVIDEND
    needs_new_id = kind is CorporateActionKind.SYMBOL_CHANGE
    if needs_ratio != (action.ratio is not None):
        raise InvalidFieldError(f"{kind} {'needs' if needs_ratio else 'forbids'} ratio")
    if needs_cash != (action.cash_amount is not None and action.currency is not None):
        raise InvalidFieldError(f"{kind} {'needs' if needs_cash else 'forbids'} cash terms")
    if not needs_cash and action.currency is not None:
        raise InvalidFieldError(f"{kind} forbids currency")
    if needs_new_id != (action.new_instrument_id is not None):
        raise InvalidFieldError(
            f"{kind} {'needs' if needs_new_id else 'forbids'} new_instrument_id"
        )
    if action.ratio is not None:
        require_positive(action.ratio, "ratio")
    if action.cash_amount is not None:
        require_positive(action.cash_amount, "cash_amount")
    if action.currency is not None:
        require_match(action.currency, CURRENCY_CODE, "currency")


class CorporateActionV2(Event, frozen=True, tag="lifecycle.corporate_action.v2"):
    """Corporate action with the three dates of a cash dividend (BP6 C1).

    As version 1, plus ``ex_date_ns``, ``record_date_ns`` and ``pay_date_ns``:
    each the start of that date in the listing venue's time zone, as UTC
    nanoseconds, set by the producer. They are required for a cash dividend and
    forbidden for the other kinds. Entitlement is fixed at the ex-date (a
    purchase on or after it does not receive the dividend), so for a cash
    dividend ``effective_ns`` equals ``ex_date_ns``; for the other kinds
    ``effective_ns`` keeps its version-1 meaning, which for a split or a stock
    dividend is the start of its ex-date, the first day trading at the
    adjusted price (BP6 amendment 2). The dates are not ordered
    here: for a distribution of 25 percent or more the ex-date follows the
    payable date (FINRA Rule 11140(b)(2)).
    """

    instrument_id: InstrumentId
    kind: CorporateActionKind
    effective_ns: int
    ratio: Decimal | None = None
    cash_amount: Decimal | None = None
    currency: str | None = None
    new_instrument_id: InstrumentId | None = None
    ex_date_ns: int | None = None
    record_date_ns: int | None = None
    pay_date_ns: int | None = None

    def __post_init__(self) -> None:
        """Validate the terms and the dividend dates."""
        super().__post_init__()
        _require_terms(self)
        dates = (self.ex_date_ns, self.record_date_ns, self.pay_date_ns)
        if self.kind is not CorporateActionKind.CASH_DIVIDEND:
            if any(d is not None for d in dates):
                raise InvalidFieldError(f"{self.kind} forbids dividend dates")
            return
        if self.ex_date_ns is None or self.record_date_ns is None or self.pay_date_ns is None:
            raise InvalidFieldError("cash_dividend needs ex, record and pay dates")
        require_time_ns(self.ex_date_ns, "ex_date_ns")
        require_time_ns(self.record_date_ns, "record_date_ns")
        require_time_ns(self.pay_date_ns, "pay_date_ns")
        if self.effective_ns != self.ex_date_ns:
            raise InvalidFieldError("cash_dividend effective_ns must equal ex_date_ns")


def upcast_corporate_action(event: CorporateAction) -> CorporateActionV2:
    """``lifecycle.corporate_action.v1`` → v2; a v1 cash dividend is refused.

    Raises:
        UpcastRefusedError: a cash dividend: its ex, record and pay dates are
            not in version 1 and cannot be derived.
    """
    if event.kind is CorporateActionKind.CASH_DIVIDEND:
        raise UpcastRefusedError(
            f"lifecycle.corporate_action.v1 cash_dividend {event.id}"
            " has no ex, record and pay dates"
        )
    return CorporateActionV2(
        id=event.id,
        source=event.source,
        ts_event_ns=event.ts_event_ns,
        ts_ingest_ns=event.ts_ingest_ns,
        instrument_id=event.instrument_id,
        kind=event.kind,
        effective_ns=event.effective_ns,
        ratio=event.ratio,
        cash_amount=event.cash_amount,
        currency=event.currency,
        new_instrument_id=event.new_instrument_id,
    )


class FuturesExpiry(Event, frozen=True, tag="lifecycle.futures_expiry.v1"):
    """Final settlement of a future (CDM: ``transfer``, cash or deliverable)."""

    instrument_id: InstrumentId
    settlement: Settlement
    settlement_price: Decimal

    def __post_init__(self) -> None:
        """Validate the settlement price."""
        super().__post_init__()
        require_decimals(settlement_price=self.settlement_price)


class OptionExercise(Event, frozen=True, tag="lifecycle.option_exercise.v1", omit_defaults=True):
    """Exercise of option contracts by the holder (CDM: ``exercise``; intent ``OptionExercise``).

    ``exercise_settlement_value`` is the value S of a cash-settled option's
    underlying interest that sets the cash settlement amount (OCC
    *Characteristics and Risks of Standardized Options*, ch. II); it is absent
    for physical settlement. Added within version 1 under Rule W (BP6
    amendment 2): absent, it is not encoded, so stored events keep their bytes.
    """

    instrument_id: InstrumentId
    quantity: Decimal
    exercise_settlement_value: Decimal | None = None

    def __post_init__(self) -> None:
        """Validate the exercised quantity and the settlement value."""
        super().__post_init__()
        require_positive(self.quantity, "quantity")
        require_decimals(exercise_settlement_value=self.exercise_settlement_value)


class OptionAssignment(
    Event, frozen=True, tag="lifecycle.option_assignment.v1", omit_defaults=True
):
    """Assignment to the writer: the counterparty side of an exercise (CDM: ``exercise``).

    ``exercise_settlement_value`` as for :class:`OptionExercise` (Rule W).
    """

    instrument_id: InstrumentId
    quantity: Decimal
    exercise_settlement_value: Decimal | None = None

    def __post_init__(self) -> None:
        """Validate the assigned quantity and the settlement value."""
        super().__post_init__()
        require_positive(self.quantity, "quantity")
        require_decimals(exercise_settlement_value=self.exercise_settlement_value)


class OptionExpiry(Event, frozen=True, tag="lifecycle.option_expiry.v1"):
    """Expiry of option contracts; ``underlying_price`` decides moneyness (CDM: ``exercise``)."""

    instrument_id: InstrumentId
    underlying_price: Decimal

    def __post_init__(self) -> None:
        """Validate the underlying price."""
        super().__post_init__()
        require_decimals(underlying_price=self.underlying_price)


class FxSettlement(Event, frozen=True, tag="lifecycle.fx_settlement.v1", omit_defaults=True):
    """Exchange of the two currency amounts on the value date (CDM: ``transfer``).

    ``causation_id`` is the id of the fill being settled, when the source
    gives it: the common trade reference that avoids cross-matching (BP6
    amendment 3). Added within version 1 under Rule W: absent, it is not
    encoded, so stored events keep their bytes.
    """

    instrument_id: InstrumentId
    value_date_ns: int
    base_amount: Decimal
    quote_amount: Decimal
    causation_id: uuid.UUID | None = None

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


class FundingPaymentV2(Event, frozen=True, tag="lifecycle.funding_payment.v2"):
    """Perpetual-swap funding for the period ``interval_ns`` ending at ``ts_event_ns`` (BP6 D3).

    Positive ``funding_rate``: longs pay shorts. ``amount``, when present, is
    the payment the venue reports, signed (positive = received) and in the
    instrument's settlement currency; it is posted as reported (BP6 E2).
    """

    instrument_id: InstrumentId
    funding_rate: Decimal
    mark_price: Decimal
    interval_ns: int
    amount: Decimal | None = None

    def __post_init__(self) -> None:
        """Validate the rate, mark price, period and reported amount."""
        super().__post_init__()
        require_decimals(funding_rate=self.funding_rate, amount=self.amount)
        require_positive(self.mark_price, "mark_price")
        require_positive_int(self.interval_ns, "interval_ns")


def upcast_funding_payment(event: FundingPayment) -> FundingPaymentV2:
    """``lifecycle.funding_payment.v1`` has no period: it cannot be upcast (BP6 D3).

    Raises:
        UpcastRefusedError: always.
    """
    raise UpcastRefusedError(
        f"lifecycle.funding_payment.v1 {event.id} has no funding period (interval_ns)"
    )


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
