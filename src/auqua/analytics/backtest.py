# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Single-configuration backtest (walking skeleton runner, rebuilt on BP5).

One historical path: canonical bars from a point-in-time snapshot of the input
log (:mod:`auqua.analytics.inputs`; ADR 0030) → baseline signal → portfolio
target → pre-trade risk gate → simulated venue fill → portfolio update →
static report, through :class:`auqua.analytics.engine.Simulation`. The input
log is read, never written. Every decision is a canonical event appended to
the run's own log (``<runs_root>/<run_id>/event_log.sqlite3``); the run log
starts with :class:`RunStartedV2`, which names the input snapshot and its
sources, the configuration, specification, risk limits, code and environment.
Ids of derived events are name-based (run id, causing event, tag), and every
timestamp comes from the log clock, so running the same inputs again produces
byte-identical records (the second run's appends are counted duplicates).
Experiments with several configurations, validation and the Research
Integrity Report are :mod:`auqua.analytics.experiment`.
"""

import hashlib
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import msgspec
from pydantic import Field

from auqua.analytics.engine import (
    SOURCE,
    RunMarket,
    Settings,
    Simulation,
    require_run_configuration,
    run_market,
)
from auqua.analytics.inputs import snapshot
from auqua.analytics.lineage import code_id as current_code_id
from auqua.analytics.lineage import code_version, environment, run_id
from auqua.config.base import ConfigModel
from auqua.config.identity import config_id as model_id
from auqua.config.loader import LoadedConfig
from auqua.contracts.events.base import Event
from auqua.contracts.events.research import RunStartedV2
from auqua.contracts.events.trading import Fill, OrderSubmitted, RiskDecision
from auqua.contracts.instruments import AnyInstrument
from auqua.contracts.numeric import arithmetic, on_grid
from auqua.contracts.portfolio import PostingKind
from auqua.execution.costs import FixedBps
from auqua.portfolio.account import Portfolio
from auqua.portfolio.allocation import FixedQuantitySpec
from auqua.risk.gate import TradingLimits
from auqua.storage.eventlog import EventLog
from auqua.storage.replay import EventLogReader
from auqua.visualization.report import ReportData, ReportRow


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
    started: RunStartedV2
    events: list[Event]
    portfolio: Portfolio
    instrument: AnyInstrument
    final_mark: Decimal
    equity: Decimal
    appended: int
    duplicates: int


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

    The run reads the input up to its head at the start (one point-in-time
    snapshot; its only bar series of ``instrument``). Its events are appended
    in one durable transaction at the end: a backtest has no external
    effects, so a run that stops early is simply run again, and a repeated
    run's identical records are counted as duplicates. ``RunStartedV2``
    carries time 0: the run starts before the first input record has
    advanced its log clock.

    Raises:
        OffGridError: ``spec.quantity`` is not on the instrument's quantity grid.
        InputError, NotEligibleError, PermitError: the input is not usable.
    """
    on_grid(spec.quantity, instrument.quantity_increment)
    allocation = FixedQuantitySpec(quantity=spec.quantity)
    code = code or current_code_id()
    config = loaded.config
    require_run_configuration(config.portfolio, instrument, allocation)
    inputs = snapshot(reader, instrument.id, config.ingestion)
    head = inputs.head
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
    env = environment()
    started = RunStartedV2(
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
        interval_ns=inputs.interval_ns,
        knowledge_ns=inputs.knowledge_ns,
        inputs=inputs.inputs,
        uv_lock_sha256=env.uv_lock_sha256,
        python_version=env.python_version,
    )
    settings = Settings(
        allocation=allocation,
        initial_cash=spec.initial_cash,
        costs=FixedBps(spec.fee_bps, Decimal(0), spec.fee_increment),
        limits=loaded.risk_limits.limits,
    )
    market = run_market(config.portfolio, inputs, instrument)
    run = Simulation(instrument, spec.fast_window, spec.slow_window, settings, rid, market)
    run.run(inputs.bars)
    run_log = config.paths.output_root / "runs" / str(rid) / "event_log.sqlite3"
    timeout = config.storage.busy_timeout_ms
    with EventLog(run_log, config.environment, busy_timeout_ms=timeout) as log:
        result = log.append([started, *run.events])
    return BacktestResult(
        run_id=rid,
        run_log=run_log,
        started=started,
        events=run.events,
        portfolio=run.portfolio,
        instrument=instrument,
        final_mark=run.mark,
        equity=run.equity_at(run.mark),
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
    quote = result.instrument.quote_currency
    with arithmetic():
        pnl = result.equity - spec.initial_cash
        fees = -sum(
            (
                p.amount
                for p in result.portfolio.postings
                if p.kind is PostingKind.FEE and p.currency == quote
            ),
            Decimal(0),
        )
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
            ("final position", str(result.portfolio.quantity(result.instrument.id))),
            ("final mark", str(result.final_mark)),
            ("equity", str(result.equity)),
            ("P&L (fees included)", str(pnl)),
            (
                "realized P&L (before fees)",
                str(result.portfolio.realized(result.instrument.id, quote)),
            ),
            ("fees", str(fees)),
        ],
        order_columns=("order id", "time (ns)", "side", "quantity", "risk", "rule"),
        orders=orders,
        fill_columns=("order id", "time (ns)", "side", "price", "quantity", "fee"),
        fills=fills,
    )


def replay_portfolio(reader: EventLogReader, market: RunMarket) -> Portfolio:
    """Rebuild a run's account by folding its run log (real log sequences and hashes).

    ``market`` is the run's (its input snapshot's view, instrument index and
    knowledge time); the run log itself holds the transfer, orders, decisions
    and fills.
    """
    portfolio = Portfolio(
        market.config, market.view, market.reference, knowledge_ns=market.knowledge_ns
    )
    for record, digest in reader.records_with_hashes():
        portfolio.apply(record, digest)
    return portfolio
