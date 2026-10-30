# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Research & backtest (BP5 section 9, K1-K10, K12 command line).

All bars are first-party synthetic data (a seeded random walk in whole ticks).
"""

import ast
import datetime
import hashlib
import json
import math
import random
import stat
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import msgspec
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from auqua.analytics import cli, integrity, stats
from auqua.analytics import robustness as rb
from auqua.analytics.engine import Settings, Simulation
from auqua.analytics.experiment import ExperimentError, evaluate_test, run_experiment
from auqua.analytics.experiment import _configuration as configuration_hash
from auqua.analytics.inputs import InputError, snapshot
from auqua.analytics.spec import ExperimentSpec
from auqua.analytics.splits import SplitError, purged_kfold, walk_forward
from auqua.analytics.trials import config_hash
from auqua.config.base import Environment
from auqua.config.loader import LoadedConfig, load
from auqua.contracts.events.base import Event
from auqua.contracts.events.market import Bar
from auqua.contracts.events.reference import InstrumentDefined
from auqua.contracts.events.research import RunStartedV2, Trial
from auqua.contracts.events.trading import Fill, OrderSubmitted, OrderType, Side, Signal
from auqua.contracts.identity import InstrumentId, derived_id
from auqua.contracts.instruments import CryptoSpot, Identifiers
from auqua.execution.costs import FixedBps
from auqua.execution.simulated import SimulatedVenue
from auqua.ingestion.errors import NotEligibleError
from auqua.portfolio.allocation import FixedQuantitySpec, VolatilityTargetSpec
from auqua.risk.gate import TradingLimits
from auqua.storage.eventlog import EventLog
from auqua.storage.marketview import available_ns
from auqua.storage.replay import EventLogReader

ENV = Environment.RESEARCH
TIMEOUT_MS = 5_000
MINUTE = 60_000_000_000
DAY = 1_440 * MINUTE
T0 = 1_788_220_800_000_000_000  # 2026-09-01T00:00:00Z
TICK = Decimal("0.01")
LOT = Decimal("0.001")
SYNTH = CryptoSpot(
    id=InstrumentId("SYNTH", "SYNUSD"),
    quote_currency="USD",
    price_increment=TICK,
    quantity_increment=LOT,
    multiplier=Decimal(1),
    valid_from_ns=0,
    knowledge_ns=0,
    identifiers=Identifiers(),
    base_currency="BTC",
)
NAMESPACE = derived_id(uuid.NAMESPACE_URL, b"https://github.com/shahram-boshra/auqua#test-bar")
CONFIG = """\
schema_version = 4
environment = "research"
[paths]
data_root = "{data}"
output_root = "{output}"
[ingestion]
jurisdiction = "US"
"""
LIMITS = """\
schema_version = 1
environment = "research"
[limits]
max_order_notional = 1000000.0
max_position = 10.0
"""


def utc(day: int) -> str:
    """ISO text of 2026-09-01 plus ``day`` days, UTC."""
    moment = datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC) + datetime.timedelta(days=day)
    return moment.isoformat()


def synthetic_bars(
    count: int, seed: int, *, source: str = "ingest/synthetic", instrument: CryptoSpot = SYNTH
) -> list[Bar]:
    """One-minute bars of a seeded random walk in whole ticks."""
    rng = random.Random(seed)  # noqa: S311 - reproducible test data
    close = 1_000_000
    bars = []
    for i in range(count):
        start = T0 + i * MINUTE
        open_ = close
        close = max(1_000, open_ + rng.randint(-40, 41))
        high, low = max(open_, close) + rng.randint(0, 10), min(open_, close) - rng.randint(0, 10)
        bars.append(
            Bar(
                id=derived_id(NAMESPACE, source.encode(), start.to_bytes(8, "big")),
                source=source,
                ts_event_ns=start + MINUTE,
                ts_ingest_ns=T0 + 10 * DAY,
                instrument_id=instrument.id,
                start_ns=start,
                interval_ns=MINUTE,
                open=Decimal(open_) * TICK,
                high=Decimal(high) * TICK,
                low=Decimal(low) * TICK,
                close=Decimal(close) * TICK,
                volume=Decimal(1),
            )
        )
    return bars


@dataclass
class Lab:
    """A loaded research configuration and an input log."""

    root: Path
    loaded: LoadedConfig[TradingLimits]
    input_log: Path

    def reader(self) -> EventLogReader:
        """A reader of the input log."""
        return EventLogReader(self.input_log, ENV, busy_timeout_ms=TIMEOUT_MS)

    def append(self, events: list[Event]) -> None:
        """Append to the input log."""
        with EventLog(self.input_log, ENV, busy_timeout_ms=TIMEOUT_MS) as log:
            log.append(events)


def make_lab(root: Path, bars: list[Bar]) -> Lab:
    """Configuration, risk limits and an input log holding ``bars``."""
    config_root, secrets_root = root / "config", root / "secrets"
    (config_root / "research").mkdir(parents=True)
    (config_root / "research" / "config.toml").write_text(
        CONFIG.format(data=root / "data", output=root / "out"), encoding="utf-8"
    )
    (config_root / "research" / "risk_limits.toml").write_text(LIMITS, encoding="utf-8")
    (secrets_root / "research").mkdir(parents=True, mode=0o700)
    loaded = load(
        ENV, config_root, secrets_root, limits_model=TradingLimits, secret_names=(), environ={}
    )
    lab = Lab(root, loaded, loaded.config.paths.data_root / "event_log.sqlite3")
    lab.append(list(bars))
    return lab


def spec(**changes: Any) -> ExperimentSpec:  # noqa: ANN401 - test overrides
    """A three-day experiment: train day 0, validation day 1, test day 2."""
    document: dict[str, Any] = {
        "hypothesis_id": "ma-crossover-synth",
        "hypothesis": "A moving-average crossover earns a positive risk-adjusted return.",
        "instrument_id": "SYNTH:SYNUSD",
        "train": {"start": utc(0), "end": utc(1)},
        "validation": {"start": utc(1), "end": utc(2)},
        "test": {"start": utc(2), "end": utc(3)},
        "signal": {"fast": [5, 10], "slow": [20, 40]},
        "quantity": "0.5",
        "initial_cash": "100000",
        "fee_bps": "1",
        "slippage_bps": "1",
        "fee_increment": "0.01",
        "validation_method": {"method": "walk_forward", "window_bars": 600, "step_bars": 300},
        "seed": 7,
    } | changes
    return ExperimentSpec.model_validate(document, strict=False)


@pytest.fixture(scope="module")
def lab(tmp_path_factory: pytest.TempPathFactory) -> Lab:
    """Three days of synthetic one-minute bars."""
    return make_lab(tmp_path_factory.mktemp("lab"), synthetic_bars(3 * 1_440, seed=11))


# --- K6 statistics ------------------------------------------------------------------------------


def test_dsr_reproduces_the_paper() -> None:
    """Bailey & Lopez de Prado's example: SR 2.5, T 1250, skew -3, kurt 10, V 0.5 (annual)."""
    sr = 2.5 / math.sqrt(250)
    skewed = stats.Moments(1250, 0.0, 1.0, -3.0, 10.0)
    normal = stats.Moments(1250, 0.0, 1.0, 0.0, 3.0)
    assert stats.dsr(sr, skewed, 0.5 / 250, 100) == pytest.approx(0.9004, abs=1e-4)
    assert stats.dsr(sr, skewed, 0.5 / 250, 46) == pytest.approx(0.9505, abs=1e-4)
    assert stats.dsr(sr, normal, 0.5 / 250, 88) == pytest.approx(0.9505, abs=1e-4)
    assert stats.expected_max_sharpe(0.5 / 250, 1) == 0.0


