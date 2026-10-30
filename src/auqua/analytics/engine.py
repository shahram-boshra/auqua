# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Event-driven backtest engine (BP5 section 4; ADR 0030).

One configuration over one bar series: bar → signal → allocation (target) →
pre-trade risk gate → simulated venue (cost model, optional latency) → account,
as in the walking skeleton (ADR 0025). The log clock advances to each bar's
**availability time** (BP4 section 4.6), so no decision can see a bar before it
was available; the bars come from a point-in-time snapshot
(:mod:`auqua.analytics.inputs`). Every decision is a canonical event whose id
is name-based on the run, its cause and its tag, so the same inputs give
byte-identical records.

The account is a :class:`~auqua.portfolio.account.Portfolio` (BP6 S11): the
initial cash is one ``account.transfer.v1``; every event of the simulation is
folded as a record of the simulation's own chain — sequences from 0, hashes by
``storage.chain.record_hash`` over the canonical encoding — which is run-local
(never a log position, never stamped on a projection; amendment 12, F-AC). The
account and the allocator read the run's market view as known at the input
snapshot's knowledge time (F-AB, F-AG). Equity is recorded after each bar in
the instrument's quote currency, exactly: the quote balance plus the held
quantity — for a spot pair the base-currency balance — times the close and the
multiplier (F-AA); a balance in any other currency is refused, never ignored.
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal

from auqua.alpha.baseline import PROVIDER, PROVIDER_VERSION, MovingAverageCrossover
from auqua.analytics.inputs import InputSnapshot
from auqua.clock.log import LogClock
from auqua.config.portfolio import PortfolioConfig, RateSource
from auqua.contracts.codec import encode
from auqua.contracts.events.account import CashTransfer
from auqua.contracts.events.base import Event, LogRecord
from auqua.contracts.events.market import Bar
from auqua.contracts.events.trading import (
    AllocationUnavailable,
    Fill,
    OrderSubmitted,
    OrderType,
    RiskDecision,
    Side,
    Signal,
    Target,
)
from auqua.contracts.identity import derived_id
from auqua.contracts.instruments import (
    AnyInstrument,
    Bond,
    CryptoSpot,
    Equity,
    FxPair,
    Option,
    PerpetualSwap,
)
from auqua.contracts.numeric import arithmetic, exact, on_grid
from auqua.contracts.portfolio import BASE_CURRENCY
from auqua.execution.costs import CostModel
from auqua.execution.simulated import SimulatedVenue
from auqua.portfolio.account import Portfolio
from auqua.portfolio.allocation import FixedQuantitySpec, VolatilityTargetSpec, allocator
from auqua.portfolio.errors import ConfigurationError
from auqua.portfolio.valuation import Valuation
from auqua.risk.gate import PreTradeGate, TradingLimits
from auqua.storage.chain import GENESIS, record_hash
from auqua.storage.marketview import MarketView, available_ns
from auqua.storage.reference import ReferenceIndex

SOURCE = "research/backtest"
#: Namespace of ids of events derived by runs.
EVENT_NAMESPACE = derived_id(uuid.NAMESPACE_URL, b"https://github.com/shahram-boshra/auqua#event")
#: ``reference`` of the transfer that funds a simulation (BP6 S11).
INITIAL_CASH = "initial_cash"


class EngineError(ValueError):
    """The simulation cannot value its account (an unsupported instrument or currency)."""


@dataclass(frozen=True)
class Settings:
    """Everything except the signal parameters that shapes a simulation.

    Attributes:
        allocation: Position sizing (BP6 section 4.9).
        initial_cash: Starting cash in the quote currency (one transfer).
        costs: Fill price and fee model.
        limits: Pre-trade risk limits.
        delay_bars: Extra bars before an order fills (latency).
    """

    allocation: FixedQuantitySpec | VolatilityTargetSpec
    initial_cash: Decimal
    costs: CostModel
    limits: TradingLimits
    delay_bars: int = 0


@dataclass(frozen=True)
class RunMarket:
    """What the run's account and allocator read (BP6 amendment 12, F-AB, F-AE, F-AG).

    Attributes:
        config: The portfolio configuration (currencies, rates).
        view: The run's market view: the snapshot's selected series and rates.
        reference: The snapshot's instrument versions.
        knowledge_ns: The snapshot's knowledge time; every lookup is as known then.
    """

    config: PortfolioConfig
    view: MarketView
    reference: ReferenceIndex
    knowledge_ns: int


def run_market(
    config: PortfolioConfig, inputs: InputSnapshot, instrument: AnyInstrument
) -> RunMarket:
    """The run's market from its input snapshot (F-AB, F-AG).

    Raises:
        EngineError: ``instrument`` has no version in the snapshot's instrument
            index as known at its knowledge time (the account needs it).
    """
    known = inputs.knowledge_ns
    start = inputs.bars[0].start_ns if inputs.bars else 0
    if inputs.reference.as_of(instrument.id, start, known) is None:
        raise EngineError(f"no instrument definition of {instrument.id} in the input snapshot")
    return RunMarket(config, inputs.market, inputs.reference, known)


