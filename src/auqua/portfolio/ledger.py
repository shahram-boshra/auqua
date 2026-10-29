# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Postings and balances per currency of one account (BP6 section 4.2; PC-2).

Every movement of money is a :class:`~auqua.contracts.portfolio.Posting`; a
balance is the exact sum of the postings of its currency. ``receivable``
postings (a dividend fixed at its ex-date and not yet paid) form the
receivable balance; every other kind forms the cash balance.
"""

from collections import defaultdict
from decimal import Decimal

from auqua.contracts.numeric import exact
from auqua.contracts.portfolio import Balance, Posting, PostingKind
from auqua.portfolio.errors import PortfolioError


class Ledger:
    """The postings of one account, in the order they were made."""

    def __init__(self, account: str) -> None:
        """Start an empty ledger for ``account``."""
        self.account = account
        self._postings: list[Posting] = []
        self._cash: defaultdict[str, Decimal] = defaultdict(Decimal)
        self._receivable: defaultdict[str, Decimal] = defaultdict(Decimal)

    def post(self, posting: Posting) -> None:
        """Record ``posting``.

        Raises:
            PortfolioError: the posting is for another account, or it would make
                a receivable balance negative.
        """
        if posting.account != self.account:
            raise PortfolioError(f"posting for {posting.account}, ledger of {self.account}")
        with exact():
            if posting.kind is PostingKind.RECEIVABLE:
                balance = self._receivable[posting.currency] + posting.amount
                if balance < 0:
                    raise PortfolioError(f"receivable in {posting.currency} would be {balance}")
                self._receivable[posting.currency] = balance
            else:
                self._cash[posting.currency] += posting.amount
        self._postings.append(posting)

    @property
    def postings(self) -> tuple[Posting, ...]:
        """All postings, oldest first."""
        return tuple(self._postings)

    def balance(self, currency: str) -> Balance:
        """Cash and receivable of ``currency`` (zero if nothing was posted)."""
        zero = Decimal(0)
        return Balance(
            currency, self._cash.get(currency, zero), self._receivable.get(currency, zero)
        )

    def balances(self) -> tuple[Balance, ...]:
        """Every currency with a posting, in increasing code order."""
        codes = sorted(set(self._cash) | set(self._receivable))
        return tuple(self.balance(code) for code in codes)