def test_lo_and_newey_west() -> None:
    """eta(q) = sqrt(q) without autocorrelation; the bandwidth rule; positive rho lowers SR."""
    assert stats.lo_eta(12, []) == pytest.approx(math.sqrt(12))
    assert stats.lo_eta(12, [0.2]) < math.sqrt(12)
    assert stats.newey_west_lags(100) == 4  # noqa: PLR2004
    assert stats.newey_west_lags(1250) == math.floor(4 * 12.5 ** (2 / 9))


def test_pbo_noise_and_dominance() -> None:
    """All-noise trials give PBO near 0.5 on average; one dominant trial gives PBO 0."""
    estimates = []
    for seed in range(30):
        noise_rng = random.Random(seed)  # noqa: S311
        noise = [[noise_rng.gauss(0, 1) for _ in range(10)] for _ in range(320)]
        estimates.append(stats.pbo_cscv(noise, 8).probability)
    assert 0.4 < sum(estimates) / len(estimates) < 0.7  # noqa: PLR2004
    rng = random.Random(3)  # noqa: S311
    dominant = [[rng.gauss(0.8 if n == 0 else 0.0, 1) for n in range(8)] for _ in range(640)]
    assert stats.pbo_cscv(dominant, 8).probability == 0.0
    with pytest.raises(stats.StatisticsError, match="two trials"):
        stats.pbo_cscv([[0.1]] * 64, 8)


