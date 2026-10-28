# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Walking skeleton: the three BP0 A5 exit criteria and its thin components (ADR 0025).

Synthetic first-party data only (decision log a20, W-1a); every file is
created in a temporary directory.
"""

import hashlib
import random
import shutil
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import msgspec
import pytest

from auqua.alpha.baseline import MovingAverageCrossover
from auqua.analytics.backtest import (
    BacktestResult,
    BacktestSpec,
    replay_book,
    report_data,
    run_backtest,
)
from auqua.analytics.lineage import code_id
from auqua.config.base import Environment
from auqua.config.loader import LoadedConfig, load
from auqua.contracts.events.base import Event
from auqua.contracts.events.market import Bar
from auqua.contracts.events.research import RunStartedV2
from auqua.contracts.events.trading import (
    Fill,
    OrderSubmitted,
    OrderType,
    RiskDecision,
    Side,
    Signal,
    Target,
)
from auqua.contracts.identity import InstrumentId, derived_id
from auqua.contracts.instruments import CryptoSpot, Identifiers
from auqua.execution.costs import FixedBps
from auqua.execution.simulated import SimulatedVenue
from auqua.ingestion.parsers.binance_vision_klines import KlineFormatError, TimeUnit, read_klines
from auqua.portfolio.book import Book
from auqua.risk.gate import MAX_ORDER_NOTIONAL, MAX_POSITION, PreTradeGate, TradingLimits
from auqua.storage.eventlog import EventLog
from auqua.storage.replay import EventLogReader
from auqua.visualization.report import ReportData, ReportRow, render, write_report

# Synthetic data (W-1a) -----------------------------------------------------------------------
# A seeded random walk in whole price ticks, written in the published 12-column kline
# layout with microsecond times. Only ``random.Random(seed).random()`` is used: the one
# generator method whose sequence Python guarantees for a given seed across versions.

#: Fictional instrument (no real venue or symbol).
INSTRUMENT = CryptoSpot(
    id=InstrumentId("SYNTH", "SYNUSD"),
    quote_currency="USD",
    price_increment=Decimal("0.01"),
    quantity_increment=Decimal("0.001"),
    multiplier=Decimal(1),
    valid_from_ns=0,
    knowledge_ns=0,
    identifiers=Identifiers(),
    base_currency="SYN",
)
START_US = 1_735_689_600_000_000  # 2025-01-01T00:00:00Z
MINUTE_US = 60_000_000
MINUTE_NS = MINUTE_US * 1_000
TICK = Decimal("0.01")
LOT = Decimal("0.001")


def _ticks(generator: random.Random, spread: int) -> int:
    """An integer in ``-spread..spread`` from one ``random()`` draw."""
    return int(generator.random() * (2 * spread + 1)) - spread


def write_klines(path: Path, bars: int, seed: int, start_price_ticks: int = 1_000_000) -> Path:
    """Write ``bars`` one-minute klines; returns ``path``."""
    generator = random.Random(seed)  # noqa: S311 - reproducible test data, not cryptography
    close = start_price_ticks
    lines = []
    for index in range(bars):
        open_ = close
        close = max(1, open_ + _ticks(generator, 40))
        high = max(open_, close) + int(generator.random() * 15)
        low = max(1, min(open_, close) - int(generator.random() * 15))
        volume = 1 + int(generator.random() * 5_000_000)
        open_time = START_US + index * MINUTE_US
        prices = ",".join(str(Decimal(t) * TICK) for t in (open_, high, low, close))
        lines.append(
            f"{open_time},{prices},{Decimal(volume) * LOT},{open_time + MINUTE_US - 1},0,0,0,0,0"
        )
    path.write_text("\n".join(lines) + "\n", encoding="ascii")
    return path


ENV = Environment.RESEARCH
TIMEOUT_MS = 5_000
BARS = 7_200  # five days of one-minute bars
SPEC = BacktestSpec(
    fast_window=20,
    slow_window=60,
    quantity=Decimal("0.500"),
    fee_bps=Decimal(10),
    fee_increment=Decimal("0.01"),
    initial_cash=Decimal(100_000),
)
CONFIG = """\
schema_version = 4
environment = "research"

[paths]
data_root = "{data}"
output_root = "{output}"
"""
LIMITS = """\
schema_version = 1
environment = "research"

