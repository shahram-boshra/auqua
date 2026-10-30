# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Experiments: validation, trials, robustness and the integrity report (BP5; ADRs 0030-0032).

:func:`run_experiment` (train and validation only):

1. point-in-time input snapshot; every input permitted and cleared;
2. each configuration of the grid simulated over train + validation;
3. one trial per configuration and period in the run log and the ledger;
4. N and the variance of trial Sharpe ratios from the ledger (all runs);
5. robustness sweeps and the robust ranking; the selected configuration is
   the first of the ranking, not the highest raw Sharpe ratio;
6. DSR, PSR, PBO (CSCV), walk-forward or purged k-fold validation of the
   selection procedure, annualised Sharpe ratio for reporting;
7. ``report.json`` and ``report.html`` next to the run log.

:func:`evaluate_test` reads the test period for one configuration; it is the
only function that does, and each call is a counted trial.
"""

import dataclasses
import hashlib
import statistics
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import msgspec

from auqua.analytics import robustness as rb
from auqua.analytics import stats
from auqua.analytics.engine import (
    SOURCE,
    RunMarket,
    Settings,
    Simulation,
    require_run_configuration,
    run_market,
    run_rates,
)
from auqua.analytics.inputs import InputSnapshot, snapshot
from auqua.analytics.integrity import (
    ReportInputs,
    build_report,
    build_test_report,
    write_report,
)
from auqua.analytics.lineage import code_id as current_code_id
from auqua.analytics.lineage import code_version, environment, run_id
from auqua.analytics.spec import ExperimentSpec, PurgedKFold
from auqua.analytics.splits import Split, purged_kfold, walk_forward
from auqua.analytics.trials import LedgerView, TrialLedger, config_hash, make_trial
from auqua.config.identity import config_id as model_id
from auqua.config.loader import LoadedConfig
from auqua.contracts.events.market import Bar
from auqua.contracts.events.research import Period, PeriodName, RunStartedV2, Trial
from auqua.contracts.events.trading import Fill
from auqua.contracts.identity import InstrumentId
from auqua.contracts.instruments import AnyInstrument
from auqua.contracts.numeric import arithmetic
from auqua.execution.costs import FixedBps
from auqua.portfolio.allocation import FixedQuantitySpec
from auqua.portfolio.series import daily_last, periods_per_year
from auqua.risk.gate import TradingLimits
from auqua.storage.eventlog import EventLog
from auqua.storage.marketview import available_ns
from auqua.storage.replay import EventLogReader

_HALF = Decimal("0.5")
_DOUBLE = Decimal(2)
#: Run-id component of test evaluations (SHA-256 of the text "test-evaluation").
TEST_EVALUATION = hashlib.sha256(b"test-evaluation").hexdigest()


class ExperimentError(ValueError):
    """The experiment cannot be evaluated (too few bars or returns)."""


@dataclass(frozen=True)
class Series:
    """Equity after each bar of one simulation and the per-period returns.

    ``times[i]`` is the availability time of the bar after which ``equity[i + 1]``
    was recorded; ``equity[0]`` is the initial cash.
    """

    times: tuple[int, ...]
    equity: tuple[Decimal, ...]
    returns: tuple[float, ...]

    def within(self, period: Period) -> list[float]:
        """Returns whose bar lies in ``period``."""
        return [r for t, r in zip(self.times, self.returns, strict=True) if _inside(t, period)]

    def equity_within(self, period: Period) -> list[Decimal]:
        """Equity values recorded in ``period`` (with the value before its first bar)."""
        out: list[Decimal] = []
        for index, t in enumerate(self.times):
            if _inside(t, period):
                if not out:
                    out.append(self.equity[index])
                out.append(self.equity[index + 1])
        return out


def _inside(t: int, period: Period) -> bool:
    return period.start_ns <= t < period.end_ns


def _series(simulation: Simulation, initial_cash: Decimal) -> Series:
    times = tuple(t for t, _ in simulation.equity)
    equity = (initial_cash, *(e for _, e in simulation.equity))
    return Series(times, equity, tuple(stats.returns(equity)))


@dataclass
class Candidate:
    """One configuration: its simulation, returns and robustness."""

    fast: int
    slow: int
    hash: str
    simulation: Simulation
    series: Series
    train: list[float]
    validation: list[float]
    validation_sharpe: float
    robustness: rb.Robustness | None = None

    @property
    def label(self) -> str:
        """``fast/slow``."""
        return f"{self.fast}/{self.slow}"


def _configuration(spec: ExperimentSpec, fast: int, slow: int, delay: int = 0) -> str:
    """Trial configuration hash; for ``fixed_quantity`` the keys of BP5 (T15), unchanged."""
    allocation = spec.allocation
    sizing: dict[str, str | int]
    if isinstance(allocation, FixedQuantitySpec):
        sizing = {"quantity": str(allocation.quantity)}
    else:
        sizing = {
            "allocation": allocation.method,
            "target_volatility": str(allocation.target_volatility),
            "leverage_cap": str(allocation.leverage_cap),
            "half_life_days": allocation.half_life_days,
            "warmup_days": allocation.warmup_days,
        }
    return config_hash(
        {
            "provider": spec.signal.provider,
            "fast": fast,
            "slow": slow,
            "fee_bps": str(spec.fee_bps),
            "slippage_bps": str(spec.slippage_bps),
            "delay_bars": delay,
        }
        | sizing
    )


def _settings(
    spec: ExperimentSpec, limits: TradingLimits, factor: Decimal = Decimal(1)
) -> Settings:
    costs = FixedBps(spec.fee_bps, spec.slippage_bps, spec.fee_increment).scaled(factor)
    return Settings(spec.allocation, spec.initial_cash, costs, limits)


def _simulate(  # noqa: PLR0913 - one simulation's inputs
    instrument: AnyInstrument,
    bars: Sequence[Bar],
    *,
    fast: int,
    slow: int,
    settings: Settings,
    rid: uuid.UUID,
    market: RunMarket,
) -> Simulation:
    return Simulation(instrument, fast, slow, settings, rid, market).run(bars)


def _sharpe(values: Sequence[float]) -> float:
    try:
        return stats.sharpe(values)
    except stats.StatisticsError:
        return 0.0


def _bars_in(inputs: InputSnapshot, start_ns: int, end_ns: int) -> list[Bar]:
    return [b for b in inputs.bars if start_ns <= available_ns(b) < end_ns]


def _instrument_sha(instrument: AnyInstrument) -> str:
    return hashlib.sha256(msgspec.json.encode(instrument)).hexdigest()


def _run_id(
    inputs: InputSnapshot,
    instrument: AnyInstrument,
    spec: ExperimentSpec,
    loaded: LoadedConfig[TradingLimits],
    code: str,
    *extra: str,
) -> uuid.UUID:
    return run_id(
        inputs.head.hash.hex(),
        _instrument_sha(instrument),
        loaded.config_id,
        model_id(spec),
        loaded.risk_limits_sha256,
        code,
        *extra,
    )


def _started(  # noqa: PLR0913 - the record's sources
    rid: uuid.UUID,
    *,
    inputs: InputSnapshot,
    instrument: AnyInstrument,
    spec: ExperimentSpec,
    loaded: LoadedConfig[TradingLimits],
    code: str,
) -> RunStartedV2:
    env = environment()
    return RunStartedV2(
        id=rid,
        source=SOURCE,
        ts_event_ns=0,
        ts_ingest_ns=0,
        input_head_hash=inputs.head.hash.hex(),
        input_next_sequence=inputs.head.next_sequence,
        instrument_id=instrument.id,
        instrument_sha256=_instrument_sha(instrument),
        config_id=loaded.config_id,
        spec_id=model_id(spec),
        risk_limits_sha256=loaded.risk_limits_sha256,
        code_id=code,
        code_version=code_version(),
        hypothesis_id=spec.hypothesis_id,
        spec_version=spec.spec_version,
        interval_ns=inputs.interval_ns,
        knowledge_ns=inputs.knowledge_ns,
        periods=spec.periods(),
        inputs=inputs.inputs,
        uv_lock_sha256=env.uv_lock_sha256,
        python_version=env.python_version,
        seed=spec.seed,
    )


def _ledger(loaded: LoadedConfig[TradingLimits]) -> TrialLedger:
    config = loaded.config
    return TrialLedger(config.paths.output_root, config.environment, config.storage.busy_timeout_ms)


def _snapshot(
    reader: EventLogReader,
    spec: ExperimentSpec,
    loaded: LoadedConfig[TradingLimits],
    input_records: int | None,
    instrument: AnyInstrument,
) -> tuple[InputSnapshot, RunMarket]:
    """The input snapshot and the run's market (BP6 amendment 12, F-AE, F-AG).

    Raises:
        ConfigurationError: before the snapshot, a currency or rate the run
            needs is missing from ``portfolio``.
    """
    portfolio = loaded.config.portfolio
    require_run_configuration(portfolio, instrument, spec.allocation)
    inputs = snapshot(
        reader,
        InstrumentId.parse(spec.instrument_id),
        loaded.config.ingestion,
        interval_ns=spec.interval_ns,
        event_source=spec.event_source,
        input_records=input_records,
        rates=run_rates(portfolio, instrument, spec.allocation),
    )
    return inputs, run_market(portfolio, inputs, instrument)


def _daily(series: Series, period: Period) -> list[float]:
    """Daily returns in ``period``: equity dated by the availability of its bar (BP6 F-V)."""
    inside = [index for index, t in enumerate(series.times) if _inside(t, period)]
    if not inside:
        return []
    days = daily_last(
        [series.times[index] for index in inside], [series.equity[index + 1] for index in inside]
    )
    if len(days) < 3:  # noqa: PLR2004 - two daily returns at least
        return []
    return stats.returns([series.equity[inside[0]], *(value for _, value in days)])


def _selection_validation(
    candidates: Sequence[Candidate], period: Period, spec: ExperimentSpec
) -> dict[str, Any]:
    """Out-of-sample returns of the selection procedure (pick the best in-sample)."""
    rows = [
        [c.series.returns[i] for c in candidates]
        for i, t in enumerate(candidates[0].series.times)
        if t < period.end_ns
    ]
    method = spec.validation_method
    try:
        if isinstance(method, PurgedKFold):
            splits: list[Split] = purged_kfold(
                len(rows), method.k, horizon=method.horizon_bars, embargo=method.embargo_bars
            )
        else:
            splits = walk_forward(
                len(rows), method.window_bars, method.step_bars, anchored=method.anchored
            )
    except ValueError as error:
        return {"method": method.method, "status": "not computed", "reason": str(error)}
    oos: list[float] = []
    chosen: list[str] = []
    for split in splits:
        scores = [_sharpe([rows[i][n] for i in split.train]) for n in range(len(candidates))]
        best = max(range(len(candidates)), key=lambda n: (scores[n], -n))
        chosen.append(candidates[best].label)
        oos.extend(rows[i][best] for i in split.test)
    result: dict[str, Any] = {
        "method": method.method,
        "splits": len(splits),
        "selected_per_split": chosen,
        "out_of_sample_observations": len(oos),
        "out_of_sample_sharpe": _sharpe(oos),
    }
    try:
        result["out_of_sample_psr"] = stats.psr(_sharpe(oos), 0.0, stats.moments(oos))
    except stats.StatisticsError as error:
        result["out_of_sample_psr"] = None
        result["psr_note"] = str(error)
    return result


@dataclass(frozen=True)
class ExperimentResult:
    """Run id, run log, report paths, the selected configuration and the report."""

    run_id: uuid.UUID
    run_log: Path
    report_json: Path
    report_html: Path
    selected: str
    report: dict[str, Any]


def run_experiment(  # noqa: PLR0913, PLR0915 - the steps of the module docstring, in order
    reader: EventLogReader,
    instrument: AnyInstrument,
    spec: ExperimentSpec,
    loaded: LoadedConfig[TradingLimits],
    *,
    code: str | None = None,
    input_records: int | None = None,
) -> ExperimentResult:
    """Run the experiment on train and validation; write the run log and the report.

    ``input_records`` pins the input snapshot to an earlier head (a rerun of
    an earlier run reproduces it; BP5 scenario B1).

    Raises:
        ExperimentError: too few bars for statistics.
        InputError, NotEligibleError, PermitError: the input is not usable.
    """
    if str(instrument.id) != spec.instrument_id:
        raise ExperimentError(f"spec is for {spec.instrument_id}, not {instrument.id}")
    code = code or current_code_id()
    inputs, market = _snapshot(reader, spec, loaded, input_records, instrument)
    train, validation, _ = spec.periods()
    bars = _bars_in(inputs, train.start_ns, validation.end_ns)
    if len(bars) < 4:  # noqa: PLR2004 - two returns per period at least
        raise ExperimentError(f"{len(bars)} bars in train + validation")
    rid = _run_id(inputs, instrument, spec, loaded, code)
    limits = loaded.risk_limits.limits
    grid = spec.signal.configurations()
    candidates: list[Candidate] = []
    for fast, slow in grid:
        simulation = _simulate(
            instrument,
            bars,
            fast=fast,
            slow=slow,
            settings=_settings(spec, limits),
            rid=rid,
            market=market,
        )
        series = _series(simulation, spec.initial_cash)
        val = series.within(validation)
        candidates.append(
            Candidate(
                fast,
                slow,
                _configuration(spec, fast, slow),
                simulation,
                series,
                series.within(train),
                val,
                _sharpe(val),
            )
        )
    if any(len(c.validation) < 2 or len(c.train) < 2 for c in candidates):  # noqa: PLR2004
        raise ExperimentError("each period needs at least two returns")
    trials: list[Trial] = []
    for c in candidates:
        for period, values in ((PeriodName.TRAIN, c.train), (PeriodName.VALIDATION, c.validation)):
            trials.append(
                make_trial(
                    hypothesis_id=spec.hypothesis_id,
                    run_id=rid,
                    configuration=c.hash,
                    period=period,
                    values=values,
                    sharpe=_sharpe(values),
                    ts_ns=0,
                )
            )
    ledger = _ledger(loaded)
    ledger.append(trials)
    view: LedgerView = ledger.view(spec.hypothesis_id)
    n_trials = max(view.configurations(), 1)
    sharpes = view.validation_sharpes()
    variance = statistics.variance(sharpes) if len(sharpes) > 1 else 0.0
    threshold = float(spec.dsr_threshold)
    sweeps_spec = (
        (rb.COST_LOW, _HALF, 0, bars),
        (rb.COST_HIGH, _DOUBLE, 0, bars),
        (rb.LATENCY, Decimal(1), 1, bars),
        (rb.PERTURBED, Decimal(1), 0, rb.drop_bars(bars, float(spec.perturbation_drop), spec.seed)),
    )
    halves = _halves(validation)
    by_label = {(c.fast, c.slow): c for c in candidates}
    for c in candidates:
        sweeps: dict[str, float] = {}
        for name, factor, delay, sweep_bars in sweeps_spec:
            settings = _settings(spec, limits, factor)
            settings = dataclasses.replace(settings, delay_bars=delay)
            sim = _simulate(
                instrument,
                sweep_bars,
                fast=c.fast,
                slow=c.slow,
                settings=settings,
                rid=rid,
                market=market,
            )
            sweeps[name] = _sharpe(_series(sim, spec.initial_cash).within(validation))
        sweeps[rb.FIRST_HALF] = _sharpe(c.series.within(halves[0]))
        sweeps[rb.SECOND_HALF] = _sharpe(c.series.within(halves[1]))
        near = [by_label[n].validation_sharpe for n in rb.neighbours((c.fast, c.slow), grid)]
        if near:
            sweeps[rb.NEIGHBOURS] = min(near)
        c.robustness = rb.Robustness(
            c.validation_sharpe, sweeps, _dsr(c, variance, n_trials) >= threshold
        )
    ranking = sorted(
        candidates,
        key=lambda c: (
            not (c.robustness and c.robustness.robust),
            -(c.robustness.score if c.robustness else 0.0),
            c.label,
        ),
    )
    selected = ranking[0]
    matrix = [
        [c.series.returns[i] for c in candidates]
        for i, t in enumerate(selected.series.times)
        if _inside(t, validation)
    ]
    try:
        pbo: dict[str, Any] = {
            "probability": stats.pbo_cscv(matrix, spec.pbo_groups).probability,
            "groups": spec.pbo_groups,
            "trials": len(candidates),
        }
    except stats.StatisticsError as error:
        pbo = {"probability": None, "reason": str(error), "trials": len(candidates)}
    started = _started(
        rid, inputs=inputs, instrument=instrument, spec=spec, loaded=loaded, code=code
    )
    config = loaded.config
    run_dir = config.paths.output_root / "runs" / str(rid)
    run_log = run_dir / "event_log.sqlite3"
    with EventLog(
        run_log, config.environment, busy_timeout_ms=config.storage.busy_timeout_ms
    ) as log:
        log.append([started, *trials, *selected.simulation.events])
    report = build_report(
        ReportInputs(
            started=started,
            spec=spec,
            instrument=instrument,
            inputs=inputs,
            profiles=inputs.profiles,
            candidates=[
                {
                    "configuration": c.label,
                    "config_hash": c.hash,
                    "validation_sharpe": c.validation_sharpe,
                    "robust": bool(c.robustness and c.robustness.robust),
                    "robust_score": c.robustness.score if c.robustness else None,
                    "sweeps": c.robustness.sweeps if c.robustness else {},
                }
                for c in ranking
            ],
            selected=_selected_metrics(
                selected,
                validation=validation,
                variance=variance,
                trials=n_trials,
                instrument=instrument,
                spec=spec,
            ),
            trials=n_trials,
            trial_variance=variance,
            test_uses=view.test_uses(),
            pbo=pbo,
            selection_validation=_selection_validation(candidates, validation, spec),
            similar_hypotheses=ledger.hypotheses_like(
                spec.hypothesis_id, {c.hash for c in candidates}
            ),
            gaps_in_periods=sum(
                1 for g in inputs.gaps if train.start_ns <= g.start_ns < validation.end_ns
            ),
        )
    )
    json_path, html_path = write_report(run_dir, "report", report)
    return ExperimentResult(rid, run_log, json_path, html_path, selected.label, report)


def _halves(period: Period) -> tuple[Period, Period]:
    middle = (period.start_ns + period.end_ns) // 2
    return (
        Period(period.name, period.start_ns, middle),
        Period(period.name, middle, period.end_ns),
    )


def _dsr(c: Candidate, variance: float, trials: int) -> float:
    try:
        return stats.dsr(c.validation_sharpe, stats.moments(c.validation), variance, trials)
    except stats.StatisticsError:
        return 0.0


def _selected_metrics(  # noqa: PLR0913 - the metrics' inputs
    c: Candidate,
    *,
    validation: Period,
    variance: float,
    trials: int,
    instrument: AnyInstrument,
    spec: ExperimentSpec,
) -> dict[str, Any]:
    out: dict[str, Any] = {"configuration": c.label, "config_hash": c.hash}
    try:
        m = stats.moments(c.validation)
        out |= {
            "observations": m.n,
            "sharpe_per_bar": c.validation_sharpe,
            "skewness": m.skew,
            "kurtosis": m.kurt,
            "psr_vs_0": stats.psr(c.validation_sharpe, 0.0, m),
            "expected_max_sharpe": stats.expected_max_sharpe(variance, trials),
            "dsr": stats.dsr(c.validation_sharpe, m, variance, trials),
        }
    except stats.StatisticsError as error:
        out["statistics_note"] = str(error)
    daily = _daily(c.series, validation)
    year = periods_per_year(instrument.asset_class)
    try:
        out["annualised_sharpe_daily_lo"] = stats.annualised_sharpe(daily, year) if daily else None
    except stats.StatisticsError as error:
        out["annualised_sharpe_daily_lo"] = None
        out["annualisation_note"] = str(error)
    out["daily_returns"] = len(daily)
    equity = c.series.equity_within(validation)
    out["max_drawdown"] = str(stats.max_drawdown(equity)) if equity else None
    fills = [
        e for e in c.simulation.events if isinstance(e, Fill) and _inside(e.ts_event_ns, validation)
    ]
    with arithmetic():
        traded = sum((f.price * f.quantity for f in fills), Decimal(0))
        out["turnover"] = str(traded / spec.initial_cash)
        out["fees"] = str(sum((f.fee for f in fills), Decimal(0)))
    out["fills"] = len(fills)
    return out


@dataclass(frozen=True)
class TestResult:
    """Outcome of one evaluation on the test period."""

    run_id: uuid.UUID
    report_json: Path
    report_html: Path
    report: dict[str, Any]


def evaluate_test(  # noqa: PLR0913 - the experiment plus the configuration to test
    reader: EventLogReader,
    instrument: AnyInstrument,
    spec: ExperimentSpec,
    loaded: LoadedConfig[TradingLimits],
    *,
    configuration: tuple[int, int],
    code: str | None = None,
    input_records: int | None = None,
) -> TestResult:
    """Evaluate ``configuration`` on the test period; a counted, one-time use (BP5 section 4.1).

    Raises:
        ExperimentError: the configuration is not in the grid or too few test returns.
    """
    if configuration not in spec.signal.configurations():
        raise ExperimentError(f"{configuration} is not in the grid")
    code = code or current_code_id()
    inputs, market = _snapshot(reader, spec, loaded, input_records, instrument)
    train, _, test = spec.periods()
    bars = _bars_in(inputs, train.start_ns, test.end_ns)
    fast, slow = configuration
    configuration_hash = _configuration(spec, fast, slow)
    rid = _run_id(inputs, instrument, spec, loaded, code, TEST_EVALUATION, configuration_hash)
    settings = _settings(spec, loaded.risk_limits.limits)
    simulation = _simulate(
        instrument, bars, fast=fast, slow=slow, settings=settings, rid=rid, market=market
    )
    values = _series(simulation, spec.initial_cash).within(test)
    if len(values) < 2:  # noqa: PLR2004
        raise ExperimentError("the test period needs at least two returns")
    sharpe = _sharpe(values)
    trial = make_trial(
        hypothesis_id=spec.hypothesis_id,
        run_id=rid,
        configuration=configuration_hash,
        period=PeriodName.TEST,
        values=values,
        sharpe=sharpe,
        ts_ns=0,
    )
    ledger = _ledger(loaded)
    ledger.append([trial])
    uses = ledger.view(spec.hypothesis_id).test_uses()
    started = _started(
        rid, inputs=inputs, instrument=instrument, spec=spec, loaded=loaded, code=code
    )
    config = loaded.config
    run_dir = config.paths.output_root / "runs" / str(rid)
    with EventLog(
        run_dir / "event_log.sqlite3",
        config.environment,
        busy_timeout_ms=config.storage.busy_timeout_ms,
    ) as log:
        log.append([started, trial, *simulation.events])
    try:
        psr: float | None = stats.psr(sharpe, 0.0, stats.moments(values))
    except stats.StatisticsError:
        psr = None
    report = build_test_report(
        started=started,
        profiles=inputs.profiles,
        configuration=f"{fast}/{slow}",
        config_hash=configuration_hash,
        test=test,
        values=values,
        sharpe=sharpe,
        psr=psr,
        uses=uses,
    )
    json_path, html_path = write_report(run_dir, "test_report", report)
    return TestResult(rid, json_path, html_path, report)