def test_returns_are_the_float_boundary() -> None:
    """Exact equity in, floats out; drawdown stays exact."""
    equity = [Decimal(100), Decimal(110), Decimal(99)]
    assert stats.returns(equity) == [0.1, -0.1]
    assert stats.max_drawdown(equity) == Decimal("0.1")


def test_float_only_in_statistics() -> None:
    """Book, venue, cost model and engine contain no float (K7)."""
    root = Path(__file__).parent.parent / "src" / "auqua"
    for module in ("portfolio/book.py", "execution/simulated.py", "execution/costs.py",
                   "analytics/engine.py"):  # fmt: skip
        tree = ast.parse((root / module).read_text(encoding="utf-8"))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        floats = [
            n for n in ast.walk(tree) if isinstance(n, ast.Constant) and type(n.value) is float
        ]
        assert "float" not in names, module
        assert not floats, module


# --- K3 splits ----------------------------------------------------------------------------------


def test_walk_forward_rolling_and_anchored() -> None:
    """Exact windows (K3)."""
    rolling = walk_forward(10, 4, 3)
    assert [(s.train, s.test) for s in rolling] == [
        ((0, 1, 2, 3), (4, 5, 6)),
        ((3, 4, 5, 6), (7, 8, 9)),
    ]
    anchored = walk_forward(10, 4, 3, anchored=True)
    assert anchored[1].train == tuple(range(7))
    with pytest.raises(SplitError):
        walk_forward(5, 4, 3)


@settings(max_examples=60, deadline=None)
@given(st.integers(4, 60), st.integers(2, 6), st.integers(0, 5), st.integers(0, 5))
def test_purged_kfold_matches_an_oracle(n: int, k: int, horizon: int, embargo: int) -> None:
    """Training never overlaps a test label interval or its embargo (K3)."""
    if k > n:
        with pytest.raises(SplitError):
            purged_kfold(n, k, horizon=horizon, embargo=embargo)
        return
    splits = purged_kfold(n, k, horizon=horizon, embargo=embargo)
    assert sorted(i for s in splits for i in s.test) == list(range(n))
    for s in splits:
        start, end = s.test[0], s.test[-1]
        expected = [
            i
            for i in range(n)
            if not (i <= end + horizon and i + horizon >= start) and not (end < i <= end + embargo)
        ]
        assert list(s.train) == expected


# --- costs and venue ----------------------------------------------------------------------------


def test_cost_model_rounds_against_the_trader() -> None:
    """Slippage moves the price adversely and rounds to the grid against the trader."""
    model = FixedBps(Decimal(10), Decimal(3), Decimal("0.01"))
    assert model.fill_price(Side.BUY, Decimal("100.00"), TICK) == Decimal("100.03")
    assert model.fill_price(Side.SELL, Decimal("100.00"), TICK) == Decimal("99.97")
    assert model.fill_price(Side.BUY, Decimal("100.01"), TICK) == Decimal("100.05")  # 100.040003
    assert model.fee(Decimal("1000")) == Decimal("1.00")
    assert model.scaled(Decimal(2)).slippage_bps == Decimal(6)


def test_venue_latency_delays_fills() -> None:
    """With one bar of delay the order fills at the second bar's open."""
    bars = synthetic_bars(3, seed=1)
    venue = SimulatedVenue(FixedBps(Decimal(0), Decimal(0), TICK), TICK, delay_bars=1)
    order = OrderSubmitted(
        id=uuid.uuid7(),
        source="research/backtest",
        ts_event_ns=0,
        ts_ingest_ns=0,
        causation_id=uuid.uuid7(),
        instrument_id=SYNTH.id,
        side=Side.BUY,
        quantity=Decimal(1),
        order_type=OrderType.MARKET,
    )
    venue.submit(order)
    assert venue.on_bar(bars[0]) == []
    (fill,) = venue.on_bar(bars[1])
    assert fill.price == bars[1].open


