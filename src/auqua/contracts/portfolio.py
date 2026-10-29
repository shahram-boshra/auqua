# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Account state shared by the portfolio, risk and reports (BP6 sections 4.2, 4.4, 4.6, 4.8).

:class:`AccountView` is the only portfolio type the pre-trade gate reads
(import-linter contract 6): risk depends on these frozen values, never on
:mod:`auqua.portfolio`. Every value is exact; nothing here is estimated. A
total that cannot be computed is reported as unavailable (``None``) with the
reason listed, never as zero (BP6 P4, amendment 1).

Rows (balances, positions, margin, conversions) are in strictly increasing key
order, so equal views have equal encodings.
"""

import itertools
import uuid
from decimal import Decimal
from enum import StrEnum

import msgspec

from auqua.contracts.errors import InvalidFieldError
from auqua.contracts.identity import InstrumentId
from auqua.contracts.instruments import CURRENCY_CODE
from auqua.contracts.numeric import exact
from auqua.contracts.validation import (
    require_decimals,
    require_label,
    require_match,
    require_non_negative,
    require_positive,
    require_sha256,
    require_time_ns,
)

#: The account's base currency (BP6 Q-BP6-1, ISO 4217).
BASE_CURRENCY = "USD"


def _require_increasing(keys: list[str], name: str) -> None:
    if any(a >= b for a, b in itertools.pairwise(keys)):
        raise InvalidFieldError(f"{name} must be in strictly increasing key order")


class PostingKind(StrEnum):
    """What moved the balance (BP6 section 4.2)."""

    TRADE = "trade"
    FEE = "fee"
    TRANSFER = "transfer"
    VARIATION = "variation"
    PREMIUM = "premium"
    EXERCISE = "exercise"
    SETTLEMENT = "settlement"
    FUNDING = "funding"
    COUPON = "coupon"
    DIVIDEND = "dividend"
    RECEIVABLE = "receivable"
    REALIZED = "realized"
    ACCRUED = "accrued"


class Posting(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """One movement of one currency; a balance is the sum of its postings.

    ``cause_id`` is the id of the event that caused it. ``receivable``
    postings form the receivable balance (a dividend fixed and not yet paid),
    every other kind the cash balance. ``accrued`` is a bond fill's reported
    accrued interest (buyer negative, seller positive), never part of a lot's
    cost (BP6 amendment 2).
    """

    account: str
    currency: str
    amount: Decimal
    kind: PostingKind
    cause_id: uuid.UUID

    def __post_init__(self) -> None:
        """Validate the account, the currency and a non-zero amount."""
        require_label(self.account, "account")
        require_match(self.currency, CURRENCY_CODE, "currency")
        require_decimals(amount=self.amount)
        if self.amount.is_zero():
            raise InvalidFieldError("a posting moves a non-zero amount")


class LogPosition(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """The log position a view reflects: the next sequence and the hash of the last record."""

    next_sequence: int
    hash: str

    def __post_init__(self) -> None:
        """Validate the sequence and the hash."""
        if self.next_sequence < 0:
            raise InvalidFieldError(f"next_sequence must be >= 0, got {self.next_sequence}")
        require_sha256(self.hash, "hash")


class Balance(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Cash and receivable of one currency (BP6 section 4.2)."""

    currency: str
    cash: Decimal
    receivable: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        """Validate the currency and both amounts."""
        require_match(self.currency, CURRENCY_CODE, "currency")
        require_decimals(cash=self.cash)
        require_non_negative(self.receivable, "receivable")


class PositionView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """One instrument's position (BP6 section 4.8, C7).

    ``quantity`` is the filled position and ``working`` the accepted, unfilled
    orders, both signed and kept apart: decisions and the pre-trade check use
    ``quantity + working`` (BP5 section 12, L7). ``mark`` is the latest
    available mark and ``unrealized`` the profit or loss of ``quantity`` at
    it, in ``currency``; an unmarked position has neither.
    """

    instrument_id: InstrumentId
    currency: str
    quantity: Decimal
    working: Decimal = Decimal(0)
    mark: Decimal | None = None
    mark_available_ns: int | None = None
    unrealized: Decimal | None = None

    def __post_init__(self) -> None:
        """Validate the quantities and that a mark comes with its time and P&L."""
        require_match(self.currency, CURRENCY_CODE, "currency")
        require_decimals(
            quantity=self.quantity, working=self.working, mark=self.mark, unrealized=self.unrealized
        )
        marked = (self.mark is not None, self.mark_available_ns is not None)
        if marked[0] != marked[1] or marked[0] != (self.unrealized is not None):
            raise InvalidFieldError("mark, mark_available_ns and unrealized come together")
        if self.mark_available_ns is not None:
            require_time_ns(self.mark_available_ns, "mark_available_ns")

    @property
    def exposure_quantity(self) -> Decimal:
        """``quantity + working``: what decisions and the pre-trade check count."""
        with exact():
            return self.quantity + self.working


