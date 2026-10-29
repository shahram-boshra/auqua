# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Cash effects of futures, spot pairs and perpetuals (BP6 section 4.5 B, D, E; T6, T8-T10).

Pure functions: they read no clock and no store, and return amounts or
:class:`~auqua.contracts.portfolio.Posting` values for the account fold.

- **Futures (B).** CME, *Money Calculations for Futures and Options*: the
  day's variation is "The mark-to-market amounts on each trade cleared on that
  day, from trade price to that day's end-of-day settlement price" plus "the
  mark-to-market amount on the start-of-day position, from the previous day's
  settlement price". The monetary value of one contract at each price is
  rounded "normally to the normal precision of the contract's settlement
  currency", where "values that are precisely equidistant between two points
  are rounded up - ie, away from zero" (``HALF_UP``), then multiplied by the
  signed quantity. Expiry: the final variation at the expiry's settlement
  price; physical delivery is refused (BP6 R3).
- **Spot pairs (D, E).** A fill posts ``+base`` and ``-quote`` (a buy) or the
  reverse (a sell) on the trade date, and its fee in its fee currency. Spot is
  long or flat in phase 1 (BP6 section 4.2). FX forwards are refused. An FX
  settlement is matched to one pending fill (BP6 amendment 3, after T2S
  matching practice, NBB-SSS Annex 21): by its trade reference
  (``causation_id``) when present, else by its economic fields; a difference,
  no candidate, or candidates on both sides is a break, never a guess.
- **Inverse perpetuals (E).** Profit in coin = USD notional x (1/entry -
  1/exit), one rational rounded once (``HALF_EVEN``, BP6 D1). Funding (Deribit,
  *Inverse Perpetual*): "Funding Payment = Funding Rate * Position Size * Time
  Fraction", "Time Fraction = Funding Rate Time Period / 8 hours"; positive
  rate: longs pay. A reported amount is posted as reported (BP6 E2); without
  one, funding is refused in phase 1 (amendment 3, F-H): Deribit's "Position
  Size" is the size in the settlement currency, which needs a price for both
  inverse and linear positions (R10 (c), R1). :func:`funding_amount` is the
  formula as printed, tested and not used by the fold.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from auqua.config.portfolio import PortfolioConfig
from auqua.contracts.events.account import Difference
from auqua.contracts.events.lifecycle import FundingPaymentV2, FuturesExpiry, FxSettlement
from auqua.contracts.events.trading import Fill, Side
from auqua.contracts.identity import InstrumentId
from auqua.contracts.instruments import (
    CryptoSpot,
    Future,
    FxDelivery,
    FxPair,
    PerpetualSwap,
    Settlement,
)
from auqua.contracts.numeric import Rounding, divide, exact, quantize
from auqua.contracts.portfolio import Posting, PostingKind
from auqua.portfolio.errors import ConfigurationError, PortfolioError


def increment(config: PortfolioConfig, currency: str) -> Decimal:
    """The money increment of ``currency`` from its configured minor units (BP6 R6, C5).

    Raises:
        ConfigurationError: ``currency`` is not among the configured currencies.
    """
    entry = config.currencies.get(currency)
    if entry is None:
        raise ConfigurationError(f"{currency} is not among the configured currencies")
    return Decimal(1).scaleb(-entry.minor_units)


def signed_quantity(fill: Fill) -> Decimal:
    """The fill's quantity, positive for a buy and negative for a sell."""
    return fill.quantity if fill.side is Side.BUY else -fill.quantity


def fee_posting(fill: Fill, account: str) -> Posting | None:
    """The fill's fee as a ``fee`` posting in ``Fill.fee_currency``; ``None`` for no fee."""
    if fill.fee.is_zero():
        return None
    return Posting(account, fill.fee_currency, -fill.fee, PostingKind.FEE, fill.id)


# --- B: futures ------------------------------------------------------------------------------
def contract_value(price: Decimal, multiplier: Decimal, money_increment: Decimal) -> Decimal:
    """The monetary value of one contract at ``price``, rounded normally (CME; ``HALF_UP``)."""
    with exact():
        value = price * multiplier
    return quantize(value, money_increment, Rounding.HALF_UP)