@settings(max_examples=40, deadline=None)
@given(
    st.integers(30, 160),
    st.integers(0, 10_000),
    st.integers(2, 5),
    st.integers(6, 12),
    st.integers(0, 2),
    st.lists(st.integers(0, 5 * MINUTE), min_size=160, max_size=160),
)
def test_no_event_precedes_the_bars_it_follows(  # noqa: PLR0913, PLR0917 - hypothesis inputs
    count: int, seed: int, fast: int, slow: int, delay: int, lags: list[int]
) -> None:
    """Events are stamped at their bar's availability; orders in flight count (K1, latency)."""
    bars = [
        msgspec.structs.replace(bar, ts_published_ns=bar.ts_event_ns + lag)
        for bar, lag in zip(synthetic_bars(count, seed), lags, strict=False)
    ]
    ordered = sorted(bars, key=lambda bar: (available_ns(bar), bar.start_ns))
    limits = TradingLimits(max_order_notional=Decimal(10) ** 9, max_position=Decimal(10))
    costs = FixedBps(Decimal(1), Decimal(1), TICK)
    simulation = Simulation(
        SYNTH, fast, slow, Settings(Decimal("0.5"), Decimal(10) ** 6, costs, limits, delay),
        uuid.UUID(int=seed),
    )  # fmt: skip
    seen: set[uuid.UUID] = set()
    for bar in ordered:
        before = len(simulation.events)
        simulation.on_bar(bar)
        seen.add(bar.id)
        for event in simulation.events[before:]:
            assert event.ts_ingest_ns == available_ns(bar)
            if isinstance(event, Signal):
                assert event.causation_id in seen
        expected = simulation.book.position + simulation.venue.working(SYNTH.id)
        assert simulation.book.position >= 0
        assert expected in {Decimal(0), Decimal("0.5")}, "orders in flight count (latency)"


# --- K2 inputs ----------------------------------------------------------------------------------


def test_inputs_must_be_single_series_with_a_cleared_profile(tmp_path: Path) -> None:
    """No profile, several sources, or a source not cleared for the US: refused (K2)."""
    lab = make_lab(tmp_path / "a", synthetic_bars(10, 1, source="ingest/unknown"))
    with lab.reader() as reader, pytest.raises(InputError, match="no source profile"):
        snapshot(reader, SYNTH.id, lab.loaded.config.ingestion)
    binance = CryptoSpot(**{**_fields(SYNTH), "id": InstrumentId("BINANCE", "BTCUSDT")})
    lab = make_lab(tmp_path / "b", synthetic_bars(10, 1, source="ingest/binance/vision",
                                                    instrument=binance))  # fmt: skip
    with lab.reader() as reader, pytest.raises(NotEligibleError, match="not cleared"):
        snapshot(reader, binance.id, lab.loaded.config.ingestion)
    two = synthetic_bars(10, 1) + synthetic_bars(10, 2, source="ingest/coinbase/exchange")
    lab = make_lab(tmp_path / "c", two)
    with lab.reader() as reader:
        with pytest.raises(InputError, match="name one source"):
            snapshot(reader, SYNTH.id, lab.loaded.config.ingestion)
        chosen = snapshot(
            reader, SYNTH.id, lab.loaded.config.ingestion, event_source="ingest/synthetic"
        )
    assert {b.source for b in chosen.bars} == {"ingest/synthetic"}
    assert chosen.inputs[0].profile == "first-party/synthetic"


def _fields(instrument: CryptoSpot) -> dict[str, Any]:
    return {f: getattr(instrument, f) for f in instrument.__struct_fields__}


# --- K1, K4, K5, K8, K9, K10 experiment ----------------------------------------------------------


