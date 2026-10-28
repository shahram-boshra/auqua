# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Event-driven backtest engine (BP5 section 4; ADR 0030).

One configuration over one bar series: bar → signal → target → pre-trade risk
gate → simulated venue (cost model, optional latency) → book, as in the walking
skeleton (ADR 0025). The log clock advances to each bar's **availability time**
(BP4 section 4.6), so no decision can see a bar before it was available; the
bars come from a point-in-time snapshot (:mod:`auqua.analytics.inputs`).
Every decision is a canonical event whose id is name-based on the run, its
cause and its tag, so the same inputs give byte-identical records. Equity is
recorded after each bar, marked at its close, in exact decimals.
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal

from auqua.alpha.baseline import PROVIDER, PROVIDER_VERSION, MovingAverageCrossover
from auqua.clock.log import LogClock
from auqua.contracts.events.base import Event
from auqua.contracts.events.market import Bar
from auqua.contracts.events.trading import (
    Fill,
    OrderSubmitted,
    OrderType,
    RiskDecision,
    Side,
    Signal,
    Target,
)
from auqua.contracts.identity import derived_id
from auqua.contracts.instruments import AnyInstrument
from auqua.contracts.numeric import arithmetic, on_grid
from auqua.execution.costs import CostModel
from auqua.execution.simulated import SimulatedVenue
from auqua.portfolio.book import Book
from auqua.risk.gate import PreTradeGate, TradingLimits
from auqua.storage.marketview import available_ns

SOURCE = "research/backtest"
#: Namespace of ids of events derived by runs.
EVENT_NAMESPACE = derived_id(uuid.NAMESPACE_URL, b"https://github.com/shahram-boshra/auqua#event")


@dataclass(frozen=True)
class Settings:
    """Everything except the signal parameters that shapes a simulation.

    Attributes:
        quantity: Position held while the signal is +1 (on the quantity grid).
        initial_cash: Starting cash in the quote currency.
        costs: Fill price and fee model.
        limits: Pre-trade risk limits.
        delay_bars: Extra bars before an order fills (latency).
    """

    quantity: Decimal
    initial_cash: Decimal
    costs: CostModel
    limits: TradingLimits
    delay_bars: int = 0


@dataclass
class Simulation:
    """State and output of one configuration's simulation."""

    instrument: AnyInstrument
    fast: int
    slow: int
    settings: Settings
    run_id: uuid.UUID
    events: list[Event] = field(default_factory=list)
    equity: list[tuple[int, Decimal]] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Build the components; refuse a quantity off the instrument's grid."""
        on_grid(self.settings.quantity, self.instrument.quantity_increment)
        self.clock = LogClock()
        self.signal = MovingAverageCrossover(self.fast, self.slow)
        self.gate = PreTradeGate(self.settings.limits)
        self.venue = SimulatedVenue(
            self.settings.costs, self.instrument.price_increment, self.settings.delay_bars
        )
        self.book = Book(cash=self.settings.initial_cash)
        self.direction: int | None = None
        self.mark = Decimal(0)

    def _id(self, cause: uuid.UUID, tag: str) -> uuid.UUID:
        return derived_id(EVENT_NAMESPACE, self.run_id.bytes, cause.bytes, tag.encode())

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
            self.book.apply(fill)
            self.events.append(fill)
        self.mark = bar.close
        self.equity.append((available_ns(bar), self.book.equity(bar.close)))
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
        target = Target(
            id=self._id(signal.id, "portfolio.target.v1"),
            source=SOURCE,
            ts_event_ns=now,
            ts_ingest_ns=now,
            causation_id=signal.id,
            instrument_id=bar.instrument_id,
            quantity=self.settings.quantity if direction > 0 else Decimal(0),
        )
        self.events += [signal, target]
        with arithmetic():  # orders still in flight count (latency sweep)
            expected = self.book.position + self.venue.working(bar.instrument_id)
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
        self.events += [order, decision]
        if check.accepted:
            self.venue.submit(order)
