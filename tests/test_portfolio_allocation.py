# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Allocation and the shared daily series (BP6 section 4.9, F-A; amendment 10; T14)."""

import ast
import datetime
import math
import uuid
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest

from auqua.analytics import stats
from auqua.config.portfolio import PortfolioConfig
from auqua.contracts.errors import OffGridError
from auqua.contracts.events.base import Event, LogRecord
from auqua.contracts.events.market import Bar, ReferenceRate
from auqua.contracts.events.reference import InstrumentDefined
from auqua.contracts.events.trading import AllocationUnavailableReason as Reason
from auqua.contracts.events.trading import Signal
from auqua.contracts.identity import InstrumentId, SeriesId
from auqua.contracts.instruments import (
    AnyInstrument,
    AssetClass,
    CryptoSpot,
    Equity,
    Identifiers,
    PerpetualSwap,
    PerpetualType,
)
from auqua.contracts.numeric import Rounding, from_float
from auqua.contracts.portfolio import AccountView, LogPosition
from auqua.portfolio import allocation as allocation_module
from auqua.portfolio.allocation import (
    SIGMA_INCREMENT,
    FixedQuantity,
    FixedQuantitySpec,
    Proposal,
    ProposedTarget,
    Unavailable,
    VolatilityTarget,
    VolatilityTargetSpec,
    allocator,
    daily_closes,
    volatility,
)
from auqua.portfolio.errors import PortfolioError
from auqua.portfolio.series import (
    BUSINESS_DAYS,
    CALENDAR_DAYS,
    NS_PER_DAY,
    SeriesError,
    daily_last,
    periods_per_year,
    simple_returns,
)
from auqua.portfolio.valuation import Valuation, latest_close
from auqua.storage.marketview import MarketView
from auqua.storage.reference import ReferenceIndex

HOUR = 3_600 * 10**9
D0 = 1_788_220_800 * 10**9  # 2026-09-01T00:00:00Z
CLOSES = ("100", "102", "100.98", "101.9898")  # returns 0.02, -0.01, 0.01
VARIANCE = 1 / 7000  # (1 x 0.0001 + 0.5 x 0.0001 + 0.25 x 0.0004) / 1.75 (H = 1, W = 3)
T = D0 + 4 * NS_PER_DAY  # the day-3 bar's availability: four closed days
SPY = InstrumentId("ARCX", "SPY")
TSE = InstrumentId("XTKS", "PENNY")
EUR = InstrumentId("XETR", "SAP")
GBP = InstrumentId("XLON", "VOD")
ETH_BTC = InstrumentId("COINBASE", "ETH-BTC")
BTC_USD = InstrumentId("COINBASE", "BTC-USD")
INVERSE = InstrumentId("DERIBIT", "BTC-PERPETUAL")
LINEAR = InstrumentId("DERIBIT", "BTC_USDC-PERPETUAL")
FLAT = InstrumentId("ARCX", "FLAT")
NOBARS = InstrumentId("ARCX", "NONE")
ZERO = InstrumentId("ARCX", "ZERO")
EUR_SERIES = SeriesId("FRB", "H10/RXI$US_N.B.EU")
JPY_SERIES = SeriesId("FRB", "H10/RXI_N.B.JA")
SPEC = VolatilityTargetSpec(
    target_volatility=Decimal("0.10"), leverage_cap=Decimal(2), half_life_days=1, warmup_days=3
)
C = Decimal("100000.00")


def _equity(instrument_id: InstrumentId, quote: str, increment: str = "1") -> Equity:
    return Equity(
        id=instrument_id,
        quote_currency=quote,
        price_increment=Decimal("0.0000001"),
        quantity_increment=Decimal(increment),
        multiplier=Decimal(1),
        valid_from_ns=0,
        knowledge_ns=0,
        identifiers=Identifiers(),
        is_fund=False,
    )


def _perpetual(instrument_id: InstrumentId, kind: PerpetualType, quote: str) -> PerpetualSwap:
    return PerpetualSwap(
        id=instrument_id,
        quote_currency=quote,
        price_increment=Decimal("0.5"),
        quantity_increment=Decimal(1),
        multiplier=Decimal(10),
        valid_from_ns=0,
        knowledge_ns=0,
        identifiers=Identifiers(),
        base_currency="BTC",
        settlement_currency="BTC" if kind is PerpetualType.INVERSE else "USDC",
        contract_type=kind,
        funding_interval_ns=8 * HOUR,
    )