def futures_variation(
    *,
    multiplier: Decimal,
    money_increment: Decimal,
    settlement_price: Decimal,
    start: tuple[Decimal, Decimal] | None,
    trades: Sequence[tuple[Decimal, Decimal]],
) -> Decimal:
    """One day's variation: the start-of-day position plus the day's trades (CME).

    ``start`` is (signed quantity, previous settlement price) of the
    start-of-day position, ``None`` when there was none; ``trades`` are
    (signed quantity, trade price) of the fills cleared that day.
    """
    settle = contract_value(settlement_price, multiplier, money_increment)
    with exact():
        total = Decimal(0)
        if start is not None:
            quantity, previous = start
            total += quantity * (settle - contract_value(previous, multiplier, money_increment))
        for quantity, price in trades:
            total += quantity * (settle - contract_value(price, multiplier, money_increment))
    return total


def require_cash_settled_expiry(expiry: FuturesExpiry, future: Future) -> None:
    """Refuse an expiry with physical delivery (BP6 R3).

    Raises:
        PortfolioError: the expiry or the future is physically settled, or the
            expiry is for another instrument.
    """
    if expiry.instrument_id != future.id:
        raise PortfolioError(f"expiry of {expiry.instrument_id}, future {future.id}")
    if expiry.settlement is not Settlement.CASH or future.settlement is not Settlement.CASH:
        raise PortfolioError(f"physical delivery of {future.id} is not supported (BP6 R3)")


# --- D, E: spot pairs ------------------------------------------------------------------------
def spot_fill_postings(
    fill: Fill, pair: CryptoSpot | FxPair, account: str, held: Decimal
) -> tuple[Posting, ...]:
    """``+base``/``-quote`` (or the reverse) on the trade date, then the fee (BP6 section 4.2).

    ``held`` is the base quantity held before the fill: spot is long or flat.

    Raises:
        PortfolioError: the fill is for another instrument, the pair is an FX
            forward, its multiplier is not 1, or a sell exceeds ``held``.
    """
    if fill.instrument_id != pair.id:
        raise PortfolioError(f"fill for {fill.instrument_id}, instrument {pair.id}")
    if isinstance(pair, FxPair) and pair.delivery is not FxDelivery.SPOT:
        raise PortfolioError(f"{pair.id} is an FX forward; only spot is supported")
    if pair.multiplier != 1:
        raise PortfolioError(f"{pair.id} has multiplier {pair.multiplier}; spot needs 1")
    if fill.side is Side.SELL and fill.quantity > held:
        raise PortfolioError(f"sell of {fill.quantity} {pair.base_currency}, {held} held")
    quantity = signed_quantity(fill)
    with exact():
        notional = fill.price * quantity
    postings = [Posting(account, pair.base_currency, quantity, PostingKind.TRADE, fill.id)]
    if not notional.is_zero():
        postings.append(
            Posting(account, pair.quote_currency, -notional, PostingKind.TRADE, fill.id)
        )
    fee = fee_posting(fill, account)
    if fee is not None:
        postings.append(fee)
    return tuple(postings)


@dataclass(frozen=True)
class PendingFx:
    """An FX spot fill not yet settled: its side and the two amounts it exchanges."""

    fill_id: uuid.UUID
    instrument_id: InstrumentId
    side: Side
    base_amount: Decimal
    quote_amount: Decimal
    ts_event_ns: int


def pending_fx(fill: Fill, pair: FxPair) -> PendingFx:
    """The settlement a spot fill awaits (amounts positive, as an ``FxSettlement`` carries them).

    Raises:
        PortfolioError: as :func:`spot_fill_postings` for the instrument terms.
    """
    if fill.instrument_id != pair.id:
        raise PortfolioError(f"fill for {fill.instrument_id}, instrument {pair.id}")
    if pair.delivery is not FxDelivery.SPOT or pair.multiplier != 1:
        raise PortfolioError(f"{pair.id}: only spot with multiplier 1 is supported")
    with exact():
        quote = fill.price * fill.quantity
    return PendingFx(fill.id, pair.id, fill.side, fill.quantity, quote, fill.ts_event_ns)


class FxBreak(StrEnum):
    """Why an FX settlement matched no pending fill."""

    AMOUNTS = "amounts"
    UNMATCHED = "unmatched"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class FxMatch:
    """The outcome for one settlement: the fill it settles, or the break and its differences."""

    settlement_id: uuid.UUID
    fill_id: uuid.UUID | None
    reason: FxBreak | None = None
    differences: tuple[Difference, ...] = ()

    @property
    def matched(self) -> bool:
        """``True`` when the settlement settles :attr:`fill_id` exactly."""
        return self.reason is None


