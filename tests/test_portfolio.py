# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Portfolio ledger, lots, day counts and valuation (BP6 sections 4.2-4.5; T2-T5, T11, T12)."""

import datetime
import uuid
from collections import deque
from decimal import Decimal
from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from auqua.config.portfolio import PortfolioConfig
from auqua.contracts.events.base import Event, LogRecord
from auqua.contracts.events.market import Bar, ReferenceRate
from auqua.contracts.events.reference import InstrumentDefined
from auqua.contracts.identity import InstrumentId, SeriesId
from auqua.contracts.instruments import Bond, CryptoSpot, DayCount, Identifiers
from auqua.contracts.portfolio import Posting, PostingKind
from auqua.portfolio.daycount import (
    require_bond_day_count,
    require_supported,
    thirty_360_days,
    treasury_accrued_amount,
    treasury_accrued_per_1000,
)
from auqua.portfolio.errors import (
    ConfigurationError,
    DayCountNotSupportedError,
    LotError,
    PortfolioError,
    RoundingTieError,
)
from auqua.portfolio.ledger import Ledger
from auqua.portfolio.lots import Lots
from auqua.portfolio.valuation import Valuation
from auqua.storage.marketview import MarketView
from auqua.storage.reference import ReferenceIndex

CENT = Decimal("0.01")
CAUSE = uuid.UUID(int=1)
AMOUNTS = st.decimals(min_value=-(10**6), max_value=10**6, places=2).filter(lambda d: d != 0)
KINDS = st.sampled_from([k for k in PostingKind if k is not PostingKind.RECEIVABLE])


# --- T2: balances are the sums of their postings ----------------------------------------------
@given(st.lists(st.tuples(st.sampled_from(["USD", "EUR", "BTC"]), AMOUNTS, KINDS), max_size=40))
def test_balances_equal_the_sum_of_postings(rows: list[tuple[str, Decimal, PostingKind]]) -> None:
    """Each currency's cash balance is the exact sum of its postings (T2, PC-2)."""
    ledger = Ledger("research")
    for currency, amount, kind in rows:
        ledger.post(Posting("research", currency, amount, kind, CAUSE))
    expected: dict[str, Fraction] = {}
    for currency, amount, _ in rows:
        expected[currency] = expected.get(currency, Fraction(0)) + Fraction(amount)
    assert {b.currency: Fraction(b.cash) for b in ledger.balances()} == expected
    assert [b.currency for b in ledger.balances()] == sorted(expected)
    assert ledger.postings == tuple(Posting("research", c, a, k, CAUSE) for c, a, k in rows)


def test_receivable_is_kept_apart_from_cash() -> None:
    """A dividend receivable is its own balance and never negative (BP6 4.2, C1)."""
    ledger = Ledger("research")
    ledger.post(Posting("research", "USD", Decimal("26.00"), PostingKind.RECEIVABLE, CAUSE))
    ledger.post(Posting("research", "USD", Decimal("26.00"), PostingKind.DIVIDEND, CAUSE))
    ledger.post(Posting("research", "USD", Decimal("-26.00"), PostingKind.RECEIVABLE, CAUSE))
    balance = ledger.balance("USD")
    assert (balance.cash, balance.receivable) == (Decimal("26.00"), Decimal(0))
    with pytest.raises(PortfolioError, match="receivable"):
        ledger.post(Posting("research", "USD", Decimal(-1), PostingKind.RECEIVABLE, CAUSE))
    with pytest.raises(PortfolioError, match="ledger of research"):
        ledger.post(Posting("other", "USD", Decimal(1), PostingKind.FEE, CAUSE))
    assert ledger.balance("JPY").cash == 0
    assert [b.currency for b in ledger.balances()] == ["USD"]


# --- T3: FIFO against a brute-force oracle ------------------------------------------------------
TRADES = st.lists(
    st.tuples(st.integers(min_value=-6, max_value=6).filter(bool), st.integers(1, 500)),
    max_size=30,
)


@given(TRADES)
def test_fifo_matches_a_unit_by_unit_oracle(trades: list[tuple[int, int]]) -> None:
    """Relieved cost, position and open cost equal a one-unit-at-a-time FIFO queue (T3)."""
    lots = Lots(CENT)
    queue: deque[tuple[int, int]] = deque()  # (direction, price) per unit
    for quantity, price in trades:
        direction = 1 if quantity > 0 else -1
        expected_cost = 0
        for _ in range(abs(quantity)):
            if queue and queue[0][0] != direction:
                expected_cost += queue.popleft()[1]
            else:
                queue.append((direction, price))
        result = lots.trade(Decimal(quantity), Decimal(price))
        assert result.relieved_cost == expected_cost
        assert lots.position == sum(d for d, _ in queue)
        assert lots.cost == sum(p for _, p in queue)