def test_experiment_report_and_determinism(lab: Lab) -> None:
    """Report checks present; same experiment twice gives the same bytes; N not doubled."""
    with lab.reader() as reader:
        first = run_experiment(reader, SYNTH, spec(), lab.loaded)
        data = first.report_json.read_bytes()
        second = run_experiment(reader, SYNTH, spec(), lab.loaded)
    assert second.report_json.read_bytes() == data  # K10
    report = json.loads(data)
    checks = {c["check"]: c for c in report["checks"]}
    assert set(checks) >= {
        "point_in_time_inputs",
        "inputs_permitted",
        "period_separation",
        "test_period_use",
        "multiple_testing",
        "deflated_sharpe_ratio",
        "backtest_overfitting",
        "robustness",
        "execution_assumptions",
        "universe",
    }
    assert checks["multiple_testing"]["value"]["trials"] == 4  # noqa: PLR2004 - K5
    assert report["terms"]["notice"] is None
    assert len(report["ranking"]) == 4  # noqa: PLR2004
    sweeps = {rb.COST_LOW, rb.COST_HIGH, rb.LATENCY, rb.PERTURBED, rb.FIRST_HALF, rb.SECOND_HALF}
    assert set(report["ranking"][0]["sweeps"]) >= sweeps
    assert report["pbo"]["groups"] == 16  # noqa: PLR2004
    assert report["selection_validation"]["method"] == "walk_forward"
    html = first.report_html.read_text(encoding="utf-8")
    assert "<script" not in html
    assert "Research Integrity Report" in html
    output = lab.loaded.config.paths.output_root
    for path in (output, output / "runs", first.report_json.parent):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700  # noqa: PLR2004 - BP5 section 6
    for path in (first.report_json, first.report_html):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600  # noqa: PLR2004


def test_run_record_and_look_ahead(lab: Lab) -> None:
    """The run record names hypothesis, periods and inputs; no signal precedes its bar (K1)."""
    with lab.reader() as reader:
        result = run_experiment(reader, SYNTH, spec(), lab.loaded)
    with EventLogReader(result.run_log, ENV, busy_timeout_ms=TIMEOUT_MS) as run:
        events = [r.event for r in run.records()]
    started = events[0]
    assert isinstance(started, RunStartedV2)
    assert started.hypothesis_id == "ma-crossover-synth"
    assert [p.name.value for p in started.periods] == ["train", "validation", "test"]
    assert started.inputs[0].bars == 3 * 1_440
    assert started.seed == 7  # noqa: PLR2004
    bars = {b.id: b for b in synthetic_bars(3 * 1_440, seed=11)}
    signals = [e for e in events if isinstance(e, Signal)]
    assert signals
    assert all(s.ts_ingest_ns >= available_ns(bars[s.causation_id]) for s in signals)
    assert all(s.ts_ingest_ns < T0 + 2 * DAY for s in signals), (
        "the experiment never reads the test period"
    )
    assert sum(1 for e in events if isinstance(e, Trial)) == 8  # noqa: PLR2004


def test_pinned_snapshot_hides_later_corrections(tmp_path: Path) -> None:
    """A rerun pinned to the earlier input reproduces it; a later correction is invisible (B1)."""
    bars = synthetic_bars(3 * 1_440, seed=11)
    lab = make_lab(tmp_path, bars)
    with lab.reader() as reader:
        first = run_experiment(reader, SYNTH, spec(), lab.loaded)
    data = first.report_json.read_bytes()
    pin = json.loads(data)["lineage"]["input_records"]
    late = bars[2 * 1_440 + 100]
    corrected = msgspec.structs.replace(
        late,
        id=derived_id(NAMESPACE, b"correction", late.id.bytes),
        ts_ingest_ns=late.ts_ingest_ns + DAY,
        close=late.high,
        revision=late.revision + 1,
    )
    lab.append([corrected])
    with lab.reader() as reader:
        ids = {b.id for b in snapshot(reader, SYNTH.id, lab.loaded.config.ingestion).bars}
        pinned = snapshot(reader, SYNTH.id, lab.loaded.config.ingestion, input_records=pin)
        assert corrected.id in ids
        assert corrected.id not in {b.id for b in pinned.bars}
        assert late.id in {b.id for b in pinned.bars}
        assert run_experiment(reader, SYNTH, spec(), lab.loaded).run_id != first.run_id
        again = run_experiment(reader, SYNTH, spec(), lab.loaded, input_records=pin)
        with pytest.raises(InputError, match="outside"):
            snapshot(reader, SYNTH.id, lab.loaded.config.ingestion, input_records=pin + 2)
    assert again.run_id == first.run_id
    assert again.report_json.read_bytes() == data