def _spot(instrument_id: InstrumentId, base: str, quote: str) -> CryptoSpot:
    return CryptoSpot(
        id=instrument_id,
        quote_currency=quote,
        price_increment=Decimal("0.00000001"),
        quantity_increment=Decimal("0.0001"),
        multiplier=Decimal(1),
        valid_from_ns=0,
        knowledge_ns=0,
        identifiers=Identifiers(),
        base_currency=base,
    )


def _bar(
    instrument_id: InstrumentId,
    start: int,
    close: str,
    interval: int = NS_PER_DAY,
    published: int | None = None,
) -> Bar:
    price = Decimal(close)
    return Bar(
        id=uuid.uuid5(uuid.NAMESPACE_URL, f"{instrument_id}/{start}/{interval}"),
        source="ingest/test",
        ts_event_ns=start + interval,
        ts_ingest_ns=start + interval,
        instrument_id=instrument_id,
        start_ns=start,
        interval_ns=interval,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=Decimal(1),
        ts_published_ns=published,
    )


def _daily(instrument_id: InstrumentId, closes: tuple[str, ...], scale: str = "1") -> list[Bar]:
    factor = Decimal(scale)
    return [
        _bar(instrument_id, D0 + day * NS_PER_DAY, str(Decimal(close) * factor))
        for day, close in enumerate(closes)
    ]


def _rate(series: SeriesId, value: str, unit: str) -> ReferenceRate:
    return ReferenceRate(
        id=uuid.uuid5(uuid.NAMESPACE_URL, str(series)),
        source="ingest/frb/h10",
        ts_event_ns=D0,
        ts_ingest_ns=D0,
        series_id=series,
        value=Decimal(value),
        unit=unit,
        ts_published_ns=D0,
    )


def _market(events: list[Event]) -> tuple[MarketView, ReferenceIndex, Valuation]:
    instruments: list[AnyInstrument] = [
        _equity(SPY, "USD"),
        _equity(TSE, "JPY"),
        _equity(EUR, "EUR"),
        _equity(GBP, "GBP"),
        _equity(FLAT, "USD"),
        _equity(NOBARS, "USD"),
        _equity(ZERO, "USD"),
        _spot(ETH_BTC, "ETH", "BTC"),
        _spot(BTC_USD, "BTC", "USD"),
        _perpetual(INVERSE, PerpetualType.INVERSE, "USD"),
        _perpetual(LINEAR, PerpetualType.LINEAR, "USDC"),
    ]
    defined: list[Event] = [
        InstrumentDefined(
            id=uuid.uuid5(uuid.NAMESPACE_URL, str(i.id)),
            source="ingest/test",
            ts_event_ns=0,
            ts_ingest_ns=0,
            instrument=i,
        )
        for i in instruments
    ]
    records = [LogRecord(sequence=n, event=e) for n, e in enumerate(defined + events)]
    entry = {"source": "s", "read_on": datetime.date(2026, 10, 29)}
    config = PortfolioConfig.model_validate(
        {
            "currencies": {
                code: {"kind": kind, "minor_units": units, **entry}
                for code, kind, units in (
                    ("USD", "fiat", 2),
                    ("EUR", "fiat", 2),
                    ("JPY", "fiat", 0),
                    ("GBP", "fiat", 2),
                    ("BTC", "crypto", 8),
                )
            },
            "rates": {
                "EUR": {"series": str(EUR_SERIES)},
                "JPY": {"series": str(JPY_SERIES)},
                "BTC": {"instrument": str(BTC_USD)},
            },
        },
        strict=False,
    )
    market, reference = MarketView(records), ReferenceIndex.from_records(records)
    return market, reference, Valuation(config, market, reference)


def _scenario() -> tuple[MarketView, ReferenceIndex, Valuation]:
    return _market(
        [
            *_daily(SPY, CLOSES),
            *_daily(TSE, CLOSES, "0.004"),  # 0.4 JPY a share at 150 JPY per USD
            *_daily(EUR, CLOSES),
            *_daily(GBP, CLOSES),
            *_daily(ETH_BTC, CLOSES, "0.0005"),
            *_daily(INVERSE, CLOSES),
            *_daily(LINEAR, CLOSES),
            *_daily(FLAT, ("100", "100", "100", "100")),
            *_daily(ZERO, ("100", "102", "100.98", "0")),
            _bar(BTC_USD, D0 + 3 * NS_PER_DAY, "60000"),
            _rate(EUR_SERIES, "1.1211", "USD per EUR"),
            _rate(JPY_SERIES, "150", "JPY per USD"),
        ]
    )