def test_trade_refuses_zero() -> None:
    """A trade moves units."""
    with pytest.raises(LotError, match="non-zero"):
        Lots(CENT).trade(Decimal(0), Decimal(1))


# --- T4: the IRS split example ----------------------------------------------------------------
@pytest.mark.parametrize(("price", "unit"), [(Decimal(10), Decimal(5)), (Decimal(12), Decimal(6))])
def test_irs_split_example(price: Decimal, unit: Decimal) -> None:
    """100 shares at $10 -> 200 at $5 ($1,000/200); 100 at $12 -> 200 at $6 (T4, IRS FAQ)."""
    lots = Lots(CENT)
    lots.trade(Decimal(100), price)
    lots.split(Decimal(2))
    (lot,) = lots.lots()
    assert (lot.quantity, lot.total_cost, lot.unit_cost) == (Decimal(200), price * 100, unit)
    assert lots.trade(Decimal(-50), Decimal(1)).relieved_cost == unit * 50
    with pytest.raises(LotError, match="positive"):
        lots.split(Decimal(0))


# --- T5: the one rounding rule ------------------------------------------------------------------
@pytest.mark.parametrize(
    ("units", "price", "multiplier"),
    [(1, Decimal(1), Decimal(3)), (1, Decimal(2), Decimal(3)), (3, Decimal(10), Decimal(3))],
    ids=["one-third", "two-thirds", "split-3"],
)
@given(cuts=st.lists(st.integers(min_value=1, max_value=4), min_size=1, max_size=9))
def test_relief_parts_sum_to_the_lot_cost(
    units: int, price: Decimal, multiplier: Decimal, cuts: list[int]
) -> None:
    """After a non-terminating split, relieved parts plus the rest equal the lot cost (T5)."""
    lots = Lots(CENT)
    lots.trade(Decimal(units), price)
    total = price * units
    lots.split(multiplier)
    assert lots.lots()[0].unit_cost is None
    open_units = units * 3
    relieved = Decimal(0)
    for cut in cuts:
        take = min(cut, open_units)
        if take == 0:
            break
        cost = lots.trade(Decimal(-take), Decimal(1)).relieved_cost
        assert cost == cost.quantize(CENT), "each part is on the minor-unit grid"
        relieved += cost
        open_units -= take
    assert relieved + lots.cost == total
    if open_units:
        assert lots.trade(Decimal(-open_units), Decimal(1)).relieved_cost + relieved == total
    assert lots.position == 0


def test_trade_at_value_splits_the_value_exactly() -> None:
    """Amendment 13: relieved units take their share of the value; the opened lot the rest."""
    lots = Lots(CENT)
    lots.trade(Decimal(3), Decimal(10))
    result = lots.trade_at_value(Decimal(-5), Decimal(100))
    assert (result.proceeds, result.relieved_cost, result.opened) == (
        Decimal(60),
        Decimal(30),
        Decimal(2),
    )
    assert (lots.position, lots.cost) == (Decimal(-2), Decimal(40))
    lots = Lots(CENT)
    lots.trade(Decimal(1), Decimal(10))
    result = lots.trade_at_value(Decimal(-3), Decimal(100))  # 100 / 3 does not terminate
    assert result.proceeds == Decimal("33.33")
    assert result.proceeds + lots.cost == Decimal(100)
    (lot,) = lots.lots()
    assert (lot.total_cost, lot.unit_cost) == (Decimal("66.67"), Decimal("33.335"))
    assert lots.trade(Decimal(1), Decimal(1)).proceeds == Decimal(1)


def test_relief_without_a_split_never_rounds() -> None:
    """A lot opened at a fill price relieves unit_cost * u exactly (PC-10)."""
    lots = Lots(CENT)
    lots.trade(Decimal("0.5"), Decimal("93800.10"))
    assert lots.trade(Decimal("-0.001"), Decimal(1)).relieved_cost == Decimal("93.80010")


# --- T11: accrued interest and 30/360 from their primary texts ---------------------------------
def test_treasury_accrued_interest_reproduces_31_cfr_356() -> None:
    """31 CFR 356 App. B examples: $16.87500 / $2,531.25; $36.72798 / $404.01 (T11)."""
    note = treasury_accrued_per_1000(Decimal("0.0675"), [(92, 184)])
    assert str(note) == "16.87500"
    assert str(treasury_accrued_amount(note, Decimal(150000))) == "2531.25"
    bond = treasury_accrued_per_1000(Decimal("0.1075"), [(44, 181), (81, 184)])
    assert str(bond) == "36.72798"
    assert str(treasury_accrued_amount(bond, Decimal(11000))) == "404.01"