def test_test_period_is_counted_and_reuse_fails(tmp_path: Path) -> None:
    """First test use is out of sample; the second fails the claim (K4)."""
    lab = make_lab(tmp_path, synthetic_bars(3 * 1_440, seed=11))
    with lab.reader() as reader:
        first = evaluate_test(reader, SYNTH, spec(), lab.loaded, configuration=(5, 20))
        second = evaluate_test(reader, SYNTH, spec(), lab.loaded, configuration=(10, 40))
        with pytest.raises(ExperimentError, match="not in the grid"):
            evaluate_test(reader, SYNTH, spec(), lab.loaded, configuration=(7, 9))
    assert first.report["checks"][0]["status"] == "pass"
    assert second.report["checks"][0]["status"] == "fail"
    with lab.reader() as reader:
        report = run_experiment(reader, SYNTH, spec(), lab.loaded).report
    use = next(c for c in report["checks"] if c["check"] == "test_period_use")
    assert (use["value"], use["status"]) == (2, "warn")


def test_renamed_hypothesis_is_disclosed(tmp_path: Path) -> None:
    """The same configurations under another hypothesis id are listed (K5)."""
    lab = make_lab(tmp_path, synthetic_bars(3 * 1_440, seed=11))
    with lab.reader() as reader:
        run_experiment(reader, SYNTH, spec(), lab.loaded)
        renamed = run_experiment(reader, SYNTH, spec(hypothesis_id="other-name"), lab.loaded)
    check = next(c for c in renamed.report["checks"] if c["check"] == "hypothesis_renaming")
    assert (check["status"], check["value"]) == ("warn", ["ma-crossover-synth"])


def test_coinbase_inputs_carry_the_notice(tmp_path: Path) -> None:
    """Outputs of Coinbase data are marked not for redistribution (K9; ADR 0027)."""
    coin = CryptoSpot(**{**_fields(SYNTH), "id": InstrumentId("COINBASE", "BTC-USD")})
    bars = synthetic_bars(3 * 1_440, 5, source="ingest/coinbase/exchange", instrument=coin)
    lab = make_lab(tmp_path, bars)
    with lab.reader() as reader:
        result = run_experiment(reader, coin, spec(instrument_id="COINBASE:BTC-USD"), lab.loaded)
    assert result.report["terms"]["sharing"] == "prohibited"
    assert "Not for redistribution" in result.report_html.read_text(encoding="utf-8")


REPORT_SCHEMA = Path(__file__).parent / "snapshots" / "research_report.schema.json"


def test_report_schema_snapshot_and_refusal(lab: Lab, tmp_path: Path) -> None:
    """The report schema matches its snapshot; a report off the schema is not written (K9)."""
    schema = json.dumps(integrity.report_schema(), indent=2, sort_keys=True) + "\n"
    assert schema == REPORT_SCHEMA.read_text(encoding="utf-8")
    with lab.reader() as reader:
        report = run_experiment(reader, SYNTH, spec(), lab.loaded).report
    report["checks"][0]["status"] = "ok"
    with pytest.raises(integrity.ReportError, match="status"):
        integrity.write_report(tmp_path, "report", report)
    assert not list(tmp_path.iterdir())


def test_robust_ranking_prefers_the_worst_case() -> None:
    """A robust configuration ranks above a fragile one with a higher peak (K8)."""
    fragile = rb.Robustness(0.30, {rb.COST_HIGH: -0.01}, dsr_ok=True)
    steady = rb.Robustness(0.10, {rb.COST_HIGH: 0.08}, dsr_ok=True)
    assert (fragile.robust, steady.robust) == (False, True)
    assert steady.score > fragile.score
    assert rb.neighbours((10, 20), [(5, 20), (10, 20), (10, 40), (5, 40)]) == [(5, 20), (10, 40)]
    assert len(rb.drop_bars(synthetic_bars(1_000, 1), 0.1, seed=3)) < 1_000  # noqa: PLR2004


def test_spec_refuses_overlapping_periods() -> None:
    """Periods must be ordered and disjoint."""
    with pytest.raises(ValueError, match="ordered and disjoint"):
        spec(validation={"start": utc(0), "end": utc(2)})


VOLATILITY = {"method": "volatility_target", "target_volatility": "0.1", "leverage_cap": "2"}


def _v2(**changes: Any) -> ExperimentSpec:  # noqa: ANN401 - test overrides
    """A version-2 document: ``quantity`` replaced by ``allocation``, version stated."""
    document = spec().model_dump(mode="json") | changes
    return ExperimentSpec.model_validate(document, strict=False)