def _view(equity: Decimal | None = C, at: int = T) -> AccountView:
    return AccountView(
        account_id="research",
        as_of_ns=at,
        log_position=LogPosition(next_sequence=1, hash="a" * 64),
        equity_base=equity,
        unconverted=() if equity is not None else ("GBP",),
    )


def _signal(instrument_id: InstrumentId, direction: int = 1) -> Signal:
    return Signal(
        id=uuid.uuid5(uuid.NAMESPACE_URL, f"signal/{instrument_id}/{direction}"),
        source="research/backtest",
        ts_event_ns=T,
        ts_ingest_ns=T,
        causation_id=uuid.UUID(int=1),
        instrument_id=instrument_id,
        direction=direction,
        provider="baseline/ma-crossover",
        provider_version="1",
    )


def _target(spec: VolatilityTargetSpec = SPEC) -> VolatilityTarget:
    market, reference, valuation = _scenario()
    return VolatilityTarget(spec, market=market, valuation=valuation, reference=reference)


def _sigma(asset_class: AssetClass) -> Decimal:
    closes = [Decimal(c) for c in CLOSES]
    sigma = volatility(
        closes, half_life=1, window=3, periods_per_year=periods_per_year(asset_class)
    )
    assert isinstance(sigma, float)
    return from_float(sigma, SIGMA_INCREMENT, Rounding.UP)


def _down(numerator: Fraction, denominator: Fraction, increment: Decimal) -> Decimal:
    """Oracle: the exact quotient rounded toward zero onto ``increment`` (positive operands)."""
    steps = math.floor(numerator / denominator / Fraction(increment))
    return Decimal(steps) * increment


def _one(
    target: VolatilityTarget, instrument_id: InstrumentId, equity: Decimal | None = C
) -> ProposedTarget | Unavailable:
    proposal = target.propose([_signal(instrument_id)], _view(equity))
    rows: list[ProposedTarget | Unavailable] = [*proposal.targets, *proposal.unavailable]
    assert len(rows) == 1
    return rows[0]


# --- F-A: the shared series --------------------------------------------------------------------


