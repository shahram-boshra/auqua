# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Backtest runner of the walking skeleton (thin); owned and extended by BP5 (ADR 0025).

One historical path: canonical bars in the input event log → baseline signal
→ portfolio target → pre-trade risk gate → simulated venue fill → portfolio
update → static report. The input log is read, never written. Every decision
is a canonical event appended to the run's own log
(``<runs_root>/<run_id>/event_log.sqlite3``), opened with :class:`EventLog`
like any other log; the run log starts with :class:`RunStarted`, which names
the input snapshot, configuration, specification, risk limits and code. Ids of
derived events are name-based (run id, causing event, tag), and every
timestamp comes from the log clock, so running the same inputs again produces
byte-identical records (the second run's appends are counted duplicates).
"""

import hashlib
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import msgspec
from pydantic import Field

from auqua.alpha.baseline import PROVIDER, PROVIDER_VERSION, MovingAverageCrossover
from auqua.analytics.lineage import code_id as current_code_id
from auqua.analytics.lineage import code_version, run_id
from auqua.clock.log import LogClock
from auqua.config.base import ConfigModel
from auqua.config.identity import config_id as model_id
from auqua.config.loader import LoadedConfig
from auqua.contracts.events.base import Event
from auqua.contracts.events.market import Bar
from auqua.contracts.events.trading import (
    Fill,
    OrderSubmitted,
    OrderType,
    RiskDecision,
    RunStarted,
    Side,
    Signal,
    Target,
)
from auqua.contracts.identity import derived_id
from auqua.contracts.instruments import AnyInstrument
from auqua.contracts.numeric import arithmetic, on_grid
from auqua.execution.simulated import SimulatedVenue
from auqua.portfolio.book import Book
from auqua.risk.gate import PreTradeGate, TradingLimits
from auqua.storage.eventlog import EventLog
from auqua.storage.replay import EventLogReader
from auqua.visualization.report import ReportData, ReportRow

SOURCE = "research/backtest"
#: Namespace of ids of events derived by runs.
_EVENT_NAMESPACE = derived_id(uuid.NAMESPACE_URL, b"https://github.com/shahram-boshra/auqua#event")


class BacktestSpec(ConfigModel):
    """Parameters of one backtest (an experiment, not platform configuration).

    Attributes:
        fast_window: Bars in the fast moving average.
        slow_window: Bars in the slow moving average.
        quantity: Position taken while the signal is +1 (on the quantity grid).
        fee_bps: Fee in basis points of the notional.
        fee_increment: Grid of fee amounts.
        initial_cash: Starting cash in the quote currency.
    """

    fast_window: int = Field(gt=0)
    slow_window: int = Field(gt=0)
    quantity: Decimal = Field(gt=0)
    fee_bps: Decimal = Field(ge=0)
    fee_increment: Decimal = Field(gt=0)
    initial_cash: Decimal = Field(ge=0)


@dataclass
class BacktestResult:
    """Outcome of one run."""

    run_id: uuid.UUID
    run_log: Path
    started: RunStarted
    events: list[Event]
    book: Book
    final_mark: Decimal
    equity: Decimal
    appended: int
    duplicates: int


class _Run:
    """State of one run while the input is replayed (long or flat; spot)."""

    def __init__(
        self, instrument: AnyInstrument, spec: BacktestSpec, limits: TradingLimits, rid: uuid.UUID
    ) -> None:
        self.instrument = instrument
        self.spec = spec
        self.rid = rid
        self.clock = LogClock()
        self.signal = MovingAverageCrossover(spec.fast_window, spec.slow_window)
        self.gate = PreTradeGate(limits)
        self.venue = SimulatedVenue(spec.fee_bps, spec.fee_increment)
        self.book = Book(cash=spec.initial_cash)
        self.direction: int | None = None
        self.events: list[Event] = []
        self.mark = Decimal(0)

    def _id(self, cause: uuid.UUID, tag: str) -> uuid.UUID:
        return derived_id(_EVENT_NAMESPACE, self.rid.bytes, cause.bytes, tag.encode())

    def on_bar(self, bar: Bar) -> None:
        """Fill pending orders at this bar's open, then decide on its close."""
        self.clock.advance(bar.ts_ingest_ns)
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
        direction = self.signal.on_bar(bar)
        if direction is None or direction == self.direction:
            return
        self.direction = direction
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
            quantity=self.spec.quantity if direction > 0 else Decimal(0),
        )
        self.events += [signal, target]
        with arithmetic():
            delta = target.quantity - self.book.position
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
        check = self.gate.check(order.side, order.quantity, bar.close, self.book.position)
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


