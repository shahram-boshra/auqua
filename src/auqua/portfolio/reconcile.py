# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Reconciliation of venue statements with the account (BP6 section 4.7; PC-7; T13).

:meth:`Reconciler.reconcile` compares an ``account.statement.v1`` with the
:class:`~auqua.contracts.portfolio.AccountView` exactly: the cash balance of
every currency and the filled quantity of every instrument. Each difference is
a :class:`~auqua.contracts.events.account.Difference` (``balance:<CCY>`` or
``position:<VENUE:SYMBOL>``, expected = the view, reported = the statement); a
currency or instrument absent on one side counts as zero there, so a zero row
and a missing row agree. Reported funding amounts and bond accrued interest are
compared through the balances they post into: the statement carries balances
and positions, not per-posting lines.

A result is keyed by (``statement_id``, ``statement_sha256``), the SHA-256 of
the statement's canonical encoding, and its id is derived from that key: the
same statement twice yields one result, and the same id with other content is
refused (:class:`~auqua.portfolio.errors.ReconciliationConflictError`). A break
stays open until a later ``matched`` result names it in ``resolves`` (an
operator action, BP11).
"""

import hashlib
import uuid
from collections.abc import Iterable
from decimal import Decimal

from auqua.contracts.codec import encode
from auqua.contracts.events.account import (
    AccountStatement,
    Difference,
    ReconciliationResult,
    ReconciliationStatus,
)
from auqua.contracts.identity import derived_id
from auqua.contracts.portfolio import AccountView
from auqua.portfolio.errors import PortfolioError, ReconciliationConflictError

#: Namespace of reconciliation result ids (RFC 9562 name-based, ``derived_id``).
RESULT_NAMESPACE = derived_id(
    uuid.NAMESPACE_URL, b"https://github.com/shahram-boshra/auqua#reconciliation"
)
SOURCE = "portfolio/reconcile"


def statement_sha256(statement: AccountStatement) -> str:
    """SHA-256 of the statement's canonical encoding (lower-case hex)."""
    return hashlib.sha256(encode(statement)).hexdigest()


def _differences(
    prefix: str, expected: dict[str, Decimal], reported: dict[str, Decimal]
) -> list[Difference]:
    zero = Decimal(0)
    rows = []
    for key in sorted(set(expected) | set(reported)):
        mine, theirs = expected.get(key, zero), reported.get(key, zero)
        if mine != theirs:
            rows.append(Difference(f"{prefix}:{key}", expected.get(key), reported.get(key)))
    return rows


def compare(view: AccountView, statement: AccountStatement) -> tuple[Difference, ...]:
    """The differences between the view and the statement, in increasing field order."""
    balances = _differences(
        "balance",
        {b.currency: b.cash for b in view.balances},
        {b.currency: b.amount for b in statement.balances},
    )
    positions = _differences(
        "position",
        {str(p.instrument_id): p.quantity for p in view.positions},
        {str(p.instrument_id): p.quantity for p in statement.positions},
    )
    return tuple(balances + positions)


class Reconciler:
    """Results by statement id, with the breaks still open."""

    def __init__(self, results: Iterable[ReconciliationResult] = ()) -> None:
        """Start from the results already recorded (for example, replayed from the log)."""
        self._by_statement: dict[str, ReconciliationResult] = {}
        self._open: dict[uuid.UUID, ReconciliationResult] = {}
        for result in results:
            self.apply(result)

    def apply(self, result: ReconciliationResult) -> None:
        """Record ``result``; a repeat is ignored.

        Raises:
            ReconciliationConflictError: another result for the same statement id
                with another statement hash.
            PortfolioError: ``resolves`` names no open break.
        """
        known = self._by_statement.get(result.statement_id)
        if known is not None:
            if known.statement_sha256 != result.statement_sha256:
                raise ReconciliationConflictError(
                    f"statement {result.statement_id} re-sent with other content"
                )
            return
        if result.resolves is not None:
            if result.resolves not in self._open:
                raise PortfolioError(f"{result.resolves} is not an open break")
            del self._open[result.resolves]
        self._by_statement[result.statement_id] = result
        if result.status is ReconciliationStatus.BREAK:
            self._open[result.id] = result

    @property
    def open_breaks(self) -> tuple[ReconciliationResult, ...]:
        """Breaks not yet resolved, oldest first."""
        return tuple(self._open.values())

    def reconcile(
        self,
        view: AccountView,
        statement: AccountStatement,
        *,
        recorded_ns: int,
        resolves: uuid.UUID | None = None,
    ) -> ReconciliationResult:
        """Compare ``statement`` with ``view`` and record the result; a repeat returns the first.

        ``recorded_ns`` is the log-clock time of the statement's record (the
        result's ``ts_ingest_ns``); ``resolves`` names the open break an
        operator closes with this result (BP11).

        Raises:
            ReconciliationConflictError: the statement id was reconciled with other content.
            PortfolioError: ``resolves`` is given for a break, or names no open break.
        """
        digest = statement_sha256(statement)
        known = self._by_statement.get(statement.statement_id)
        if known is not None:
            if known.statement_sha256 != digest:
                raise ReconciliationConflictError(
                    f"statement {statement.statement_id} re-sent with other content"
                )
            return known
        differences = compare(view, statement)
        status = ReconciliationStatus.BREAK if differences else ReconciliationStatus.MATCHED
        if resolves is not None and status is ReconciliationStatus.BREAK:
            raise PortfolioError(f"statement {statement.statement_id} breaks; it resolves nothing")
        result = ReconciliationResult(
            id=derived_id(RESULT_NAMESPACE, statement.statement_id.encode(), digest.encode()),
            source=SOURCE,
            ts_event_ns=statement.as_of_ns,
            ts_ingest_ns=recorded_ns,
            statement_id=statement.statement_id,
            statement_sha256=digest,
            status=status,
            differences=differences,
            resolves=resolves,
        )
        self.apply(result)
        return result