def traded_currencies(instrument: AnyInstrument) -> tuple[str, ...]:
    """USD and the currencies a simulation of ``instrument`` posts, in code order."""
    codes = {BASE_CURRENCY, instrument.quote_currency}
    if isinstance(instrument, (CryptoSpot, FxPair)):
        codes.add(instrument.base_currency)
    if isinstance(instrument, PerpetualSwap):
        codes.add(instrument.settlement_currency)
    return tuple(sorted(codes))


def require_run_configuration(
    config: PortfolioConfig,
    instrument: AnyInstrument,
    allocation: FixedQuantitySpec | VolatilityTargetSpec,
) -> None:
    """Refuse a run whose portfolio configuration misses a currency or rate (F-AE).

    Every currency the run trades must be declared (code, kind, minor units,
    source, ``read_on``); ``volatility_target`` also needs the USD rate of each
    non-USD currency.

    Raises:
        ConfigurationError: naming the first missing currency or rate.
    """
    for code in traded_currencies(instrument):
        if code not in config.currencies:
            raise ConfigurationError(f"portfolio.currencies has no entry for {code}")
    if isinstance(allocation, VolatilityTargetSpec):
        for code in traded_currencies(instrument):
            if code != BASE_CURRENCY and code not in config.rates:
                raise ConfigurationError(f"portfolio.rates has no source for {code}")


def run_rates(
    config: PortfolioConfig,
    instrument: AnyInstrument,
    allocation: FixedQuantitySpec | VolatilityTargetSpec,
) -> dict[str, RateSource]:
    """The rate sources a run reads: those of its non-USD currencies, for volatility targeting."""
    if not isinstance(allocation, VolatilityTargetSpec):
        return {}
    return {
        code: config.rates[code]
        for code in traded_currencies(instrument)
        if code != BASE_CURRENCY and code in config.rates
    }