def run_backtest(
    reader: EventLogReader,
    instrument: AnyInstrument,
    spec: BacktestSpec,
    loaded: LoadedConfig[TradingLimits],
    *,
    code: str | None = None,
) -> BacktestResult:
    """Replay ``reader``'s bars of ``instrument`` through the skeleton and log every decision.

    Configuration, risk limits and their identities all come from ``loaded``
    (one source, so the recorded hashes always describe the limits applied);
    the run log goes to ``<output_root>/runs/<run_id>/`` of that configuration.
    ``code`` defaults to the identity of the imported package source.

    The run reads the input up to its head at the start (one snapshot). Its
    events are appended in one durable transaction at the end: a backtest has
    no external effects, so a run that stops early is simply run again, and
    a repeated run's identical records are counted as duplicates.
    ``RunStarted`` carries time 0: the run starts before the first input
    record has advanced its log clock.

    Raises:
        OffGridError: ``spec.quantity`` is not on the instrument's quantity grid.
    """
    on_grid(spec.quantity, instrument.quantity_increment)
    code = code or current_code_id()
    head = reader.head()
    spec_id = model_id(spec)
    instrument_sha256 = hashlib.sha256(msgspec.json.encode(instrument)).hexdigest()
    rid = run_id(
        head.hash.hex(),
        instrument_sha256,
        loaded.config_id,
        spec_id,
        loaded.risk_limits_sha256,
        code,
    )
    started = RunStarted(
        id=rid,
        source=SOURCE,
        ts_event_ns=0,
        ts_ingest_ns=0,
        input_head_hash=head.hash.hex(),
        input_next_sequence=head.next_sequence,
        instrument_id=instrument.id,
        instrument_sha256=instrument_sha256,
        config_id=loaded.config_id,
        spec_id=spec_id,
        risk_limits_sha256=loaded.risk_limits_sha256,
        code_id=code,
        code_version=code_version(),
    )
    run = _Run(instrument, spec, loaded.risk_limits.limits, rid)
    for record in reader.records():
        if record.sequence >= head.next_sequence:
            break
        event = record.event
        if isinstance(event, Bar) and event.instrument_id == instrument.id:
            run.on_bar(event)
    config = loaded.config
    run_log = config.paths.output_root / "runs" / str(rid) / "event_log.sqlite3"
    timeout = config.storage.busy_timeout_ms
    with EventLog(run_log, config.environment, busy_timeout_ms=timeout) as log:
        result = log.append([started, *run.events])
    return BacktestResult(
        run_id=rid,
        run_log=run_log,
        started=started,
        events=run.events,
        book=run.book,
        final_mark=run.mark,
        equity=run.book.equity(run.mark),
        appended=result.appended,
        duplicates=result.duplicates,
    )


def report_data(result: BacktestResult, spec: BacktestSpec) -> ReportData:
    """The static report's content for ``result`` (all values as exact decimal text)."""
    started = result.started
    decisions = {e.causation_id: e for e in result.events if isinstance(e, RiskDecision)}
    orders = [
        ReportRow((
            str(order.id), str(order.ts_ingest_ns), order.side.value, str(order.quantity),
            "accepted" if decisions[order.id].accepted else "rejected", decisions[order.id].rule,
        ))
        for order in result.events
        if isinstance(order, OrderSubmitted)
    ]  # fmt: skip
    fills = [
        ReportRow((
            str(fill.causation_id), str(fill.ts_event_ns), fill.side.value, str(fill.price),
            str(fill.quantity), f"{fill.fee} {fill.fee_currency}",
        ))
        for fill in result.events
        if isinstance(fill, Fill)
    ]  # fmt: skip
    with arithmetic():
        pnl = result.equity - spec.initial_cash
    return ReportData(
        title=f"Backtest {result.run_id}",
        lineage=[
            ("run id", str(result.run_id)),
            ("input log head hash", started.input_head_hash),
            ("input records", str(started.input_next_sequence)),
            ("config_id", started.config_id),
            ("spec_id", started.spec_id),
            ("risk limits SHA-256", started.risk_limits_sha256),
            ("code_id", started.code_id),
            ("code version", started.code_version),
        ],
        summary=[
            ("initial cash", str(spec.initial_cash)),
            ("final position", str(result.book.position)),
            ("final mark", str(result.final_mark)),
            ("equity", str(result.equity)),
            ("P&L (fees included)", str(pnl)),
            ("realized P&L (before fees)", str(result.book.realized)),
            ("fees", str(result.book.fees)),
        ],
        order_columns=("order id", "time (ns)", "side", "quantity", "risk", "rule"),
        orders=orders,
        fill_columns=("order id", "time (ns)", "side", "price", "quantity", "fee"),
        fills=fills,
    )


def replay_book(reader: EventLogReader, initial_cash: Decimal) -> Book:
    """Rebuild the book of a run from the fills in its log."""
    book = Book(cash=initial_cash)
    for record in reader.records():
        if isinstance(record.event, Fill):
            book.apply(record.event)
    return book
