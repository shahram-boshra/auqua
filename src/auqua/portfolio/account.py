# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""The account fold and its view (BP6 sections 4.8 and 4.10; amendment 6; T1).

:meth:`Portfolio.apply` folds one log record into the account; it is pure (no
wall clock, no I/O) and deterministic, so replaying the same log yields the
same postings, lots and views (P1). Its time is a :class:`LogClock` advanced by
each record's ``ts_ingest_ns`` (ADR 0009, as ``storage.replay``). It returns
the events it derives (FX settlement breaks, reconciliation results) with
replay-stable ids; the caller appends them to the log the fold reads, and the
fold, on reading its own derived event, checks it equals its derivation and
never applies it as an input (amendment 6, F-P).

Effects per record, from the per-class modules:

- a fill, by the instrument version in effect at the fill (ADR 0023): equities
  and bonds post the notional (bonds also the reported accrued interest),
  options the premium, spot pairs base and quote, futures and inverse
  perpetuals only the fee (a reduced inverse position posts its realized coin
  P&L); every fill posts its fee in its fee currency; linear perpetuals are
  refused (their P&L is not sourced, R1);
- a future: a fill belongs to the settlement of trading date D when it falls
  after the end of the previously settled date and at or before the end of D
  (the configured trading day; amendment 6, F-N); a later settlement date,
  a revision or a future without configuration is refused (R16);
- a cash dividend fixes its receivable before the first record at or after
  its ex-date and pays it before the first record at or after its pay date;
  splits, stock dividends, symbol changes and delistings apply before the
  first record at or after ``effective_ns``; recorded at or after that time,
  they are refused (never back-dated);
- working orders: an order with an accepted ``risk.decision.v1``, minus its
  fills; a rejected order never works; the remainder stays working until
  terminal order events exist (BP8; R15).

