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
  long or flat in phase 1 (BP6 section 4.2). FX forwards are refused.
- **Inverse perpetuals (E).** Profit in coin = USD notional x (1/entry -
  1/exit), one rational rounded once (``HALF_EVEN``, BP6 D1). Funding (Deribit,
  *Inverse Perpetual*): "Funding Payment = Funding Rate * Position Size * Time
  Fraction", "Time Fraction = Funding Rate Time Period / 8 hours"; positive
  rate: longs pay. A reported amount is posted as reported (BP6 E2); without
  one, funding is computed only for a position whose size is given in the
  settlement currency (R10 (c)), and refused for a linear perpetual (R1).
"""

from collections.abc import Sequence
from decimal import Decimal

from auqua.config.portfolio import PortfolioConfig
from auqua.contracts.events.lifecycle import FundingPaymentV2, FuturesExpiry
from auqua.contracts.events.trading import Fill, Side
from auqua.contracts.instruments import (
    CryptoSpot,
    Future,
    FxDelivery,
    FxPair,
    PerpetualSwap,
    PerpetualType,
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


def funding_posting(
    event: FundingPaymentV2,
    swap: PerpetualSwap,
    config: PortfolioConfig,
    *,
    settlement_size: Decimal | None,
) -> Posting | None:
    """The funding posting in the settlement currency; ``None`` when the amount is zero.

    A reported ``amount`` is posted as reported, never recomputed (BP6 E2).
    Without one, the payment is computed from ``settlement_size`` (the signed
    position size in the settlement currency) and the configured rate period.

    Raises:
        PortfolioError: the event is for another instrument; no amount is
            reported and the position has no size in the settlement currency
            (R10 (c)) or the swap is linear (R1).
        ConfigurationError: the rate period or the settlement currency is not configured.
    """
    if event.instrument_id != swap.id:
        raise PortfolioError(f"funding of {event.instrument_id}, instrument {swap.id}")
    account = config.account_id
    if event.amount is not None:
        if event.amount.is_zero():
            return None
        return Posting(
            account, swap.settlement_currency, event.amount, PostingKind.FUNDING, event.id
        )
    if swap.contract_type is PerpetualType.LINEAR:
        raise PortfolioError(f"funding of linear {swap.id} without a reported amount (BP6 R1)")
    if settlement_size is None:
        raise PortfolioError(
            f"funding of {swap.id} without a reported amount on a position not sized in "
            f"{swap.settlement_currency} (BP6 R10 (c))"
        )
    entry = config.funding.get(str(swap.id))
    if entry is None:
        raise ConfigurationError(f"no funding rate period configured for {swap.id}")
    amount = funding_amount(
        event.funding_rate,
        settlement_size,
        event.interval_ns,
        entry.rate_period_ns,
        increment(config, swap.settlement_currency),
    )
    if amount.is_zero():
        return None
    return Posting(account, swap.settlement_currency, amount, PostingKind.FUNDING, event.id)