[limits]
max_order_notional = {notional}
max_position = 1.0
"""
# Decimal limits are written as TOML floats: they load as exact decimals (ADR 0018),
# while a TOML integer is refused for a decimal field by the strict models.


class SteppingClock:
    """Ingestion clock for tests: one microsecond per reading, from a fixed start."""

    def __init__(self, start_ns: int) -> None:
        """Start at ``start_ns``."""
        self._now = start_ns

    def now_ns(self) -> int:
        """Advance and return the time."""
        self._now += 1_000
        return self._now


@dataclass
class Setup:
    """Loaded configuration and an input log holding the synthetic bars."""

    loaded: LoadedConfig[TradingLimits]
    input_log: Path


def _setup(root: Path, notional: str = "1000000.00") -> Setup:
    config_root, secrets_root = root / "config", root / "secrets"
    (config_root / "research").mkdir(parents=True)
    data, output = root / "data", root / "output"
    (config_root / "research" / "config.toml").write_text(
        CONFIG.format(data=data, output=output), encoding="utf-8"
    )
    (config_root / "research" / "risk_limits.toml").write_text(
        LIMITS.format(notional=notional), encoding="utf-8"
    )
    (secrets_root / "research").mkdir(parents=True, mode=0o700)
    loaded = load(
        ENV,
        config_root,
        secrets_root,
        limits_model=TradingLimits,
        secret_names=(),
        environ={},
    )
    klines = write_klines(root / "klines.csv", BARS, seed=7)
    input_log = loaded.config.paths.data_root / "event_log.sqlite3"
    bars = read_klines(
        klines,
        INSTRUMENT,
        source="ingest/synthetic",
        interval_ns=MINUTE_NS,
        time_unit=TimeUnit.MICROSECONDS,
        clock=SteppingClock(1_760_000_000_000_000_000),
    )
    with EventLog(input_log, ENV, busy_timeout_ms=TIMEOUT_MS) as log:
        assert log.append(bars).appended == BARS
    return Setup(loaded, input_log)


def _run(setup: Setup, spec: BacktestSpec = SPEC) -> BacktestResult:
    with EventLogReader(setup.input_log, ENV, busy_timeout_ms=TIMEOUT_MS) as reader:
        return run_backtest(reader, INSTRUMENT, spec, setup.loaded)


def _of[E: Event](events: list[Event], kind: type[E]) -> list[E]:
    return [e for e in events if isinstance(e, kind)]


@pytest.fixture(scope="module")
def setup(tmp_path_factory: pytest.TempPathFactory) -> Setup:
    """One input log and configuration shared by the exit-criteria tests."""
    return _setup(tmp_path_factory.mktemp("skeleton"))


# Exit criterion 1: replay reproduces identical orders and P&L -------------------------------


def test_replay_reproduces_identical_orders_and_pnl(setup: Setup) -> None:
    """A second run on the same log yields byte-identical records and the same P&L."""
    first = _run(setup)
    second = _run(setup)
    assert len(_of(first.events, Fill)) > 2  # noqa: PLR2004 - the path really trades
    assert first.run_id == second.run_id
    assert first.events == second.events
    assert (second.appended, second.duplicates) == (0, len(first.events) + 1)
    assert (first.equity, first.book.realized, first.book.fees) == (
        second.equity,
        second.book.realized,
        second.book.fees,
    )
    with EventLogReader(first.run_log, ENV, busy_timeout_ms=TIMEOUT_MS) as reader:
        reader.verify()
        replayed = replay_book(reader, SPEC.initial_cash)
    assert (replayed.cash, replayed.position, replayed.realized, replayed.fees) == (
        first.book.cash,
        first.book.position,
        first.book.realized,
        first.book.fees,
    )
    assert replayed.equity(first.final_mark) == first.equity
    other = _run(setup, SPEC.model_copy(update={"fast_window": 10}))
    assert other.run_id != first.run_id
    assert other.run_log != first.run_log


# Exit criterion 2: an order breaching a risk limit is rejected ------------------------------


def test_order_breaching_a_limit_is_rejected(tmp_path: Path) -> None:
    """With a notional limit below every order, every order is rejected and nothing fills."""
    result = _run(_setup(tmp_path, notional="100.00"))
    decisions = _of(result.events, RiskDecision)
    assert decisions
    assert all(not d.accepted and d.rule == MAX_ORDER_NOTIONAL for d in decisions)
    assert all(d.limit == Decimal(100) and d.value is not None for d in decisions)
    assert not _of(result.events, Fill)
    assert (result.book.cash, result.book.position) == (SPEC.initial_cash, Decimal(0))


# Exit criterion 3: every order traces to signal, data, configuration and code ---------------


def test_every_order_traces_to_signal_data_configuration_and_code(setup: Setup) -> None:
    """Order → target → signal → bar in the input log; the run record names the rest."""
    result = _run(setup)
    by_id: dict[uuid.UUID, Event] = {e.id: e for e in result.events}
    with EventLogReader(setup.input_log, ENV, busy_timeout_ms=TIMEOUT_MS) as reader:
        bars = {r.event.id for r in reader.records() if isinstance(r.event, Bar)}
        head = reader.head()
    orders = _of(result.events, OrderSubmitted)
    assert orders
    for order in orders:
        target = by_id[order.causation_id]
        assert isinstance(target, Target)
        signal = by_id[target.causation_id]
        assert isinstance(signal, Signal)
        assert signal.causation_id in bars
    for fill in _of(result.events, Fill):
        assert isinstance(by_id[fill.causation_id], OrderSubmitted)
    with EventLogReader(result.run_log, ENV, busy_timeout_ms=TIMEOUT_MS) as reader:
        started = next(reader.records()).event
    assert isinstance(started, RunStartedV2)
    assert started.id == result.run_id
    assert started.input_head_hash == head.hash.hex()
    assert started.config_id == setup.loaded.config_id
    assert started.risk_limits_sha256 == setup.loaded.risk_limits_sha256
    assert started.code_id == code_id()
    assert started.instrument_id == INSTRUMENT.id
    assert started.instrument_sha256 == hashlib.sha256(msgspec.json.encode(INSTRUMENT)).hexdigest()
    page = render(report_data(result, SPEC))
    for value in (str(result.run_id), started.input_head_hash, started.config_id, started.code_id):
        assert value in page


def test_report_is_written_and_escaped(setup: Setup, tmp_path: Path) -> None:
    """The report is one self-contained file; text is escaped."""
    path = write_report(tmp_path / "report.html", report_data(_run(setup), SPEC))
    page = path.read_text(encoding="utf-8")
    assert page.startswith("<!doctype html>")
    assert "<script" not in page
    assert "http" not in page
    hostile = ReportData("<b>", [("k", "<script>")], [], ("c",), [ReportRow(("&",))], ("c",), [])
    assert "<script>" not in render(hostile)
    assert "&lt;script&gt;" in render(hostile)


# Code identity ----------------------------------------------------------------------------


def test_code_id_tracks_source_content(tmp_path: Path) -> None:
    """``code_id`` changes with any source byte and ignores ``__pycache__``."""
    import auqua  # noqa: PLC0415 - locate the imported package

    copy = tmp_path / "auqua"
    shutil.copytree(Path(auqua.__file__).parent, copy)
    baseline = code_id(copy)
    assert baseline == code_id()
    (copy / "__pycache__").mkdir(exist_ok=True)
    (copy / "__pycache__" / "x.pyc").write_bytes(b"cache")
    assert code_id(copy) == baseline
    with (copy / "__init__.py").open("a", encoding="utf-8") as handle:
        handle.write("\n")
    assert code_id(copy) != baseline


# Thin components --------------------------------------------------------------------------


def test_derived_ids_are_deterministic_uuidv8() -> None:
    """Name-based UUIDv8: equal inputs, equal ids; part boundaries matter."""
    namespace = uuid.UUID(int=1)
    first = derived_id(namespace, b"ab", b"c")
    assert first == derived_id(namespace, b"ab", b"c")
    assert first.version == 8  # noqa: PLR2004
    assert first.variant == uuid.RFC_4122
    assert first != derived_id(namespace, b"a", b"bc")
    assert first != derived_id(uuid.UUID(int=2), b"ab", b"c")


@pytest.mark.parametrize(
    ("row", "match"),
    [
        ("1,2,3", "12 columns"),
        ("x,1.00,1.00,1.00,1.00,1.000,59999999,0,0,0,0,0", "integers"),
        ("0,1.00,1.00,1.00,1.00,1.000,59999998,0,0,0,0,0", "interval"),
        ("0,1.005,1.01,1.00,1.00,1.000,59999999,0,0,0,0,0", "multiple"),
        ("0,1.00,1.00,1.01,1.00,1.000,59999999,0,0,0,0,0", "low"),
    ],
)
def test_kline_rows_refused(tmp_path: Path, row: str, match: str) -> None:
    """Wrong layout, wrong interval, off-grid values and invalid bars are refused."""
    path = tmp_path / "bad.csv"
    path.write_text(row + "\n", encoding="ascii")
    with pytest.raises(KlineFormatError, match=match):
        list(
            read_klines(
                path,
                INSTRUMENT,
                source="ingest/synthetic",
                interval_ns=MINUTE_NS,
                time_unit=TimeUnit.MICROSECONDS,
                clock=SteppingClock(0),
            )
        )


def test_klines_parse_exactly_with_derived_ids(tmp_path: Path) -> None:
    """Values arrive on the grid in canonical form; ids are stable across readings."""
    path = write_klines(tmp_path / "k.csv", 3, seed=1)

    def read() -> list[Bar]:
        return list(
            read_klines(
                path,
                INSTRUMENT,
                source="ingest/synthetic",
                interval_ns=MINUTE_NS,
                time_unit=TimeUnit.MICROSECONDS,
                clock=SteppingClock(0),
            )
        )

    first, again = read(), read()
    assert [b.id for b in first] == [b.id for b in again]
    assert first[0].start_ns == START_US * 1_000
    assert first[0].ts_event_ns == first[0].start_ns + MINUTE_NS
    assert first[0].open.as_tuple().exponent == -2  # noqa: PLR2004


def _bar(index: int, close: str, open_: str | None = None) -> Bar:
    price = Decimal(close)
    start = index * MINUTE_NS
    return Bar(
        id=uuid.UUID(int=index + 1),
        source="ingest/synthetic",
        ts_event_ns=start + MINUTE_NS,
        ts_ingest_ns=start + MINUTE_NS,
        instrument_id=INSTRUMENT.id,
        start_ns=start,
        interval_ns=MINUTE_NS,
        open=Decimal(open_) if open_ else price,
        high=max(price, Decimal(open_ or close)),
        low=min(price, Decimal(open_ or close)),
        close=price,
        volume=Decimal(1),
    )


def test_crossover_is_long_or_flat() -> None:
    """No direction before the slow window fills; +1 when fast > slow, else 0."""
    signal = MovingAverageCrossover(2, 3)
    assert [signal.on_bar(_bar(i, c)) for i, c in enumerate(["10", "10", "13", "7"])] == [
        None,
        None,
        1,
        0,
    ]
    with pytest.raises(ValueError, match="fast < slow"):
        MovingAverageCrossover(3, 3)


def _order(side: Side, quantity: str) -> OrderSubmitted:
    return OrderSubmitted(
        id=uuid.UUID(int=99),
        source="research/backtest",
        ts_event_ns=0,
        ts_ingest_ns=0,
        causation_id=uuid.UUID(int=98),
        instrument_id=INSTRUMENT.id,
        side=side,
        quantity=Decimal(quantity),
        order_type=OrderType.MARKET,
    )


def test_venue_fills_at_next_open_with_rounded_fee() -> None:
    """An order fills at the open of the next bar, never at a known price."""
    venue = SimulatedVenue(FixedBps(Decimal(10), Decimal(0), Decimal("0.01")), Decimal("0.01"))
    venue.submit(_order(Side.BUY, "0.5"))
    (execution,) = venue.on_bar(_bar(1, "101.00", open_="100.37"))
    assert (execution.price, execution.ts_event_ns) == (Decimal("100.37"), MINUTE_NS)
    assert execution.fee == Decimal("0.05")  # 100.37 * 0.5 * 10 bps = 0.050185 -> 0.05
    assert venue.on_bar(_bar(2, "1")) == []


def _fill(side: Side, price: str, quantity: str, fee: str = "0") -> Fill:
    return Fill(
        id=uuid.uuid7(),
        source="research/backtest",
        ts_event_ns=0,
        ts_ingest_ns=0,
        causation_id=uuid.UUID(int=99),
        instrument_id=INSTRUMENT.id,
        side=side,
        price=Decimal(price),
        quantity=Decimal(quantity),
        fee=Decimal(fee),
        fee_currency="USD",
    )


def test_book_fifo_pnl_and_no_shorts() -> None:
    """FIFO realized P&L is exact; fees come out of cash; shorts are refused."""
    book = Book(cash=Decimal(1000))
    book.apply(_fill(Side.BUY, "10", "1", "0.01"))
    book.apply(_fill(Side.BUY, "12", "1"))
    book.apply(_fill(Side.SELL, "15", "1.5", "0.02"))
    assert book.realized == Decimal("5") + Decimal("1.5")  # (15-10)*1 + (15-12)*0.5
    assert book.position == Decimal("0.5")
    assert book.cash == Decimal(1000) - 10 - Decimal("0.01") - 12 + Decimal("22.5") - Decimal(
        "0.02"
    )
    assert book.equity(Decimal(20)) == book.cash + 10
    with pytest.raises(ValueError, match="exceeds position"):
        book.apply(_fill(Side.SELL, "15", "1"))


def test_gate_rules_in_order() -> None:
    """Order notional is checked before the resulting position."""
    gate = PreTradeGate(TradingLimits(max_order_notional=Decimal(100), max_position=Decimal(2)))
    assert gate.check(Side.BUY, Decimal(1), Decimal(50), Decimal(0)).accepted
    notional = gate.check(Side.BUY, Decimal(3), Decimal(50), Decimal(0))
    assert (notional.accepted, notional.rule, notional.value) == (
        False,
        MAX_ORDER_NOTIONAL,
        Decimal(150),
    )
    position = gate.check(Side.BUY, Decimal(1), Decimal(10), Decimal(2))
    assert (position.accepted, position.rule, position.value) == (False, MAX_POSITION, Decimal(3))