class MarginView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Margin of one currency (BP6 section 4.6).

    The requirement is (``initial``, ``maintenance``); the margin in use is
    ``initial``; ``available`` = ``equity`` - ``initial`` and ``deficit`` =
    max(0, ``maintenance`` - ``equity``). All are exact and never rounded.
    """

    currency: str
    initial: Decimal
    maintenance: Decimal
    equity: Decimal
    available: Decimal
    deficit: Decimal

    def __post_init__(self) -> None:
        """Validate the currency and the consistency of the derived values."""
        require_match(self.currency, CURRENCY_CODE, "currency")
        require_non_negative(self.initial, "initial")
        require_non_negative(self.maintenance, "maintenance")
        require_decimals(equity=self.equity, available=self.available)
        require_non_negative(self.deficit, "deficit")
        with exact():
            available = self.equity - self.initial
            deficit = max(Decimal(0), self.maintenance - self.equity)
        if self.available != available:
            raise InvalidFieldError("available must equal equity - initial")
        if self.deficit != deficit:
            raise InvalidFieldError("deficit must equal max(0, maintenance - equity)")


class Conversion(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """One conversion into the base currency (BP6 section 4.4).

    ``unit`` is the rate's published quote convention (for example
    ``"EUR per USD"``), which decides whether the amount is divided or
    multiplied; ``value`` is rounded once to the base currency's minor units.
    """

    amount: Decimal
    from_currency: str
    to_currency: str
    rate: Decimal
    unit: str
    source: str
    rate_available_ns: int
    value: Decimal

    def __post_init__(self) -> None:
        """Validate the currencies, the rate, its unit, source and time."""
        require_decimals(amount=self.amount, value=self.value)
        require_match(self.from_currency, CURRENCY_CODE, "from_currency")
        require_match(self.to_currency, CURRENCY_CODE, "to_currency")
        if self.from_currency == self.to_currency:
            raise InvalidFieldError("a conversion needs two currencies")
        require_positive(self.rate, "rate")
        require_label(self.unit, "unit")
        require_label(self.source, "source")
        require_time_ns(self.rate_available_ns, "rate_available_ns")


class AccountView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """The account at ``as_of_ns`` as of ``log_position`` (BP6 section 4.8).

    ``equity_base`` is the equity in :data:`BASE_CURRENCY`, or ``None`` when it
    cannot be computed: ``unconverted`` lists the currencies without a rate and
    ``unmarked`` the positions without a mark whose value enters equity (BP6
    amendment 7). ``unmargined`` lists positions in margin scope without a
    usable requirement: margin is incomplete while it is not empty (amendment 1).
    """

    account_id: str
    as_of_ns: int
    log_position: LogPosition
    equity_base: Decimal | None
    balances: tuple[Balance, ...] = ()
    positions: tuple[PositionView, ...] = ()
    margin: tuple[MarginView, ...] = ()
    unmargined: tuple[InstrumentId, ...] = ()
    conversions: tuple[Conversion, ...] = ()
    unconverted: tuple[str, ...] = ()
    unmarked: tuple[InstrumentId, ...] = ()

    def __post_init__(self) -> None:
        """Validate the time, the row order and the completeness markers."""
        require_label(self.account_id, "account_id")
        require_time_ns(self.as_of_ns, "as_of_ns")
        require_decimals(equity_base=self.equity_base)
        _require_increasing([b.currency for b in self.balances], "balances")
        _require_increasing([str(p.instrument_id) for p in self.positions], "positions")
        _require_increasing([m.currency for m in self.margin], "margin")
        _require_increasing([str(i) for i in self.unmargined], "unmargined")
        _require_increasing([c.from_currency for c in self.conversions], "conversions")
        _require_increasing(list(self.unconverted), "unconverted")
        _require_increasing([str(i) for i in self.unmarked], "unmarked")
        for code in self.unconverted:
            require_match(code, CURRENCY_CODE, "unconverted")
        if any(c.to_currency != BASE_CURRENCY for c in self.conversions):
            raise InvalidFieldError(f"conversions must be into {BASE_CURRENCY}")
        if (self.equity_base is None) != bool(self.unconverted or self.unmarked):
            raise InvalidFieldError(
                "equity_base is unavailable exactly when a currency is unconverted "
                "or a position unmarked"
            )

    @property
    def margin_complete(self) -> bool:
        """``True`` when every position has a margin requirement."""
        return not self.unmargined