The view marks futures at their last settlement price (unrealized: the
variation not yet settled), inverse perpetuals at the latest close (their
unrealized P&L in the coin), equities, options and bonds at the latest close
(market value minus the lots' exact cost). Spot pairs hold their base as a
balance and are not listed as positions. Equity per currency (F-R) = cash +
receivable + the market value of equities, options and bonds (a written
option negative) + the unrealized P&L of futures and perpetuals; an unmarked
position makes its currency's equity, margin and base total unavailable.
"""

import datetime
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal

from auqua.clock.log import LogClock
from auqua.config.portfolio import FuturesEntry, PortfolioConfig
from auqua.contracts.events.account import (
    AccountStatement,
    CashTransfer,
    FxSettlementBreak,
    ReconciliationResult,
)
from auqua.contracts.events.base import Event, LogRecord
from auqua.contracts.events.lifecycle import (
    CorporateActionKind,
    CorporateActionV2,
    CouponPayment,
    FundingPaymentV2,
    FuturesExpiry,
    FxSettlement,
    OptionAssignment,
    OptionExercise,
    OptionExpiry,
)
from auqua.contracts.events.market import Bar, SettlementPrice
from auqua.contracts.events.reference import InstrumentDefined
from auqua.contracts.events.trading import Fill, OrderSubmitted, RiskDecision, Side
from auqua.contracts.identity import InstrumentId
from auqua.contracts.instruments import (
    AnyInstrument,
    Bond,
    CryptoSpot,
    Equity,
    Future,
    FxPair,
    Option,
    PerpetualSwap,
    PerpetualType,
)
from auqua.contracts.numeric import exact
from auqua.contracts.portfolio import (
    BASE_CURRENCY,
    AccountView,
    Conversion,
    LogPosition,
    PositionView,
    Posting,
    PostingKind,
)
from auqua.portfolio.bonds import bond_fill_postings, coupon_posting
from auqua.portfolio.corporate import (
    dividend_payment,
    dividend_receivable,
    require_applicable_dividend,
    split_multiplier,
)
from auqua.portfolio.effects import (
    PendingFx,
    fee_posting,
    funding_posting,
    futures_variation,
    increment,
    inverse_pnl,
    match_fx_settlement,
    pending_fx,
    require_cash_settled_expiry,
    signed_quantity,
    spot_fill_postings,
)
from auqua.portfolio.errors import CorporateActionLateError, PortfolioError
from auqua.portfolio.ledger import Ledger
from auqua.portfolio.lots import Lots
from auqua.portfolio.margin import PerInstrumentMargin, margin_rows
from auqua.portfolio.options import exercise_effect, premium_postings
from auqua.portfolio.reconcile import SOURCE as DERIVED_SOURCE
from auqua.portfolio.reconcile import Reconciler, fx_settlement_break
from auqua.portfolio.valuation import Valuation
from auqua.storage.chain import GENESIS
from auqua.storage.marketview import MarketView, available_ns, released
from auqua.storage.reference import ReferenceIndex

#: Instruments whose cost is paid in cash and whose market value counts in equity.
_CASH_INSTRUMENTS = (Equity, Option, Bond)


@dataclass
class _FutureState:
    """One future: the position at the last settlement and the trades since (F-N)."""

    settled_quantity: Decimal = Decimal(0)
    last_date: datetime.date | None = None
    last_price: Decimal | None = None
    last_available_ns: int | None = None
    trades: list[tuple[int, Decimal, Decimal]] = field(default_factory=list)


@dataclass
class _Scheduled:
    """A corporate action waiting for its time: a dividend's ex or pay date, or ``effective_ns``."""

    at_ns: int
    order: int
    action: CorporateActionV2
    stage: str
    entitled: Decimal = Decimal(0)


class Portfolio:
    """The account of ``config.account_id``, folded from log records."""

    def __init__(self, config: PortfolioConfig, market: MarketView) -> None:
        """Start an empty account; ``market`` supplies point-in-time bars and rates.

        Raises:
            ConfigurationError: USD is not among the configured currencies.
        """
        self._config = config
        self._market = market
        self._account = config.account_id
        self._clock = LogClock()
        self._ledger = Ledger(config.account_id)
        self._reference = ReferenceIndex()
        self._valuation = Valuation(config, market, self._reference)
        self._margin = PerInstrumentMargin(config)
        self._lots: dict[InstrumentId, Lots] = {}
        self._instruments: dict[InstrumentId, AnyInstrument] = {}
        self._futures: dict[InstrumentId, _FutureState] = {}
        self._submitted: dict[uuid.UUID, OrderSubmitted] = {}
        self._working: dict[uuid.UUID, tuple[InstrumentId, Decimal]] = {}
        self._pending_fx: list[PendingFx] = []
        self._scheduled: list[_Scheduled] = []
        self._delisted: set[InstrumentId] = set()
        self._reconciler = Reconciler()
        self._derived: dict[uuid.UUID, Event] = {}
        self._position = LogPosition(next_sequence=0, hash=GENESIS.hex())
        self._order = 0

    # --- folding -------------------------------------------------------------------------------
    @property
    def postings(self) -> tuple[Posting, ...]:
        """Every posting, in the order made."""
        return self._ledger.postings

    @property
    def log_position(self) -> LogPosition:
        """The next sequence the fold expects and the hash of the last record applied."""
        return self._position

    def lots(self, instrument_id: InstrumentId) -> Lots | None:
        """The lots of ``instrument_id``, if any were opened."""
        return self._lots.get(instrument_id)

    def apply(self, record: LogRecord[Event], record_hash: bytes) -> tuple[Event, ...]:
        """Fold ``record`` (with its chain hash) into the account; return the derived events.

        Raises:
            PortfolioError: a record out of sequence, or one the account
                refuses (the message names the rule).
        """
        if record.sequence != self._position.next_sequence:
            raise PortfolioError(
                f"record {record.sequence}, expected {self._position.next_sequence}"
            )
        event = record.event
        self._clock.advance(event.ts_ingest_ns)
        now = self._clock.now_ns()
        self._run_scheduled(now)
        derived = self._dispatch(record, now, record_hash)
        self._position = LogPosition(record.sequence + 1, record_hash.hex())
        for item in derived:
            self._derived[item.id] = item
        return derived

    def _dispatch(  # noqa: PLR0912 - one branch per event type
        self, record: LogRecord[Event], now: int, record_hash: bytes
    ) -> tuple[Event, ...]:
        event = record.event
        if isinstance(event, InstrumentDefined):
            self._reference.apply(record)
        elif isinstance(event, CashTransfer):
            self._ledger.post(
                Posting(self._account, event.currency, event.amount, PostingKind.TRANSFER, event.id)
            )
        elif isinstance(event, OrderSubmitted):
            self._submitted[event.id] = event
        elif isinstance(event, RiskDecision):
            self._decide(event)
        elif isinstance(event, Fill):
            self._fill(event, now)
        elif isinstance(event, SettlementPrice):
            self._settle(event, now)
        elif isinstance(event, FuturesExpiry):
            self._expire_future(event, now)
        elif isinstance(event, FundingPaymentV2):
            swap = self._version(event.instrument_id, event.ts_event_ns, now, PerpetualSwap)
            posting = funding_posting(event, swap, self._account)
            if posting is not None:
                self._ledger.post(posting)
        elif isinstance(event, CorporateActionV2):
            self._schedule(event, now)
        elif isinstance(event, (OptionExercise, OptionAssignment)):
            self._exercise(event, now)
        elif isinstance(event, OptionExpiry):
            self._close(event.instrument_id)
        elif isinstance(event, CouponPayment):
            bond = self._version(event.instrument_id, event.ts_event_ns, now, Bond)
            posting = coupon_posting(event, bond, self._quantity(bond.id), self._account)
            if posting is not None:
                self._ledger.post(posting)
        elif isinstance(event, FxSettlement):
            return self._settle_fx(event)
        elif isinstance(event, AccountStatement):
            view = self.view(LogPosition(record.sequence + 1, record_hash.hex()))
            result = self._reconciler.reconcile(view, event, recorded_ns=event.ts_ingest_ns)
            return () if result.id in self._derived else (result,)
        elif isinstance(event, (FxSettlementBreak, ReconciliationResult)):
            self._check_derived(event)
        return ()

    def _check_derived(self, event: FxSettlementBreak | ReconciliationResult) -> None:
        if event.source != DERIVED_SOURCE:
            if isinstance(event, ReconciliationResult):
                self._reconciler.apply(event)  # an operator's result (BP11)
                return
            raise PortfolioError(f"FX settlement break {event.id} from {event.source}")
        if self._derived.get(event.id) != event:
            raise PortfolioError(f"derived event {event.id} differs from the fold's derivation")

    # --- instruments ----------------------------------------------------------------------------
    def _version[I: AnyInstrument](
        self, instrument_id: InstrumentId, at_ns: int, now: int, kind: type[I]
    ) -> I:
        instrument = self._reference.as_of(instrument_id, at_ns, now)
        if not isinstance(instrument, kind):
            raise PortfolioError(f"{instrument_id} is not a {kind.__name__} in effect at {at_ns}")
        self._instruments[instrument_id] = instrument
        return instrument

    def _quantity(self, instrument_id: InstrumentId) -> Decimal:
        lots = self._lots.get(instrument_id)
        return Decimal(0) if lots is None else lots.position

    def _lots_of(self, instrument: AnyInstrument, currency: str) -> Lots:
        lots = self._lots.get(instrument.id)
        if lots is None:
            lots = Lots(increment(self._config, currency))
            self._lots[instrument.id] = lots
        return lots

    def _post(self, postings: Iterable[Posting]) -> None:
        for posting in postings:
            self._ledger.post(posting)

    # --- orders ---------------------------------------------------------------------------------
    def _decide(self, decision: RiskDecision) -> None:
        order = self._submitted.pop(decision.causation_id, None)
        if order is None or not decision.accepted:
            return
        quantity = order.quantity if order.side is Side.BUY else -order.quantity
        self._working[order.id] = (order.instrument_id, quantity)

    def _reduce_working(self, fill: Fill) -> None:
        working = self._working.get(fill.causation_id)
        if working is None:
            return
        instrument_id, remaining = working
        with exact():
            left = remaining - signed_quantity(fill)
        if left.is_zero():
            del self._working[fill.causation_id]
        elif (left > 0) != (remaining > 0):
            raise PortfolioError(f"fill {fill.id} exceeds order {fill.causation_id}")
        else:
            self._working[fill.causation_id] = (instrument_id, left)

    # --- fills ----------------------------------------------------------------------------------
    def _fill(self, fill: Fill, now: int) -> None:
        instrument = self._reference.as_of(fill.instrument_id, fill.ts_event_ns, now)
        if instrument is None:
            raise PortfolioError(f"fill {fill.id}: {fill.instrument_id} not in effect")
        self._instruments[instrument.id] = instrument
        quantity = signed_quantity(fill)
        if isinstance(instrument, (CryptoSpot, FxPair)):
            lots = self._lots_of(instrument, instrument.quote_currency)
            self._post(spot_fill_postings(fill, instrument, self._account, lots.position))
            lots.trade(quantity, fill.price)
            if isinstance(instrument, FxPair):
                self._pending_fx.append(pending_fx(fill, instrument))
        elif isinstance(instrument, (Equity, Bond)):
            self._long_or_flat(instrument, fill)
            if isinstance(instrument, Bond):
                self._post(bond_fill_postings(fill, instrument, self._account))
            else:
                self._post(self._equity_postings(fill, instrument))
            self._trade_at_value(instrument, quantity, fill.price)
        elif isinstance(instrument, Option):
            self._post(premium_postings(fill, instrument, self._account))
            self._trade_at_value(instrument, quantity, fill.price)
        elif isinstance(instrument, Future):
            self._future_fill(instrument, fill)
        elif isinstance(instrument, PerpetualSwap):
            self._perpetual_fill(instrument, fill)
        self._reduce_working(fill)

    def _long_or_flat(self, instrument: Equity | Bond, fill: Fill) -> None:
        if fill.side is Side.SELL and fill.quantity > self._quantity(instrument.id):
            raise PortfolioError(
                f"sell of {fill.quantity} {instrument.id}: spot is long or flat (BP6 section 4.2)"
            )

    def _equity_postings(self, fill: Fill, equity: Equity) -> list[Posting]:
        with exact():
            notional = -(fill.price * equity.multiplier * signed_quantity(fill))
        postings = []
        if not notional.is_zero():
            postings.append(
                Posting(self._account, equity.quote_currency, notional, PostingKind.TRADE, fill.id)
            )
        fee = fee_posting(fill, self._account)
        if fee is not None:
            postings.append(fee)
        return postings

    def _trade_at_value(
        self, instrument: Equity | Bond | Option, quantity: Decimal, price: Decimal
    ) -> None:
        """Lots at the value of one unit (price x multiplier), so their cost is in cash."""
        with exact():
            unit_value = price * instrument.multiplier
        self._lots_of(instrument, instrument.quote_currency).trade(quantity, unit_value)

    def _future_fill(self, future: Future, fill: Fill) -> None:
        entry = self._futures_entry(future.id)
        state = self._futures.setdefault(future.id, _FutureState())
        if state.last_date is not None and fill.ts_event_ns <= entry.end_ns(state.last_date):
            raise PortfolioError(
                f"fill {fill.id} of {future.id} belongs to settled trading date {state.last_date}"
            )
        self._post(p for p in (fee_posting(fill, self._account),) if p is not None)
        quantity = signed_quantity(fill)
        state.trades.append((fill.ts_event_ns, quantity, fill.price))
        self._lots_of(future, future.quote_currency).trade(quantity, fill.price)

    def _futures_entry(self, instrument_id: InstrumentId) -> FuturesEntry:
        entry = self._config.futures.get(str(instrument_id))
        if entry is None:
            raise PortfolioError(
                f"future {instrument_id} has no trading-day configuration (amendment 6, F-N)"
            )
        return entry

    def _perpetual_fill(self, swap: PerpetualSwap, fill: Fill) -> None:
        if swap.contract_type is not PerpetualType.INVERSE:
            raise PortfolioError(f"linear perpetual {swap.id}: P&L not sourced (BP6 R1)")
        self._post(p for p in (fee_posting(fill, self._account),) if p is not None)
        lots = self._lots_of(swap, swap.settlement_currency)
        quantity = signed_quantity(fill)
        result = lots.trade(quantity, fill.price)
        coin = increment(self._config, swap.settlement_currency)
        total = Decimal(0)
        for relief in result.relieved:
            with exact():
                entry = relief.cost / relief.quantity
                notional = relief.quantity * swap.multiplier * (-1 if quantity > 0 else 1)
            pnl = inverse_pnl(notional, entry, fill.price, coin)
            with exact():
                total += pnl
        if not total.is_zero():
            self._ledger.post(
                Posting(
                    self._account, swap.settlement_currency, total, PostingKind.REALIZED, fill.id
                )
            )

    # --- futures settlement ---------------------------------------------------------------------
    def _settle(self, settlement: SettlementPrice, now: int) -> None:
        future = self._version(settlement.instrument_id, now, now, Future)
        entry = self._futures_entry(future.id)
        state = self._futures.setdefault(future.id, _FutureState())
        if settlement.revision > 0:
            raise PortfolioError(
                f"settlement revision {settlement.revision} of {future.id}: no correction rule "
                "(BP6 R16)"
            )
        if state.last_date is not None and settlement.trading_date <= state.last_date:
            raise PortfolioError(
                f"settlement of {future.id} for {settlement.trading_date}, "
                f"already settled through {state.last_date}"
            )
        end = entry.end_ns(settlement.trading_date)
        due = [t for t in state.trades if t[0] <= end]
        state.trades = [t for t in state.trades if t[0] > end]
        self._post_variation(future, state, settlement.price, due, settlement.id)
        with exact():
            state.settled_quantity += sum((q for _, q, _ in due), Decimal(0))
        state.last_date = settlement.trading_date
        state.last_price = settlement.price
        state.last_available_ns = settlement.ts_published_ns

    def _post_variation(
        self,
        future: Future,
        state: _FutureState,
        price: Decimal,
        trades: list[tuple[int, Decimal, Decimal]],
        cause: uuid.UUID,
    ) -> None:
        start = None
        if state.last_price is not None:
            start = (state.settled_quantity, state.last_price)
        variation = futures_variation(
            multiplier=future.multiplier,
            money_increment=increment(self._config, future.quote_currency),
            settlement_price=price,
            start=start,
            trades=[(q, p) for _, q, p in trades],
        )
        if not variation.is_zero():
            self._ledger.post(
                Posting(
                    self._account, future.quote_currency, variation, PostingKind.VARIATION, cause
                )
            )

    def _expire_future(self, expiry: FuturesExpiry, now: int) -> None:
        future = self._version(expiry.instrument_id, now, now, Future)
        require_cash_settled_expiry(expiry, future)
        state = self._futures.setdefault(future.id, _FutureState())
        self._post_variation(future, state, expiry.settlement_price, state.trades, expiry.id)
        self._futures.pop(future.id)
        self._close(future.id)

    def _close(self, instrument_id: InstrumentId) -> None:
        self._lots.pop(instrument_id, None)

    # --- options --------------------------------------------------------------------------------
    def _exercise(self, event: OptionExercise | OptionAssignment, now: int) -> None:
        option = self._version(event.instrument_id, event.ts_event_ns, now, Option)
        held = self._quantity(option.id)
        closing = -event.quantity if isinstance(event, OptionExercise) else event.quantity
        if (held > 0) != (closing < 0) or abs(closing) > abs(held):
            raise PortfolioError(
                f"{type(event).__name__} of {event.quantity} {option.id}, position {held}"
            )
        result = exercise_effect(
            event, option, self._account, reference=self._reference, known_ns=now
        )
        self._post(result.postings)
        self._lots_of(option, option.quote_currency).trade(closing, Decimal(0))
        if self._quantity(option.id).is_zero():
            self._close(option.id)
        delivery = result.delivery
        if delivery is None:
            return
        underlying = self._reference.as_of(delivery.instrument_id, event.ts_event_ns, now)
        if isinstance(underlying, Equity):
            self._instruments[underlying.id] = underlying
            self._trade_at_value(underlying, delivery.quantity, delivery.price)
        elif isinstance(underlying, Future):
            state = self._futures.setdefault(underlying.id, _FutureState())
            state.trades.append((event.ts_event_ns, delivery.quantity, delivery.price))
            self._instruments[underlying.id] = underlying
            self._lots_of(underlying, underlying.quote_currency).trade(
                delivery.quantity, delivery.price
            )
        else:
            raise PortfolioError(
                f"physical delivery into {delivery.instrument_id} is not supported in phase 1"
            )

    # --- corporate actions ----------------------------------------------------------------------
    def _schedule(self, action: CorporateActionV2, now: int) -> None:
        self._order += 1
        if action.kind is CorporateActionKind.CASH_DIVIDEND:
            require_applicable_dividend(action, now)
            ex_date = action.ex_date_ns
            if ex_date is None:  # pragma: no cover - the event refuses it
                raise PortfolioError(f"cash dividend {action.id} without an ex-date")
            self._scheduled.append(_Scheduled(ex_date, self._order, action, "ex"))
            return
        if now >= action.effective_ns:
            raise CorporateActionLateError(
                f"{action.kind} {action.id} recorded at {now}, at or after its effective time "
                f"{action.effective_ns}"
            )
        self._scheduled.append(_Scheduled(action.effective_ns, self._order, action, "effective"))

    def _run_scheduled(self, now: int) -> None:
        while True:
            due = [s for s in self._scheduled if s.at_ns <= now]
            if not due:
                return
            item = min(due, key=lambda s: (s.at_ns, s.order))
            self._scheduled.remove(item)
            self._apply_scheduled(item)

    def _apply_scheduled(self, item: _Scheduled) -> None:
        action = item.action
        instrument_id = action.instrument_id
        if item.stage == "ex":
            entitled = self._quantity(instrument_id)
            posting = dividend_receivable(action, entitled, self._account)
            if posting is not None:
                self._ledger.post(posting)
            pay = action.pay_date_ns
            if pay is None:  # pragma: no cover - the event refuses it
                raise PortfolioError(f"cash dividend {action.id} without a pay date")
            self._scheduled.append(_Scheduled(pay, item.order, action, "pay", entitled))
        elif item.stage == "pay":
            self._post(dividend_payment(action, item.entitled, self._account))
        elif action.kind in {CorporateActionKind.SPLIT, CorporateActionKind.STOCK_DIVIDEND}:
            lots = self._lots.get(instrument_id)
            if lots is not None:
                lots.split(split_multiplier(action))
        elif action.kind is CorporateActionKind.SYMBOL_CHANGE:
            new_id = action.new_instrument_id
            if new_id is None:  # pragma: no cover - the event refuses it
                raise PortfolioError(f"symbol change {action.id} without new_instrument_id")
            if new_id in self._lots:
                raise PortfolioError(f"symbol change into {new_id}, which already has lots")
            if instrument_id in self._lots:
                at = action.effective_ns
                self._version(new_id, at, at, type(self._instruments[instrument_id]))
                self._lots[new_id] = self._lots.pop(instrument_id)
        elif action.kind is CorporateActionKind.DELISTING:
            self._delisted.add(instrument_id)

    # --- FX settlement --------------------------------------------------------------------------
    def _settle_fx(self, settlement: FxSettlement) -> tuple[Event, ...]:
        match = match_fx_settlement(settlement, self._pending_fx)
        if match.matched:
            self._pending_fx = [p for p in self._pending_fx if p.fill_id != match.fill_id]
            return ()
        recorded = fx_settlement_break(settlement, match, recorded_ns=settlement.ts_ingest_ns)
        return () if recorded is None or recorded.id in self._derived else (recorded,)

    # --- view -----------------------------------------------------------------------------------
    def view(self, log_position: LogPosition | None = None) -> AccountView:
        """The account at the clock's time, as of ``log_position`` (default: the fold's)."""
        as_of = self._clock.now_ns()
        balances = self._ledger.balances()
        equity: dict[str, Decimal | None] = {}
        for balance in balances:
            with exact():
                equity[balance.currency] = balance.cash + balance.receivable
        positions: list[PositionView] = []
        margined: list[tuple[AnyInstrument, Decimal, Decimal | None]] = []
        working = self._working_by_instrument()
        for instrument_id in sorted(set(self._lots) | set(working), key=str):
            instrument = self._instruments.get(instrument_id) or self._reference.as_of(
                instrument_id, as_of, as_of
            )
            if instrument is None:
                raise PortfolioError(f"working order on {instrument_id}, which is not defined")
            if isinstance(instrument, (CryptoSpot, FxPair)):
                continue
            if self._quantity(instrument_id).is_zero() and instrument_id not in working:
                continue
            row = self._position_view(instrument, working.get(instrument_id, Decimal(0)), as_of)
            positions.append(row.view)
            if not row.view.quantity.is_zero():
                margined.append((instrument, row.view.quantity, row.view.mark))
            _add(equity, row.view.currency, row.contribution)
        compared = dict(equity)
        for instrument, _, _ in margined:
            entry = self._config.margin.get(str(instrument.id))
            if entry is not None:
                compared.setdefault(entry.currency, Decimal(0))
        margin, unmargined = margin_rows(self._margin, margined, compared)
        conversions, unconverted, total = self._base_total(equity, as_of)
        return AccountView(
            account_id=self._account,
            as_of_ns=as_of,
            log_position=self._position if log_position is None else log_position,
            equity_base=total,
            balances=balances,
            positions=tuple(positions),
            margin=margin,
            unmargined=unmargined,
            conversions=conversions,
            unconverted=unconverted,
        )

    def _working_by_instrument(self) -> dict[InstrumentId, Decimal]:
        totals: dict[InstrumentId, Decimal] = {}
        for instrument_id, quantity in self._working.values():
            with exact():
                totals[instrument_id] = totals.get(instrument_id, Decimal(0)) + quantity
        return totals

    def _position_view(self, instrument: AnyInstrument, working: Decimal, as_of: int) -> _Row:
        quantity = self._quantity(instrument.id)
        currency = (
            instrument.settlement_currency
            if isinstance(instrument, PerpetualSwap)
            else instrument.quote_currency
        )
        mark, mark_ns, unrealized, contribution = self._valued(instrument, quantity, as_of)
        if quantity.is_zero():
            mark = mark_ns = unrealized = None
            contribution = Decimal(0)
        view = PositionView(
            instrument_id=instrument.id,
            currency=currency,
            quantity=quantity,
            working=working,
            mark=mark,
            mark_available_ns=mark_ns,
            unrealized=unrealized,
        )
        return _Row(view, contribution)

    def _valued(
        self, instrument: AnyInstrument, quantity: Decimal, as_of: int
    ) -> tuple[Decimal | None, int | None, Decimal | None, Decimal | None]:
        """Mark, its availability, unrealized P&L and the contribution to equity."""
        if isinstance(instrument, Future):
            state = self._futures.get(instrument.id)
            if state is None or state.last_price is None:
                return None, None, None, None
            pending = futures_variation(
                multiplier=instrument.multiplier,
                money_increment=increment(self._config, instrument.quote_currency),
                settlement_price=state.last_price,
                start=None,
                trades=[(q, p) for _, q, p in state.trades],
            )
            return state.last_price, state.last_available_ns, pending, pending
        close = (
            None if instrument.id in self._delisted else self._latest_close(instrument.id, as_of)
        )
        if close is None:
            return None, None, None, None
        mark, mark_ns = close
        lots = self._lots.get(instrument.id)
        if isinstance(instrument, PerpetualSwap):
            coin = increment(self._config, instrument.settlement_currency)
            total = Decimal(0)
            for lot in () if lots is None else lots.lots():
                entry = lot.unit_cost
                if entry is None:  # pragma: no cover - perpetual lots are never split
                    return None, None, None, None
                with exact():
                    notional = lot.quantity * instrument.multiplier * (1 if quantity > 0 else -1)
                pnl = inverse_pnl(notional, entry, mark, coin)
                with exact():
                    total += pnl
            return mark, mark_ns, total, total
        cost = Decimal(0) if lots is None else lots.cost
        with exact():
            value = mark * instrument.multiplier * quantity
            unrealized = value - cost if quantity > 0 else value + cost
        return mark, mark_ns, unrealized, value

    def _latest_close(self, instrument_id: InstrumentId, as_of: int) -> tuple[Decimal, int] | None:
        candidates: list[Bar] = []
        for listed, interval in self._market.bar_series():
            if listed != instrument_id:
                continue
            bars = list(released(self._market.bars(listed, interval, known_ns=as_of), as_of))
            if bars:
                candidates.append(bars[-1])
        if not candidates:
            return None
        latest = max(candidates, key=lambda bar: (available_ns(bar), bar.start_ns))
        return latest.close, available_ns(latest)

    def _base_total(
        self, equity: dict[str, Decimal | None], as_of: int
    ) -> tuple[tuple[Conversion, ...], tuple[str, ...], Decimal | None]:
        conversions: list[Conversion] = []
        unconverted: list[str] = []
        total = Decimal(0)
        for currency in sorted(equity):
            amount = equity[currency]
            if amount is None:
                unconverted.append(currency)
                continue
            if currency == BASE_CURRENCY:
                with exact():
                    total += amount
                continue
            conversion = self._valuation.convert(amount, currency, at_ns=as_of, known_ns=as_of)
            if conversion is None:
                unconverted.append(currency)
                continue
            conversions.append(conversion)
            with exact():
                total += conversion.value
        return tuple(conversions), tuple(unconverted), None if unconverted else total


@dataclass(frozen=True)
class _Row:
    view: PositionView
    contribution: Decimal | None


def _add(equity: dict[str, Decimal | None], currency: str, amount: Decimal | None) -> None:
    """Add a position's contribution; an unmarked position makes the currency unavailable."""
    current = equity.get(currency, Decimal(0))
    if amount is None or current is None:
        equity[currency] = None
        return
    with exact():
        equity[currency] = current + amount