def test_spec_version_1_upgrades_to_version_2() -> None:
    """BP6 T14 (F-Z): no spec_version = version 1; quantity becomes fixed_quantity."""
    upgraded = spec()
    assert upgraded.spec_version == 2  # noqa: PLR2004
    assert upgraded.allocation == FixedQuantitySpec(quantity=Decimal("0.5"))
    assert _v2() == upgraded
    target = _v2(allocation=VOLATILITY).allocation
    assert isinstance(target, VolatilityTargetSpec)
    assert (target.half_life_days, target.warmup_days) == (20, 270)


def test_spec_versions_are_explicit() -> None:
    """Version 2 states its version; unknown, future or non-integer versions are refused."""
    document = spec().model_dump(mode="json")
    unversioned = {k: v for k, v in document.items() if k != "spec_version"}
    with pytest.raises(ValueError, match="version 1 has no allocation"):
        ExperimentSpec.model_validate(unversioned, strict=False)
    for version, message in ((3, "no migration"), ("2", "must be an integer")):
        with pytest.raises(ValueError, match=message):
            ExperimentSpec.model_validate(document | {"spec_version": version}, strict=False)
    with pytest.raises(ValueError, match="quantity"):
        spec(quantity=None)


def test_trial_configuration_hash_keeps_bp5_keys_for_fixed_quantity() -> None:
    """T15: unchanged hash for fixed_quantity; volatility targeting adds its parameters."""
    keys = {
        "provider": "baseline/ma-crossover",
        "fast": 5,
        "slow": 20,
        "fee_bps": "1",
        "slippage_bps": "1",
        "delay_bars": 0,
        "quantity": "0.5",
    }
    assert configuration_hash(spec(), 5, 20) == config_hash(keys)
    target = configuration_hash(_v2(allocation=VOLATILITY), 5, 20)
    assert target != config_hash(keys)
    assert target != configuration_hash(_v2(allocation=VOLATILITY | {"warmup_days": 9}), 5, 20)


def test_volatility_target_waits_for_s11(lab: Lab) -> None:
    """Until the engine runs on the account view (BP6 S11), volatility targeting is refused."""
    target = _v2(allocation=VOLATILITY)
    with lab.reader() as reader:
        with pytest.raises(ExperimentError, match="S11"):
            run_experiment(reader, SYNTH, target, lab.loaded)
        with pytest.raises(ExperimentError, match="S11"):
            evaluate_test(reader, SYNTH, target, lab.loaded, configuration=(5, 20))


SPEC_TOML = """\
hypothesis_id = "ma-crossover-synth"
hypothesis = "A moving-average crossover earns a positive risk-adjusted return."
instrument_id = "SYNTH:SYNUSD"
quantity = 0.5
initial_cash = 100000.0
fee_bps = 1.0
slippage_bps = 1.0
fee_increment = 0.01
seed = 7
[train]
start = 2026-09-01T00:00:00Z
end = 2026-09-02T00:00:00Z
[validation]
start = 2026-09-02T00:00:00Z
end = 2026-09-03T00:00:00Z
[test]
start = 2026-09-03T00:00:00Z
end = 2026-09-04T00:00:00Z
[signal]
fast = [5, 10]
slow = [20, 40]
[validation_method]
method = "walk_forward"
window_bars = 600
step_bars = 300
"""


