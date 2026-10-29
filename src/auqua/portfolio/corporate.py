# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Corporate actions on equities and funds (BP6 section 4.5 A; T4, T17).

Pure functions for the account fold.

- **Cash dividend** (``lifecycle.corporate_action.v2``): entitlement is fixed
  at the ex-date — "If you purchase a stock on its ex-dividend date or after,
  you will not receive the next dividend payment" (investor.gov). The entitled
  units are those held immediately before ``ex_date_ns``; the fold posts the
  ``receivable`` before it applies the first record at or after
  ``ex_date_ns`` and moves it to cash (``dividend``) before the first record at
  or after ``pay_date_ns``. A dividend recorded at or after its ex-date would
  have to be back-dated and is refused; one paid before its ex-date is refused
  (amendment 2, F-G: a structural check, no 25 % threshold).
- **Split and stock dividend:** applied at ``effective_ns``, the start of the
  ex-date (amendment 2, F-G2); lot by lot the quantity is multiplied and the
  total cost kept (IRS FAQ; :meth:`auqua.portfolio.lots.Lots.split`). A split
  of r-for-1 multiplies by r, a stock dividend of d units per unit held by
  1 + d.
"""

from decimal import Decimal

from auqua.contracts.events.lifecycle import CorporateActionKind, CorporateActionV2
from auqua.contracts.numeric import exact
from auqua.contracts.portfolio import Posting, PostingKind
from auqua.portfolio.errors import (
    CorporateActionLateError,
    CorporateActionUnsupportedError,
    PortfolioError,
)


def require_applicable_dividend(action: CorporateActionV2, record_ns: int) -> None:
    """Refuse a cash dividend that phase 1 cannot apply as recorded.

    ``record_ns`` is the log time of the dividend's own record. The receivable
    must be posted before the first record at or after ``ex_date_ns``; a record
    at or after it is itself such a record, so it is late.

    Raises:
        PortfolioError: ``action`` is not a cash dividend.
        CorporateActionUnsupportedError: its pay date precedes its ex-date (F-G).
        CorporateActionLateError: it is recorded at or after its ex-date.
    """
    if action.kind is not CorporateActionKind.CASH_DIVIDEND:
        raise PortfolioError(f"{action.id} is a {action.kind}, not a cash dividend")
    ex_date, pay_date = action.ex_date_ns, action.pay_date_ns
    if ex_date is None or pay_date is None:  # pragma: no cover - the event refuses it
        raise PortfolioError(f"cash dividend {action.id} without its dates")
    if pay_date < ex_date:
        raise CorporateActionUnsupportedError(
            f"cash dividend {action.id} is paid before its ex-date (BP6 amendment 2, F-G)"
        )
    if record_ns >= ex_date:
        raise CorporateActionLateError(
            f"cash dividend {action.id} recorded at {record_ns}, at or after its ex-date {ex_date}"
        )


def _dividend_amount(action: CorporateActionV2, entitled_units: Decimal) -> Decimal:
    if entitled_units < 0:
        raise PortfolioError(f"entitled units must be >= 0, got {entitled_units}")
    if action.cash_amount is None:  # pragma: no cover - the event refuses it
        raise PortfolioError(f"cash dividend {action.id} without cash_amount")
    with exact():
        return action.cash_amount * entitled_units


def dividend_receivable(
    action: CorporateActionV2, entitled_units: Decimal, account: str
) -> Posting | None:
    """The ``receivable`` posted at the ex-date: ``cash_amount`` x entitled units; ``None`` if 0.

    Raises:
        PortfolioError: negative entitled units (spot is long or flat).
    """
    amount = _dividend_amount(action, entitled_units)
    if amount.is_zero() or action.currency is None:
        return None
    return Posting(account, action.currency, amount, PostingKind.RECEIVABLE, action.id)


def dividend_payment(
    action: CorporateActionV2, entitled_units: Decimal, account: str
) -> tuple[Posting, ...]:
    """At the pay date: the receivable leaves (``receivable``, negative) and the cash arrives.

    Raises:
        PortfolioError: negative entitled units.
    """
    amount = _dividend_amount(action, entitled_units)
    if amount.is_zero() or action.currency is None:
        return ()
    return (
        Posting(account, action.currency, -amount, PostingKind.RECEIVABLE, action.id),
        Posting(account, action.currency, amount, PostingKind.DIVIDEND, action.id),
    )


def split_multiplier(action: CorporateActionV2) -> Decimal:
    """The lot quantity multiplier: r for an r-for-1 split, 1 + d for a stock dividend of d.

    Raises:
        PortfolioError: the action is neither a split nor a stock dividend.
    """
    if action.ratio is None or action.kind not in {
        CorporateActionKind.SPLIT,
        CorporateActionKind.STOCK_DIVIDEND,
    }:
        raise PortfolioError(f"{action.id} is a {action.kind}: no lot multiplier")
    if action.kind is CorporateActionKind.SPLIT:
        return action.ratio
    with exact():
        return 1 + action.ratio
