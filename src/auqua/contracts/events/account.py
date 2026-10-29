# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Account events (BP6 sections 4.1 and 4.7).

A cash transfer moves money into or out of the account. A venue statement
reports balances and positions as the venue holds them; a reconciliation
result compares one statement with the account the platform computed and is
keyed by the statement's id and the SHA-256 of its canonical encoding, so the
same statement yields one result. Statement rows are in strictly increasing
key order, which makes that encoding canonical.
"""

import itertools
import uuid
from decimal import Decimal
from enum import StrEnum

import msgspec

from auqua.contracts.errors import InvalidFieldError
from auqua.contracts.events.base import Event, require_decimals
from auqua.contracts.identity import InstrumentId
from auqua.contracts.instruments import CURRENCY_CODE
from auqua.contracts.validation import (
    require_label,
    require_match,
    require_sha256,
    require_time_ns,
)


class CashTransfer(Event, frozen=True, tag="account.transfer.v1"):
    """Deposit (positive ``amount``) or withdrawal (negative) of one currency."""

    currency: str
    amount: Decimal
    reference: str

    def __post_init__(self) -> None:
        """Validate the currency, a non-zero amount and the reference."""
        super().__post_init__()
        require_match(self.currency, CURRENCY_CODE, "currency")
        require_decimals(amount=self.amount)
        if self.amount.is_zero():
            raise InvalidFieldError("amount must not be zero")
        require_label(self.reference, "reference")


class StatementBalance(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """One currency's balance as the venue reports it."""

    currency: str
    amount: Decimal

    def __post_init__(self) -> None:
        """Validate the currency and the amount."""
        require_match(self.currency, CURRENCY_CODE, "currency")
        require_decimals(amount=self.amount)


class StatementPosition(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """One instrument's signed position as the venue reports it."""

    instrument_id: InstrumentId
    quantity: Decimal

    def __post_init__(self) -> None:
        """Validate the quantity."""
        require_decimals(quantity=self.quantity)


def _require_increasing(keys: list[str], name: str) -> None:
    if any(a >= b for a, b in itertools.pairwise(keys)):
        raise InvalidFieldError(f"{name} must be in strictly increasing key order")


class AccountStatement(Event, frozen=True, tag="account.statement.v1"):
    """Balances and positions of the account as the venue reports them at ``as_of_ns``."""

    statement_id: str
    as_of_ns: int
    balances: tuple[StatementBalance, ...]
    positions: tuple[StatementPosition, ...] = ()

    def __post_init__(self) -> None:
        """Validate the id, the time and the canonical row order."""
        super().__post_init__()
        require_label(self.statement_id, "statement_id")
        require_time_ns(self.as_of_ns, "as_of_ns")
        _require_increasing([b.currency for b in self.balances], "balances")
        _require_increasing([str(p.instrument_id) for p in self.positions], "positions")


class ReconciliationStatus(StrEnum):
    """Outcome of comparing a statement with the account."""

    MATCHED = "matched"
    BREAK = "break"


class Difference(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """One field whose value differs; ``None`` means absent on that side."""

    field: str
    expected: Decimal | None
    reported: Decimal | None

    def __post_init__(self) -> None:
        """Validate the field name and that the two sides differ."""
        require_label(self.field, "field")
        require_decimals(expected=self.expected, reported=self.reported)
        if self.expected == self.reported:
            raise InvalidFieldError(f"{self.field}: expected and reported do not differ")


class ReconciliationResult(Event, frozen=True, tag="portfolio.reconciliation.v1"):
    """Result of reconciling one statement (BP6 section 4.7).

    ``resolves`` names the id of an earlier ``break`` result that this
    ``matched`` result closes.
    """

    statement_id: str
    statement_sha256: str
    status: ReconciliationStatus
    differences: tuple[Difference, ...] = ()
    resolves: uuid.UUID | None = None

    def __post_init__(self) -> None:
        """Validate the key, and that differences exist exactly for a break."""
        super().__post_init__()
        require_label(self.statement_id, "statement_id")
        require_sha256(self.statement_sha256, "statement_sha256")
        if (self.status is ReconciliationStatus.BREAK) != bool(self.differences):
            raise InvalidFieldError("a break lists its differences; a match lists none")
        if self.resolves is not None and self.status is not ReconciliationStatus.MATCHED:
            raise InvalidFieldError("only a matched result resolves a break")
        _require_increasing([d.field for d in self.differences], "differences")