@dataclass
class Simulation:
    """State and output of one configuration's simulation."""

    instrument: AnyInstrument
    fast: int
    slow: int
    settings: Settings
    run_id: uuid.UUID
    market: RunMarket
    events: list[Event] = field(default_factory=list)
    equity: list[tuple[int, Decimal]] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Build the components and fund the account.

        Raises:
            OffGridError: a fixed quantity off the instrument's grid.
            EngineError: an instrument whose research equity is not defined
                (futures and perpetual swaps value by variation, not by close).
            ConfigurationError: a currency or rate missing from the configuration.
        """
        if not isinstance(self.instrument, (CryptoSpot, FxPair, Equity, Option, Bond)):
            kind = type(self.instrument).__name__
            raise EngineError(
                f"{self.instrument.id}: no research equity for a {kind} "
                f"({self.instrument.asset_class.value}; its value is variation or coin P&L, "
                "not quantity x close; BP6 R17)"
            )
        allocation = self.settings.allocation
        if isinstance(allocation, FixedQuantitySpec):
            on_grid(allocation.quantity, self.instrument.quantity_increment)
        market = self.market
        require_run_configuration(market.config, self.instrument, allocation)
        self.clock = LogClock()
        self.signal = MovingAverageCrossover(self.fast, self.slow)
        self.gate = PreTradeGate(self.settings.limits)
        self.venue = SimulatedVenue(
            self.settings.costs, self.instrument.price_increment, self.settings.delay_bars
        )
        self.portfolio = Portfolio(
            market.config, market.view, market.reference, knowledge_ns=market.knowledge_ns
        )
        self.allocator = allocator(
            allocation,
            market=market.view,
            valuation=Valuation(market.config, market.view, market.reference),
            reference=market.reference,
            knowledge_ns=market.knowledge_ns,
        )
        self._next_sequence = 0
        self._hash = GENESIS
        self.direction: int | None = None
        self.mark = Decimal(0)
        if self.settings.initial_cash:
            self._record(
                CashTransfer(
                    id=self._id(self.run_id, "account.transfer.v1"),
                    source=SOURCE,
                    ts_event_ns=0,
                    ts_ingest_ns=0,
                    currency=self.instrument.quote_currency,
                    amount=self.settings.initial_cash,
                    reference=INITIAL_CASH,
                )
            )

    def _id(self, cause: uuid.UUID, tag: str) -> uuid.UUID:
        return derived_id(EVENT_NAMESPACE, self.run_id.bytes, cause.bytes, tag.encode())

    def _record(self, event: Event) -> None:
        """Keep ``event`` and fold it as the next record of the simulation's own chain (F-AC)."""
        self.events.append(event)
        sequence = self._next_sequence
        digest = record_hash(self._hash, sequence, encode(event))
        derived = self.portfolio.apply(LogRecord(sequence=sequence, event=event), digest)
        self._next_sequence, self._hash = sequence + 1, digest
        for item in derived:
            self._record(item)

    def equity_at(self, close: Decimal) -> Decimal:
        """Equity in the quote currency: quote balance + held quantity x close x multiplier.

        The held quantity of a spot pair is its base-currency balance (fees paid
        in the base count); otherwise the filled position (F-AA).

        Raises:
            EngineError: a balance in a currency other than the quote (and the
                spot pair's base).
        """
        instrument, portfolio = self.instrument, self.portfolio
        quote = instrument.quote_currency
        base = instrument.base_currency if isinstance(instrument, (CryptoSpot, FxPair)) else None
        others = portfolio.currencies_held() - {quote, base}
        if others:
            raise EngineError(f"a balance in {', '.join(sorted(others))}: not in the equity series")
        cash, receivable = portfolio.amounts(quote)
        if base is None:
            units = portfolio.quantity(instrument.id)
        else:
            base_cash, base_receivable = portfolio.amounts(base)
        with exact():
            if base is not None:
                units = base_cash + base_receivable
            return cash + receivable + units * close * instrument.multiplier

    def run(self, bars: Iterable[Bar]) -> Simulation:
        """Process ``bars`` (in availability order) and return ``self``."""
        for bar in bars:
            self.on_bar(bar)
        return self

    def on_bar(self, bar: Bar) -> None:
        """Fill due orders at this bar's open, record equity, then decide on its close."""
        self.clock.advance(available_ns(bar))
        now = self.clock.now_ns()
        for execution in self.venue.on_bar(bar):
            fill = Fill(
                id=self._id(execution.order.id, "execution.fill.v1"),
                source=SOURCE,
                ts_event_ns=execution.ts_event_ns,
                ts_ingest_ns=now,
                causation_id=execution.order.id,
                instrument_id=bar.instrument_id,
                side=execution.side,
                price=execution.price,
                quantity=execution.quantity,
                fee=execution.fee,
                fee_currency=self.instrument.quote_currency,
            )
            self._record(fill)
        self.mark = bar.close
        self.equity.append((available_ns(bar), self.equity_at(bar.close)))
        direction = self.signal.on_bar(bar)
        if direction is None or direction == self.direction:
            return
        self.direction = direction
        self._decide(bar, direction, now)

    def _decide(self, bar: Bar, direction: int, now: int) -> None:
        signal = Signal(
            id=self._id(bar.id, "alpha.signal.v1"),
            source=SOURCE,
            ts_event_ns=now,
            ts_ingest_ns=now,
            causation_id=bar.id,
            instrument_id=bar.instrument_id,
            direction=direction,
            provider=PROVIDER,
            provider_version=PROVIDER_VERSION,
        )
        self._record(signal)
        view = self.portfolio.view()
        proposal = self.allocator.propose([signal], view)
        if proposal.unavailable:
            (missing,) = proposal.unavailable
            self._record(
                AllocationUnavailable(
                    id=self._id(signal.id, "portfolio.allocation_unavailable.v1"),
                    source=SOURCE,
                    ts_event_ns=now,
                    ts_ingest_ns=now,
                    causation_id=signal.id,
                    instrument_id=bar.instrument_id,
                    reason=missing.reason,
                )
            )
            return
        (proposed,) = proposal.targets
        target = Target(
            id=self._id(signal.id, "portfolio.target.v1"),
            source=SOURCE,
            ts_event_ns=now,
            ts_ingest_ns=now,
            causation_id=signal.id,
            instrument_id=bar.instrument_id,
            quantity=proposed.quantity,
            conversion=proposed.conversion,
        )
        self._record(target)
        row = next((p for p in view.positions if p.instrument_id == bar.instrument_id), None)
        with arithmetic():  # orders still in flight count (latency sweep; BP5 section 12 L7)
            expected = Decimal(0) if row is None else row.quantity + row.working
            delta = target.quantity - expected
        if not delta:
            return
        order = OrderSubmitted(
            id=self._id(target.id, "order.submitted.v1"),
            source=SOURCE,
            ts_event_ns=now,
            ts_ingest_ns=now,
            causation_id=target.id,
            instrument_id=bar.instrument_id,
            side=Side.BUY if delta > 0 else Side.SELL,
            quantity=abs(delta),
            order_type=OrderType.MARKET,
        )
        check = self.gate.check(order.side, order.quantity, bar.close, expected)
        decision = RiskDecision(
            id=self._id(order.id, "risk.decision.v1"),
            source=SOURCE,
            ts_event_ns=now,
            ts_ingest_ns=now,
            causation_id=order.id,
            instrument_id=bar.instrument_id,
            accepted=check.accepted,
            rule=check.rule,
            limit=check.limit,
            value=check.value,
        )
        self._record(order)
        self._record(decision)
        if check.accepted:
            self.venue.submit(order)