def test_command_line_prints_statuses_and_hashes_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """K12: the CLI runs from TOML; output is run id, report hash and check statuses only."""
    defined = InstrumentDefined(
        id=derived_id(NAMESPACE, b"instrument"),
        source="ingest/synthetic",
        ts_event_ns=T0,
        ts_ingest_ns=T0,
        instrument=SYNTH,
    )
    lab = make_lab(tmp_path, synthetic_bars(3 * 1_440, seed=11))
    lab.append([defined])
    path = tmp_path / "spec.toml"
    path.write_text(SPEC_TOML, encoding="utf-8")
    roots = ["--config-root", str(tmp_path / "config"), "--secrets-root", str(tmp_path / "secrets")]
    argv = [*roots, "--environment", "research", "experiment", str(path)]
    assert cli.main(argv) == 0
    lines = capsys.readouterr().out.splitlines()
    run_id, records = lines[0].removeprefix("run_id=").split(" input_records=")
    assert lines[0] == f"run_id={run_id} input_records={int(records)}"
    name, digest = lines[1].removeprefix("report=").split(" sha256=")
    report = lab.loaded.config.paths.output_root / name
    assert name.startswith("runs/")
    assert hashlib.sha256(report.read_bytes()).hexdigest() == digest
    statuses = {line.split()[0] for line in lines[2:]}
    assert statuses <= {"pass", "warn", "fail"}
    assert len(lines[2:]) == len(json.loads(report.read_bytes())["checks"])
    pinned = [*roots, "--environment", "research", "--input-records", records]
    assert cli.main([*pinned, "experiment", str(path)]) == 0
    assert capsys.readouterr().out.splitlines()[:2] == lines[:2]
    assert cli.main([*argv[:-2], "test", str(path), "--fast", "5", "--slow", "20"]) == 0
    assert capsys.readouterr().out.splitlines()[-1].split()[1] == "test_period_use"
    wrong = SPEC_TOML.replace("SYNTH:SYNUSD", "SYNTH:OTHER")
    path.write_text(wrong, encoding="utf-8")
    assert cli.main(argv) == 1
    assert "no instrument definition" in capsys.readouterr().err


# --- BP6 T15: economic outputs, characterised before the portfolio change (PC-10, S0) -----------
ECONOMIC_BASELINE = Path(__file__).parent / "snapshots" / "bp5_economic_outputs.json"
#: Fixed run id for the per-configuration simulations: ids do not enter the economic outputs.
BASELINE_RUN = uuid.UUID(int=0)


def _equity_digest(equity: list[tuple[int, Decimal]]) -> dict[str, Any]:
    text = "\n".join(f"{t} {value}" for t, value in equity)
    return {
        "points": len(equity),
        "last": str(equity[-1][1]),
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
    }


def economic_outputs(lab: Lab) -> dict[str, Any]:
    """Fills, equity series and trial statistics of the BP5 synthetic experiment.

    Run ids and report bytes are excluded: they include ``code_id``, which
    changes with every code change (BP6 PC-10). Per-period Sharpe ratios are
    compared as the exact text of their binary values.
    """
    experiment = spec()
    assert isinstance(experiment.allocation, FixedQuantitySpec)
    bars = [b for b in synthetic_bars(3 * 1_440, seed=11) if available_ns(b) < T0 + 2 * DAY]
    costs = FixedBps(experiment.fee_bps, experiment.slippage_bps, experiment.fee_increment)
    settings = Settings(
        experiment.allocation.quantity,
        experiment.initial_cash,
        costs,
        lab.loaded.risk_limits.limits,
    )
    simulations: dict[str, Any] = {}
    for fast, slow in experiment.signal.configurations():
        simulation = Simulation(SYNTH, fast, slow, settings, BASELINE_RUN).run(bars)
        fills = [e for e in simulation.events if isinstance(e, Fill)]
        simulations[f"{fast}/{slow}"] = {
            "fills": [
                [f.side.value, str(f.quantity), str(f.price), str(f.fee), f.fee_currency]
                for f in fills
            ],
            "equity": _equity_digest(simulation.equity),
        }
    with lab.reader() as reader:
        result = run_experiment(reader, SYNTH, experiment, lab.loaded)
    with EventLogReader(result.run_log, ENV, busy_timeout_ms=TIMEOUT_MS) as run:
        trials = [r.event for r in run.records() if isinstance(r.event, Trial)]
    return {
        "simulations": simulations,
        "trials": sorted(
            [t.config_hash, t.period.value, t.observations, str(t.sharpe), t.returns_sha256]
            for t in trials
        ),
        "ranking": [
            {
                "configuration": c["configuration"],
                "validation_sharpe": repr(c["validation_sharpe"]),
                "sweeps": {name: repr(value) for name, value in sorted(c["sweeps"].items())},
            }
            for c in result.report["ranking"]
        ],
    }


def test_economic_outputs_match_the_baseline(tmp_path: Path) -> None:
    """BP6 T15: fills, equity series and trial statistics equal the pre-BP6 baseline."""
    lab = make_lab(tmp_path, synthetic_bars(3 * 1_440, seed=11))
    expected = json.loads(ECONOMIC_BASELINE.read_text(encoding="utf-8"))
    assert economic_outputs(lab) == expected