def test_daily_last_keeps_each_days_last_value_by_the_callers_time() -> None:
    """Grouping by the UTC day of the time the caller passes; days in first-seen order."""
    times = [D0, D0 + HOUR, D0 + NS_PER_DAY, D0 + NS_PER_DAY + 1]
    assert daily_last(times, ["a", "b", "c", "d"]) == [
        (D0 // NS_PER_DAY, "b"),
        (D0 // NS_PER_DAY + 1, "d"),
    ]
    with pytest.raises(ValueError, match=r"shorter|longer"):
        daily_last([D0], ["a", "b"])


def test_simple_returns_are_the_one_decimal_to_float_boundary() -> None:
    """Exact returns, one float conversion; analytics.stats.returns delegates (F-A)."""
    closes = [Decimal(c) for c in CLOSES]
    assert simple_returns(closes) == pytest.approx([0.02, -0.01, 0.01], rel=1e-15)
    assert stats.returns(closes) == simple_returns(closes)
    with pytest.raises(SeriesError, match="two"):
        simple_returns([Decimal(1)])
    with pytest.raises(SeriesError, match="not positive"):
        simple_returns([Decimal(0), Decimal(1)])
    with pytest.raises(stats.StatisticsError, match="not positive"):
        stats.returns([Decimal(-1), Decimal(1)])


def test_annualisation_is_bp5s_predicate() -> None:
    """365 calendar days for crypto, 252 business days otherwise (BP5 experiment)."""
    assert (CALENDAR_DAYS, BUSINESS_DAYS) == (365, 252)
    assert periods_per_year(AssetClass.CRYPTO) == CALENDAR_DAYS
    for asset_class in AssetClass:
        if asset_class is not AssetClass.CRYPTO:
            assert periods_per_year(asset_class) == BUSINESS_DAYS


# --- sigma_hat (D2; amendment 10, F-V) --------------------------------------------------------


@pytest.mark.parametrize("asset_class", [AssetClass.CRYPTO, AssetClass.EQUITY])
def test_volatility_closed_form(asset_class: AssetClass) -> None:
    """H = 1, W = 3: sigma_d^2 = 1/7000; x 365 for crypto, x 252 otherwise (rel 1e-12)."""
    year = periods_per_year(asset_class)
    closes = [Decimal(c) for c in CLOSES]
    sigma = volatility(closes, half_life=1, window=3, periods_per_year=year)
    assert sigma == pytest.approx(math.sqrt(VARIANCE * year), rel=1e-12)


def test_volatility_window_warmup_and_invalid() -> None:
    """A fourth return drops the oldest; fewer than W returns: warm-up; F-W: invalid inputs."""
    closes = [Decimal(c) for c in CLOSES]
    sigma = volatility(closes, half_life=1, window=3, periods_per_year=252)
    longer = volatility([Decimal(50), *closes], half_life=1, window=3, periods_per_year=252)
    assert longer == sigma, "the window holds the newest W returns only"
    outside = volatility([Decimal(-5), *closes], half_life=1, window=3, periods_per_year=252)
    assert outside == sigma, "a non-positive close outside the window does not count"
    short = volatility(closes[1:], half_life=1, window=3, periods_per_year=252)
    assert short is Reason.VOLATILITY_WARMUP
    for bad in ([Decimal(100), Decimal(0), *closes[2:]], [Decimal(100)] * 4):
        result = volatility(bad, half_life=1, window=3, periods_per_year=252)
        assert result is Reason.VOLATILITY_INVALID


def test_a_daily_close_is_used_at_its_own_availability() -> None:
    """F-V: the day-3 bar (available at the start of day 4) counts for day 3, used at once."""
    market, _, _ = _scenario()
    closes = [Decimal(c) for c in CLOSES]
    assert daily_closes(market, SPY, T) == closes
    assert daily_closes(market, SPY, T - 1) == closes[:3], "not yet released"


def test_hourly_closes_belong_to_the_day_of_their_interval_end() -> None:
    """F-V: the 23:00-24:00 close is its own day's close; the decision day's bars are excluded."""
    day1 = D0 + NS_PER_DAY
    market, _, _ = _market(
        [
            _bar(SPY, day1 - 2 * HOUR, "99", HOUR),
            _bar(SPY, day1 - HOUR, "100", HOUR),  # 23:00-24:00 of day 0
            _bar(SPY, day1, "101", HOUR),  # 00:00-01:00 of day 1
            _bar(SPY, day1 + HOUR, "102", HOUR, published=day1 + 5 * HOUR),
        ]
    )
    assert daily_closes(market, SPY, day1) == [Decimal(100)]
    assert daily_closes(market, SPY, day1 + 3 * HOUR) == [Decimal(100)], "day 1 has not ended"
    end = day1 + NS_PER_DAY
    assert daily_closes(market, SPY, end) == [Decimal(100), Decimal(102)]


def test_late_publication_and_days_without_bars() -> None:
    """A bar released later is excluded until then; a UTC day without a bar adds no return."""
    late = _bar(SPY, D0 + 3 * NS_PER_DAY, "101.9898", published=T + HOUR)
    market, _, _ = _market([*_daily(SPY, CLOSES[:3]), late])
    assert len(daily_closes(market, SPY, T)) == 3  # noqa: PLR2004
    assert len(daily_closes(market, SPY, T + HOUR)) == 4  # noqa: PLR2004
    gap: list[Event] = [
        _bar(SPY, D0 + day * NS_PER_DAY, c) for day, c in zip((0, 1, 3, 4), CLOSES, strict=True)
    ]
    market, _, _ = _market(gap)
    later = D0 + 5 * NS_PER_DAY
    assert daily_closes(market, SPY, later) == [Decimal(c) for c in CLOSES]


def test_latest_close_is_one_rule_and_refuses_non_positive_prices() -> None:
    """F-Y: the last released close across intervals; a non-positive close is unavailable."""
    market, _, _ = _scenario()
    bar = latest_close(market, SPY, T, T)
    assert bar is not None
    assert bar.close == Decimal("101.9898")
    assert latest_close(market, ZERO, T, T) is None
    assert latest_close(market, NOBARS, T, T) is None


# --- T14: volatility targeting -----------------------------------------------------------------


def test_usd_quote_closed_form_and_leverage_cap() -> None:
    """USD quote: q = C sigma* / (sigma_hat P M), rounded down; the cap L C / N when smaller."""
    sigma, price = _sigma(AssetClass.EQUITY), Decimal("101.9898")
    row = _one(_target(), SPY)
    assert isinstance(row, ProposedTarget)
    expected = _down(Fraction(C) * Fraction("0.10"), Fraction(sigma) * Fraction(price), Decimal(1))
    assert (row.quantity, row.conversion) == (expected, None)
    assert expected == Decimal(516)  # 10,000 / (0.18973... x 101.9898) = 516.76...
    capped = _target(
        VolatilityTargetSpec(
            target_volatility=Decimal("0.10"),
            leverage_cap=Decimal("0.5"),
            half_life_days=1,
            warmup_days=3,
        )
    )
    row = _one(capped, SPY)
    assert isinstance(row, ProposedTarget)
    assert row.quantity == _down(Fraction(C) / 2, Fraction(price), Decimal(1)) == Decimal(490)


def test_ccy_per_usd_rate_folded_in_one_rational() -> None:
    """'JPY per USD': q = C sigma* r / (sigma_hat P M); P M = 0.4 JPY < 0.005 USD (E1)."""
    sigma, price, rate = _sigma(AssetClass.EQUITY), Decimal("0.4079592"), Fraction(150)
    row = _one(_target(), TSE)
    assert isinstance(row, ProposedTarget)
    q = _down(Fraction(C) * Fraction("0.10") * rate, Fraction(sigma) * Fraction(price), Decimal(1))
    cap = _down(2 * Fraction(C) * rate, Fraction(price), Decimal(1))
    assert row.quantity == min(q, cap) > 0
    assert row.conversion is not None
    assert (row.conversion.unit, row.conversion.value) == ("JPY per USD", Decimal("0.00"))


@pytest.mark.parametrize(
    ("instrument_id", "rate", "unit", "asset_class", "increment"),
    [
        (EUR, Fraction("1.1211"), "USD per EUR", AssetClass.EQUITY, Decimal(1)),
        (ETH_BTC, Fraction(60000), "USD per BTC", AssetClass.CRYPTO, Decimal("0.0001")),
    ],
)
def test_usd_per_ccy_rate_folded_in_one_rational(
    instrument_id: InstrumentId,
    rate: Fraction,
    unit: str,
    asset_class: AssetClass,
    increment: Decimal,
) -> None:
    """'USD per <CCY>' (H.10) or a crypto close: q = C sigma* / (sigma_hat P M r)."""
    sigma = _sigma(asset_class)
    scale = Decimal("0.0005") if instrument_id == ETH_BTC else Decimal(1)
    price = Fraction(Decimal("101.9898") * scale)
    row = _one(_target(), instrument_id)
    assert isinstance(row, ProposedTarget)
    q = _down(Fraction(C) * Fraction("0.10"), Fraction(sigma) * price * rate, increment)
    cap = _down(2 * Fraction(C), price * rate, increment)
    assert row.quantity == min(q, cap)
    assert row.conversion is not None
    assert row.conversion.unit == unit


def test_inverse_perpetual_unit_value_is_the_multiplier() -> None:
    """F-X: N = M (USD per contract), independent of price; linear perpetuals refused (R1)."""
    sigma = _sigma(AssetClass.CRYPTO)
    row = _one(_target(), INVERSE)
    assert isinstance(row, ProposedTarget)
    q = _down(Fraction(C) * Fraction("0.10"), Fraction(sigma) * 10, Decimal(1))
    assert row.quantity == min(q, _down(2 * Fraction(C), Fraction(10), Decimal(1)))
    with pytest.raises(PortfolioError, match="linear"):
        _target().propose([_signal(LINEAR)], _view())


def test_direction_applies_and_flat_needs_no_size() -> None:
    """Direction -1 negates the size; direction 0 is a zero target, whatever the inputs."""
    target = _target()
    long = target.propose([_signal(SPY)], _view()).targets[0].quantity
    short = target.propose([_signal(SPY, -1)], _view()).targets[0].quantity
    assert short == -long
    flat = target.propose([_signal(NOBARS, 0)], _view(equity=None)).targets[0]
    assert (flat.quantity, flat.quantity.is_signed()) == (Decimal(0), False)


@pytest.mark.parametrize(
    ("instrument_id", "equity", "reason"),
    [
        (SPY, None, Reason.EQUITY_INCOMPLETE),
        (SPY, Decimal(0), Reason.EQUITY_NOT_POSITIVE),
        (SPY, Decimal(-1), Reason.EQUITY_NOT_POSITIVE),
        (NOBARS, C, Reason.PRICE_UNAVAILABLE),
        (ZERO, C, Reason.PRICE_UNAVAILABLE),
        (GBP, C, Reason.RATE_UNAVAILABLE),
        (FLAT, C, Reason.VOLATILITY_INVALID),
    ],
)
def test_no_proposal_is_reported_with_its_reason(
    instrument_id: InstrumentId, equity: Decimal | None, reason: Reason
) -> None:
    """Nothing is estimated (P4; amendments 2 and 10): each reason, checked in order."""
    assert _one(_target(), instrument_id, equity=equity) == Unavailable(instrument_id, reason)


@pytest.mark.parametrize(
    ("instrument_id", "equity", "at"),
    [
        (SPY, None, T),
        (SPY, Decimal(0), T),
        (NOBARS, C, T),
        (ZERO, C, T),
        (GBP, C, T),
        (FLAT, C, T),
        (SPY, C, T - 1),
    ],
    ids=["incomplete", "not-positive", "no-price", "zero-price", "no-rate", "invalid", "warmup"],
)
def test_direction_zero_is_a_zero_target_under_every_unavailable_condition(
    instrument_id: InstrumentId, equity: Decimal | None, at: int
) -> None:
    """Amendment 11 (2): closing needs no size; never blocked by a missing input."""
    proposal = _target().propose([_signal(instrument_id, 0)], _view(equity, at))
    assert proposal == Proposal((ProposedTarget(instrument_id, Decimal(0)),), ())


def test_warmup_counts_daily_returns() -> None:
    """Before the fourth daily close there are only two returns: warm-up (W = 3)."""
    target = _target()
    proposal = target.propose([_signal(SPY)], _view(at=T - 1))
    assert proposal.unavailable == (Unavailable(SPY, Reason.VOLATILITY_WARMUP),)


def test_proposals_need_defined_distinct_instruments() -> None:
    """An undefined instrument or two signals for one instrument are refused."""
    target = _target()
    with pytest.raises(PortfolioError, match="not defined"):
        target.propose([_signal(InstrumentId("ARCX", "NOPE"))], _view())
    with pytest.raises(PortfolioError, match="one signal per instrument"):
        target.propose([_signal(SPY), _signal(SPY, -1)], _view())


# --- FixedQuantity (PC-10) and the specification ----------------------------------------------


def test_fixed_quantity_is_quantity_times_direction() -> None:
    """The walking skeleton's sizing, on the instrument's quantity grid."""
    _, reference, _ = _scenario()
    fixed = FixedQuantity(FixedQuantitySpec(quantity=Decimal("0.5")), reference)
    signals = [_signal(ETH_BTC, 1), _signal(BTC_USD, -1), _signal(INVERSE, 0)]
    with pytest.raises(OffGridError):
        fixed.propose(signals, _view())
    fixed = FixedQuantity(FixedQuantitySpec(quantity=Decimal(2)), reference)
    proposal = fixed.propose(signals, _view())
    assert [t.quantity for t in proposal.targets] == [Decimal(2), Decimal(-2), Decimal(0)]
    assert proposal.unavailable == ()


def test_allocator_follows_the_spec_method() -> None:
    """``fixed_quantity`` and ``volatility_target``; defaults H = 20, W = 270 (ADR 0037)."""
    market, reference, valuation = _scenario()
    fixed = FixedQuantitySpec(quantity=Decimal(1))
    made = allocator(fixed, market=market, valuation=valuation, reference=reference)
    assert isinstance(made, FixedQuantity)
    target = VolatilityTargetSpec(target_volatility=Decimal("0.1"), leverage_cap=Decimal(1))
    assert (target.half_life_days, target.warmup_days) == (20, 270)
    made = allocator(target, market=market, valuation=valuation, reference=reference)
    assert isinstance(made, VolatilityTarget)
    assert fixed.assumptions() == {"allocation": "fixed_quantity", "quantity": "1"}
    with pytest.raises(ValueError, match="greater than or equal to 1"):
        VolatilityTargetSpec(
            target_volatility=Decimal("0.1"), leverage_cap=Decimal(1), half_life_days=0
        )


def test_floats_stay_inside_the_statistic() -> None:
    """C9: ``math`` and float literals appear in ``volatility`` only."""
    tree = ast.parse(Path(allocation_module.__file__).read_text(encoding="utf-8"))
    inside: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "volatility":
            inside |= {id(child) for child in ast.walk(node)}
    for node in ast.walk(tree):
        uses_math = isinstance(node, ast.Name) and node.id == "math"
        is_float = isinstance(node, ast.Constant) and isinstance(node.value, float)
        if uses_math or is_float:
            assert id(node) in inside, ast.dump(node)
