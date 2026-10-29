# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Option premium, exercise and assignment (BP6 section 4.5 C; T7, T18).

Pure functions for the account fold.

- **Premium** = price x multiplier x quantity (OCC *Options 101*: "Premium
  quoted on a per share basis", "Multiply by 100 to calculate total cost"); the
  buyer pays it, the writer receives it.
- **Physical exercise:** a transaction in the underlying at the strike (OCC:
  "pay $5,000 ($50 x 100) in exchange for receipt of 100 XYZ shares"). The
  holder of a call receives the underlying, the writer assigned delivers it.
  The put side is not sourced and is refused (BP6 R4).
- **Cash settlement** (OCC, *Characteristics and Risks of Standardized
  Options*, June 2024, ch. II): call ``max(0, S - K) x M x q``, put
  ``max(0, K - S) x M x q``, with S the event's ``exercise_settlement_value``
  (amendment 2, F-D), K the strike, M the multiplier and q the signed
  quantity (exercise +, assignment -). A cash-settled event without S, or a
  physical one with it, is refused. Amounts are exact products.
- **Expiry** (``OptionExpiry``): the position closes without cash; automatic
  exercise is not modelled (R4).
"""

from dataclasses import dataclass
from decimal import Decimal

from auqua.contracts.events.lifecycle import OptionAssignment, OptionExercise
from auqua.contracts.events.trading import Fill
from auqua.contracts.instruments import Option, OptionRight, Settlement
from auqua.contracts.numeric import exact
from auqua.contracts.portfolio import Posting, PostingKind
from auqua.portfolio.effects import fee_posting, signed_quantity
from auqua.portfolio.errors import PortfolioError


def premium_postings(fill: Fill, option: Option, account: str) -> tuple[Posting, ...]:
    """The premium (buyer negative, writer positive) and the fee.

    Raises:
        PortfolioError: the fill is for another instrument.
    """
    if fill.instrument_id != option.id:
        raise PortfolioError(f"fill for {fill.instrument_id}, instrument {option.id}")
    with exact():
        premium = -(fill.price * option.multiplier * signed_quantity(fill))
    postings = []
    if not premium.is_zero():
        postings.append(
            Posting(account, option.quote_currency, premium, PostingKind.PREMIUM, fill.id)
        )
    fee = fee_posting(fill, account)
    if fee is not None:
        postings.append(fee)
    return tuple(postings)


@dataclass(frozen=True)
class Delivery:
    """The underlying transaction of a physical exercise: ``quantity`` (signed) at ``price``.

    ``underlying`` is the option's ``underlying`` text; the fold resolves the
    instrument it names.
    """

    underlying: str
    quantity: Decimal
    price: Decimal


@dataclass(frozen=True)
class ExerciseResult:
    """The cash posting of an exercise or assignment, and the delivery when physical."""

    postings: tuple[Posting, ...]
    delivery: Delivery | None = None


def cash_settlement_amount(option: Option, quantity: Decimal, settlement_value: Decimal) -> Decimal:
    """OCC's cash settlement amount for ``quantity`` (exercise +, assignment -) contracts."""
    with exact():
        if option.right is OptionRight.CALL:
            intrinsic = max(Decimal(0), settlement_value - option.strike)
        else:
            intrinsic = max(Decimal(0), option.strike - settlement_value)
        return intrinsic * option.multiplier * quantity


def exercise_effect(
    event: OptionExercise | OptionAssignment, option: Option, account: str
) -> ExerciseResult:
    """The effect of an exercise (holder) or an assignment (writer) on the account.

    Raises:
        PortfolioError: another instrument; a cash-settled event without
            ``exercise_settlement_value`` or a physical one with it (F-D); a
            physical put (R4).
    """
    if event.instrument_id != option.id:
        raise PortfolioError(f"{type(event).__name__} of {event.instrument_id}, option {option.id}")
    quantity = event.quantity if isinstance(event, OptionExercise) else -event.quantity
    value = event.exercise_settlement_value
    if option.settlement is Settlement.CASH:
        if value is None:
            raise PortfolioError(
                f"cash-settled {option.id} without exercise_settlement_value (amendment 2, F-D)"
            )
        amount = cash_settlement_amount(option, quantity, value)
        if amount.is_zero():
            return ExerciseResult(())
        posting = Posting(account, option.quote_currency, amount, PostingKind.EXERCISE, event.id)
        return ExerciseResult((posting,))
    if value is not None:
        raise PortfolioError(
            f"physical {option.id} with exercise_settlement_value (amendment 2, F-D)"
        )
    if option.right is not OptionRight.CALL:
        raise PortfolioError(f"physical exercise of put {option.id} is not sourced (BP6 R4)")
    with exact():
        units = quantity * option.multiplier
        cash = -(option.strike * units)
    posting = Posting(account, option.quote_currency, cash, PostingKind.EXERCISE, event.id)
    return ExerciseResult((posting,), Delivery(option.underlying, units, option.strike))