def test_treasury_rounding_refuses_an_exact_tie() -> None:
    """'Normal rounding procedures' is undefined at a tie: an exact tie is refused (R11)."""
    with pytest.raises(RoundingTieError):
        treasury_accrued_amount(Decimal("0.00500"), Decimal(1000))
    with pytest.raises(PortfolioError, match="periods"):
        treasury_accrued_per_1000(Decimal("0.05"), [])


@pytest.mark.parametrize(
    ("start", "end", "days"),
    [
        ((2026, 1, 15), (2026, 3, 31), 76),
        ((2026, 1, 31), (2026, 3, 31), 60),
        ((2026, 1, 30), (2026, 3, 31), 60),
        ((2026, 1, 30), (2026, 2, 28), 28),
        ((2026, 2, 28), (2026, 3, 31), 33),
    ],
)
def test_msrb_g33_thirty_360(
    start: tuple[int, int, int], end: tuple[int, int, int], days: int
) -> None:
    """MSRB G-33(e)(ii): only the two quoted 31 -> 30 rules, no February rule (T11, C6)."""
    assert thirty_360_days(datetime.date(*start), datetime.date(*end)) == days


def test_unsourced_day_counts_are_refused() -> None:
    """Conventions without a verified text are refused by name (P9, R2)."""
    require_supported(DayCount.THIRTY_360)
    require_supported(DayCount.ACT_ACT_TREASURY)
    for convention in (
        DayCount.ACT_360,
        DayCount.ACT_365_FIXED,
        DayCount.ACT_ACT_ICMA,
        DayCount.ACT_ACT_ISDA,
    ):
        with pytest.raises(DayCountNotSupportedError, match=convention.value):
            require_supported(convention)


# --- T12: conversions record rate, source, availability and value ------------------------------
HOUR = 3_600 * 10**9
T0 = 1_788_220_800 * 10**9
EUR_SERIES = SeriesId("FRB", "H10/RXI$US_N.B.EU")
JPY_SERIES = SeriesId("FRB", "H10/RXI_N.B.JA")
BTC_USD = InstrumentId("COINBASE", "BTC-USD")


def _rate(series: SeriesId, value: str, unit: str, published: int, revision: int = 0) -> Event:
    return ReferenceRate(
        id=uuid.UUID(int=published + revision),
        source="ingest/frb/h10",
        ts_event_ns=T0,
        ts_ingest_ns=published + revision,
        series_id=series,
        value=Decimal(value),
        unit=unit,
        ts_published_ns=published,
        revision=revision,
    )


def _bar(start: int, interval: int, close: str) -> Event:
    price = Decimal(close)
    return Bar(
        id=uuid.UUID(int=start + interval),
        source="ingest/coinbase/exchange",
        ts_event_ns=start + interval,
        ts_ingest_ns=start + interval,
        instrument_id=BTC_USD,
        start_ns=start,
        interval_ns=interval,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=Decimal(1),
    )


def _valuation() -> Valuation:
    pair = CryptoSpot(
        id=BTC_USD,
        quote_currency="USD",
        price_increment=CENT,
        quantity_increment=Decimal("0.00000001"),
        multiplier=Decimal(1),
        valid_from_ns=0,
        knowledge_ns=0,
        identifiers=Identifiers(),
        base_currency="BTC",
    )
    events = [
        InstrumentDefined(
            id=uuid.UUID(int=7),
            source="ingest/test",
            ts_event_ns=0,
            ts_ingest_ns=0,
            instrument=pair,
        ),
        _rate(EUR_SERIES, "1.1211", "USD per EUR", T0 + HOUR),
        _rate(EUR_SERIES, "1.1300", "USD per EUR", T0 + HOUR, revision=1),
        _rate(JPY_SERIES, "149.82", "JPY per USD", T0 + HOUR),
        _bar(T0, HOUR, "60000.00"),
        _bar(T0 + HOUR - 60 * 10**9, 60 * 10**9, "60100.00"),
    ]
    records = [LogRecord(sequence=i, event=e) for i, e in enumerate(events)]
    entry = {"source": "s", "read_on": datetime.date(2026, 10, 28)}
    config = PortfolioConfig.model_validate(
        {
            "currencies": {
                "USD": {"kind": "fiat", "minor_units": 2, **entry},
                "EUR": {"kind": "fiat", "minor_units": 2, **entry},
                "JPY": {"kind": "fiat", "minor_units": 0, **entry},
                "BTC": {"kind": "crypto", "minor_units": 8, **entry},
            },
            "rates": {
                "EUR": {"series": str(EUR_SERIES)},
                "JPY": {"series": str(JPY_SERIES)},
                "BTC": {"instrument": str(BTC_USD)},
            },
        },
        strict=False,
    )
    return Valuation(config, MarketView(records), ReferenceIndex.from_records(records))