def _amount_differences(settlement: FxSettlement, fill: PendingFx) -> tuple[Difference, ...]:
    pairs = (
        ("base_amount", fill.base_amount, settlement.base_amount),
        ("quote_amount", fill.quote_amount, settlement.quote_amount),
    )
    return tuple(Difference(name, mine, theirs) for name, mine, theirs in pairs if mine != theirs)


def match_fx_settlement(settlement: FxSettlement, pending: Sequence[PendingFx]) -> FxMatch:
    """Match ``settlement`` to one of the ``pending`` fills, given in log order (amendment 3).

    With a trade reference (``causation_id``) only that fill is a candidate:
    absent, the settlement is unmatched; other amounts, an amount break.
    Without one, candidates are the fills of the instrument with equal amounts
    executed before the value date: none → unmatched; buys and sells among
    them → ambiguous (the event carries no direction); else the oldest
    (earliest ``ts_event_ns``, then log order).
    """
    fills = [p for p in pending if p.instrument_id == settlement.instrument_id]
    if settlement.causation_id is not None:
        referenced = [p for p in fills if p.fill_id == settlement.causation_id]
        if not referenced:
            return FxMatch(settlement.id, None, FxBreak.UNMATCHED)
        (fill,) = referenced
        differences = _amount_differences(settlement, fill)
        if differences:
            return FxMatch(settlement.id, fill.fill_id, FxBreak.AMOUNTS, differences)
        return FxMatch(settlement.id, fill.fill_id)
    candidates = [
        p
        for p in fills
        if p.ts_event_ns < settlement.value_date_ns and not _amount_differences(settlement, p)
    ]
    if not candidates:
        return FxMatch(settlement.id, None, FxBreak.UNMATCHED)
    if len({p.side for p in candidates}) > 1:
        return FxMatch(settlement.id, None, FxBreak.AMBIGUOUS)
    oldest = min(candidates, key=lambda p: p.ts_event_ns)  # the first in log order on a tie
    return FxMatch(settlement.id, oldest.fill_id)


# --- E: perpetuals ---------------------------------------------------------------------------
def inverse_pnl(
    notional: Decimal, entry: Decimal, exit_price: Decimal, coin_increment: Decimal
) -> Decimal:
    """Coin profit of a USD ``notional`` (long positive) from ``entry`` to ``exit_price``.

    USD notional x (1/entry - 1/exit) as one rational, rounded once (``HALF_EVEN``).

    Raises:
        PortfolioError: a price is not positive.
    """
    if entry <= 0 or exit_price <= 0:
        raise PortfolioError("inverse prices must be positive")
    with exact():
        numerator = notional * (exit_price - entry)
        denominator = entry * exit_price
    return divide(numerator, denominator, coin_increment, Rounding.HALF_EVEN)


def funding_amount(
    rate: Decimal, size: Decimal, interval_ns: int, period_ns: int, money_increment: Decimal
) -> Decimal:
    """Funding received by a position of ``size`` (long positive); a positive rate: longs pay.

    -(Funding Rate x Position Size x interval / period), one rational rounded
    once (``HALF_EVEN``).
    """
    with exact():
        numerator = -(rate * size * interval_ns)
    return divide(numerator, Decimal(period_ns), money_increment, Rounding.HALF_EVEN)


def funding_posting(event: FundingPaymentV2, swap: PerpetualSwap, account: str) -> Posting | None:
    """The reported funding in the settlement currency; ``None`` when it is zero.

    The ``amount`` is posted as reported, never recomputed (BP6 E2). Phase 1
    computes no funding (amendment 3, F-H): without an amount the event is
    refused, for inverse and linear swaps alike (R10 (c), R1).

    Raises:
        PortfolioError: the event is for another instrument, or reports no amount.
    """
    if event.instrument_id != swap.id:
        raise PortfolioError(f"funding of {event.instrument_id}, instrument {swap.id}")
    if event.amount is None:
        raise PortfolioError(
            f"funding of {swap.id} without a reported amount is not computed in phase 1 "
            "(BP6 R10 (c), R1)"
        )
    if event.amount.is_zero():
        return None
    return Posting(account, swap.settlement_currency, event.amount, PostingKind.FUNDING, event.id)
