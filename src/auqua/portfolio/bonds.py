# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Bond trades and coupons (BP6 section 4.5 F; amendment 2, F-E2; T11).

Pure functions for the account fold.

- **Trade:** the notional is price x multiplier x quantity at the clean price
  (the quote convention lives in the instrument's multiplier). The fill's
  ``accrued_interest`` — what the buyer pays the seller, as the venue reports
  it — is required for a bond with ``coupon_rate > 0`` (a zero-coupon bond is
  exempt) and posted as ``accrued``: the buyer negative, the seller positive;
  it never enters a lot's cost and is never recomputed.
- **Coupon** = ``amount_per_unit`` x units held, in the event's currency.
"""

from decimal import Decimal

from auqua.contracts.events.lifecycle import CouponPayment
from auqua.contracts.events.trading import Fill, Side
from auqua.contracts.instruments import Bond
from auqua.contracts.numeric import exact
from auqua.contracts.portfolio import Posting, PostingKind
from auqua.portfolio.effects import fee_posting, signed_quantity
from auqua.portfolio.errors import PortfolioError


def bond_fill_postings(fill: Fill, bond: Bond, account: str) -> tuple[Posting, ...]:
    """The clean notional (``trade``), the reported accrued interest (``accrued``) and the fee.

    Raises:
        PortfolioError: another instrument, or no ``accrued_interest`` on a
            bond with a coupon.
    """
    if fill.instrument_id != bond.id:
        raise PortfolioError(f"fill for {fill.instrument_id}, instrument {bond.id}")
    if bond.coupon_rate > 0 and fill.accrued_interest is None:
        raise PortfolioError(
            f"fill of {bond.id} (coupon {bond.coupon_rate}) without accrued_interest "
            "(BP6 amendment 2, F-E2)"
        )
    with exact():
        notional = -(fill.price * bond.multiplier * signed_quantity(fill))
    currency = bond.quote_currency
    postings = []
    if not notional.is_zero():
        postings.append(Posting(account, currency, notional, PostingKind.TRADE, fill.id))
    accrued = fill.accrued_interest
    if accrued is not None and not accrued.is_zero():
        paid = -accrued if fill.side is Side.BUY else accrued
        postings.append(Posting(account, currency, paid, PostingKind.ACCRUED, fill.id))
    fee = fee_posting(fill, account)
    if fee is not None:
        postings.append(fee)
    return tuple(postings)


def coupon_posting(
    event: CouponPayment, bond: Bond, units: Decimal, account: str
) -> Posting | None:
    """``amount_per_unit`` x ``units`` held, as a ``coupon`` posting; ``None`` when it is zero.

    Raises:
        PortfolioError: another instrument, or negative units (bonds are long or flat).
    """
    if event.instrument_id != bond.id:
        raise PortfolioError(f"coupon of {event.instrument_id}, bond {bond.id}")
    if units < 0:
        raise PortfolioError(f"units held must be >= 0, got {units}")
    with exact():
        amount = event.amount_per_unit * units
    if amount.is_zero():
        return None
    return Posting(account, event.currency, amount, PostingKind.COUPON, event.id)