def test_conversion_follows_the_published_unit() -> None:
    """'USD per EUR' multiplies, 'JPY per USD' divides; one rounding to cents (T12, D1)."""
    valuation = _valuation()
    eur = valuation.convert(Decimal(1000), "EUR", at_ns=T0 + HOUR, known_ns=T0 + HOUR)
    assert eur is not None
    assert (eur.rate, eur.unit, eur.value) == (Decimal("1.1211"), "USD per EUR", Decimal("1121.10"))
    assert (eur.source, eur.rate_available_ns) == (str(EUR_SERIES), T0 + HOUR)
    corrected = valuation.convert(Decimal(1000), "EUR", at_ns=T0 + HOUR, known_ns=T0 + HOUR + 1)
    assert corrected is not None
    assert corrected.value == Decimal("1130.00"), "the revision known at known_ns"
    jpy = valuation.convert(Decimal(1000000), "JPY", at_ns=T0 + HOUR, known_ns=T0 + HOUR)
    assert jpy is not None
    assert jpy.value == Decimal("6674.68")  # 1,000,000 / 149.82 = 6674.676...


def test_crypto_rate_is_the_latest_available_close() -> None:
    """The last close available at the decision time, across intervals (T12, BP6 4.4)."""
    valuation = _valuation()
    btc = valuation.convert(Decimal("0.5"), "BTC", at_ns=T0 + HOUR, known_ns=T0 + HOUR)
    assert btc is not None
    assert (btc.rate, btc.unit, btc.value) == (
        Decimal("60100.00"),
        "USD per BTC",
        Decimal("30050.00"),
    )
    assert btc.source == str(BTC_USD)


def test_missing_rate_is_unavailable_never_estimated() -> None:
    """No released rate, no source, or an unusable unit -> None (T12, P4)."""
    valuation = _valuation()
    assert valuation.convert(Decimal(1), "EUR", at_ns=T0, known_ns=T0 + HOUR) is None
    assert valuation.convert(Decimal(1), "GBP", at_ns=T0 + HOUR, known_ns=T0 + HOUR) is None
    assert valuation.convert(Decimal(1), "BTC", at_ns=T0, known_ns=T0) is None
    with pytest.raises(ValueError, match="no conversion"):
        valuation.convert(Decimal(1), "USD", at_ns=T0, known_ns=T0)


def test_valuation_needs_the_base_currency_declared() -> None:
    """USD's minor units round every value, so USD must be configured."""
    with pytest.raises(ConfigurationError, match="USD"):
        Valuation(PortfolioConfig(), MarketView([]), ReferenceIndex())


def _bond(day_count: DayCount, frequency: int) -> Bond:
    return Bond(
        id=InstrumentId("UST", "T-6.75-2005"),
        quote_currency="USD",
        price_increment=Decimal("0.0078125"),
        quantity_increment=Decimal(1000),
        multiplier=Decimal("0.01"),
        valid_from_ns=0,
        knowledge_ns=0,
        identifiers=Identifiers(),
        face_value=Decimal(1000),
        coupon_rate=Decimal("0.0675") if frequency else Decimal(0),
        coupon_frequency=frequency,
        day_count=day_count,
        issue_ns=0,
        maturity_ns=10**18,
    )


def test_treasury_rule_only_for_semiannual_coupons() -> None:
    """ACT_ACT_TREASURY divides a semiannual payment: other frequencies are refused (F-E1)."""
    assert require_bond_day_count(_bond(DayCount.ACT_ACT_TREASURY, 2)) is DayCount.ACT_ACT_TREASURY
    assert require_bond_day_count(_bond(DayCount.THIRTY_360, 1)) is DayCount.THIRTY_360
    with pytest.raises(DayCountNotSupportedError, match="semiannual"):
        require_bond_day_count(_bond(DayCount.ACT_ACT_TREASURY, 4))
    with pytest.raises(DayCountNotSupportedError, match="act_act_icma"):
        require_bond_day_count(_bond(DayCount.ACT_ACT_ICMA, 2))
