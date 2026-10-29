# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Corporate actions, options and bonds (BP6 section 4.5 A, C, F; T4, T7, T11, T17, T18)."""

import uuid
from decimal import Decimal

import msgspec
import pytest

from auqua.contracts.events.lifecycle import (
    CorporateActionKind,
    CorporateActionV2,
    CouponPayment,
    OptionAssignment,
    OptionExercise,
)
from auqua.contracts.events.trading import Fill, Side
from auqua.contracts.identity import InstrumentId
from auqua.contracts.instruments import (
    Bond,
    DayCount,
    ExerciseStyle,
    Identifiers,
    Option,
    OptionRight,
    Settlement,
)
from auqua.contracts.portfolio import Posting, PostingKind
from auqua.portfolio.bonds import bond_fill_postings, coupon_posting
from auqua.portfolio.corporate import (
    dividend_payment,
    dividend_receivable,
    require_applicable_dividend,
    split_multiplier,
)
from auqua.portfolio.errors import (
    CorporateActionLateError,
    CorporateActionUnsupportedError,
    PortfolioError,
)
from auqua.portfolio.lots import Lots
from auqua.portfolio.options import (
    Delivery,
    cash_settlement_amount,
    exercise_effect,
    premium_postings,
)

ACCOUNT = "primary"
CENT = Decimal("0.01")
DAY = 86_400 * 10**9
EX = 100 * DAY
AAPL = InstrumentId("XNAS", "AAPL")
XYZ_CALL = InstrumentId("OCC", "XYZ-C50")
SPX = InstrumentId("CBOE", "SPX-K100")
NOTE = InstrumentId("UST", "T-6.75-2005")
CAUSE = uuid.UUID(int=10)


def _action(kind: CorporateActionKind, **terms: object) -> CorporateActionV2:
    return msgspec.convert(
        {
            "id": str(uuid.UUID(int=20)),
            "source": "ingest/test",
            "ts_event_ns": EX - 10 * DAY,
            "ts_ingest_ns": EX - 10 * DAY,
            "instrument_id": {"venue": AAPL.venue, "symbol": AAPL.symbol},
            "kind": kind.value,
            "effective_ns": EX,
            **terms,
        },
        CorporateActionV2,
    )


def _dividend(pay_ns: int = EX + 14 * DAY) -> CorporateActionV2:
    return _action(
        CorporateActionKind.CASH_DIVIDEND,
        cash_amount="0.26",
        currency="USD",
        ex_date_ns=EX,
        record_date_ns=EX,
        pay_date_ns=pay_ns,
    )


def _fill(
    instrument_id: InstrumentId,
    side: Side,
    quantity: str,
    price: str,
    accrued_interest: str | None = None,
) -> Fill:
    return Fill(
        id=uuid.UUID(int=11),
        source="execution/sim",
        ts_event_ns=EX,
        ts_ingest_ns=EX,
        causation_id=CAUSE,
        instrument_id=instrument_id,
        side=side,
        price=Decimal(price),
        quantity=Decimal(quantity),
        fee=Decimal("1.00"),
        fee_currency="USD",
        accrued_interest=None if accrued_interest is None else Decimal(accrued_interest),
    )


# --- T17: cash dividend ---------------------------------------------------------------------------
def test_dividend_entitlement_is_fixed_at_the_ex_date() -> None:
    """100 held before the ex-date, 50 bought on it: 100 x 0.26 receivable, cash on the pay date."""
    action = _dividend()
    require_applicable_dividend(action, record_ns=EX - 1)
    held_before_ex = Decimal(100)  # the 50 bought on the ex-date are not entitled
    receivable = dividend_receivable(action, held_before_ex, ACCOUNT)
    assert receivable == Posting(
        ACCOUNT, "USD", Decimal("26.00"), PostingKind.RECEIVABLE, action.id
    )
    assert dividend_payment(action, held_before_ex, ACCOUNT) == (
        Posting(ACCOUNT, "USD", Decimal("-26.00"), PostingKind.RECEIVABLE, action.id),
        Posting(ACCOUNT, "USD", Decimal("26.00"), PostingKind.DIVIDEND, action.id),
    )
    assert dividend_receivable(action, Decimal(0), ACCOUNT) is None
    assert dividend_payment(action, Decimal(0), ACCOUNT) == ()


def test_dividend_refusals() -> None:
    """Recorded at or after the ex-date: late; paid before the ex-date: unsupported (F-G)."""
    with pytest.raises(CorporateActionLateError):
        require_applicable_dividend(_dividend(), record_ns=EX)
    with pytest.raises(CorporateActionUnsupportedError, match="F-G"):
        require_applicable_dividend(_dividend(pay_ns=EX - DAY), record_ns=EX - 2 * DAY)
    with pytest.raises(PortfolioError, match="not a cash dividend"):
        require_applicable_dividend(_action(CorporateActionKind.SPLIT, ratio="2"), record_ns=0)
    with pytest.raises(PortfolioError, match=">= 0"):
        dividend_receivable(_dividend(), Decimal(-1), ACCOUNT)


# --- T4: split and stock dividend multipliers ----------------------------------------------------
def test_split_and_stock_dividend_multipliers() -> None:
    """2-for-1 split: 2; stock dividend of 0.05 per unit: 1.05; applied to the lots (IRS FAQ)."""
    split = _action(CorporateActionKind.SPLIT, ratio="2")
    assert split.effective_ns == EX  # the start of the ex-date (amendment 2, F-G2)
    lots = Lots(CENT)
    lots.trade(Decimal(100), Decimal(10))
    lots.split(split_multiplier(split))
    assert (lots.position, lots.cost, lots.lots()[0].unit_cost) == (
        Decimal(200),
        Decimal(1000),
        Decimal(5),
    )
    stock = _action(CorporateActionKind.STOCK_DIVIDEND, ratio="0.05")
    assert split_multiplier(stock) == Decimal("1.05")
    with pytest.raises(PortfolioError, match="no lot multiplier"):
        split_multiplier(_action(CorporateActionKind.DELISTING))


# --- T7, T18: options ----------------------------------------------------------------------------
def _option(
    instrument_id: InstrumentId, right: OptionRight, strike: str, settlement: Settlement
) -> Option:
    return Option(
        id=instrument_id,
        quote_currency="USD",
        price_increment=CENT,
        quantity_increment=Decimal(1),
        multiplier=Decimal(100),
        valid_from_ns=0,
        knowledge_ns=0,
        identifiers=Identifiers(),
        underlying="XYZ",
        right=right,
        strike=Decimal(strike),
        expiry_ns=EX + 30 * DAY,
        exercise_style=ExerciseStyle.AMERICAN,
        settlement=settlement,
    )


def _exercise(
    instrument_id: InstrumentId, value: str | None = None, *, assigned: bool = False
) -> OptionExercise | OptionAssignment:
    kind = OptionAssignment if assigned else OptionExercise
    return kind(
        id=uuid.UUID(int=30),
        source="ingest/test",
        ts_event_ns=EX,
        ts_ingest_ns=EX,
        instrument_id=instrument_id,
        quantity=Decimal(1),
        exercise_settlement_value=None if value is None else Decimal(value),
    )


def test_premium_is_price_times_multiplier() -> None:
    """Premium x 100: the buyer pays, the writer receives; the fee apart (OCC)."""
    call = _option(XYZ_CALL, OptionRight.CALL, "50", Settlement.PHYSICAL)
    bought = premium_postings(_fill(XYZ_CALL, Side.BUY, "2", "1.25"), call, ACCOUNT)
    assert [(p.amount, p.kind) for p in bought] == [
        (Decimal(-250), PostingKind.PREMIUM),
        (Decimal("-1.00"), PostingKind.FEE),
    ]
    written = premium_postings(_fill(XYZ_CALL, Side.SELL, "2", "1.25"), call, ACCOUNT)
    assert written[0].amount == Decimal(250)


def test_occ_physical_exercise() -> None:
    """Exercise 1 XYZ 50 call: pay $5,000 ($50 x 100) for 100 shares; assignment: the reverse."""
    call = _option(XYZ_CALL, OptionRight.CALL, "50", Settlement.PHYSICAL)
    result = exercise_effect(_exercise(XYZ_CALL), call, ACCOUNT)
    assert [(p.amount, p.kind) for p in result.postings] == [(Decimal(-5000), PostingKind.EXERCISE)]
    assert result.delivery == Delivery("XYZ", Decimal(100), Decimal(50))
    assigned = exercise_effect(_exercise(XYZ_CALL, assigned=True), call, ACCOUNT)
    assert assigned.postings[0].amount == Decimal(5000)
    assert assigned.delivery == Delivery("XYZ", Decimal(-100), Decimal(50))


@pytest.mark.parametrize(
    ("right", "value", "assigned", "amount"),
    [
        (OptionRight.CALL, "110", False, "1000"),
        (OptionRight.CALL, "90", False, "0"),
        (OptionRight.PUT, "90", False, "1000"),
        (OptionRight.PUT, "110", False, "0"),
        (OptionRight.CALL, "110", True, "-1000"),
        (OptionRight.CALL, "90", True, "0"),
    ],
)
def test_cash_settlement_reproduces_occ(
    right: OptionRight, value: str, assigned: bool, amount: str
) -> None:
    """K = 100, M = 100: max(0, S - K) for a call, max(0, K - S) for a put, x M x q (T18)."""
    option = _option(SPX, right, "100", Settlement.CASH)
    quantity = Decimal(-1) if assigned else Decimal(1)
    assert cash_settlement_amount(option, quantity, Decimal(value)) == Decimal(amount)
    result = exercise_effect(_exercise(SPX, value, assigned=assigned), option, ACCOUNT)
    assert sum((p.amount for p in result.postings), Decimal(0)) == Decimal(amount)
    assert result.delivery is None


def test_exercise_refusals() -> None:
    """Cash-settled without S, physical with S (F-D), and a physical put (R4) are refused."""
    cash_call = _option(SPX, OptionRight.CALL, "100", Settlement.CASH)
    with pytest.raises(PortfolioError, match="F-D"):
        exercise_effect(_exercise(SPX), cash_call, ACCOUNT)
    physical_call = _option(XYZ_CALL, OptionRight.CALL, "50", Settlement.PHYSICAL)
    with pytest.raises(PortfolioError, match="F-D"):
        exercise_effect(_exercise(XYZ_CALL, "55"), physical_call, ACCOUNT)
    physical_put = _option(XYZ_CALL, OptionRight.PUT, "50", Settlement.PHYSICAL)
    with pytest.raises(PortfolioError, match="R4"):
        exercise_effect(_exercise(XYZ_CALL), physical_put, ACCOUNT)
    with pytest.raises(PortfolioError, match="option"):
        exercise_effect(_exercise(SPX, "110"), physical_call, ACCOUNT)


# --- T11 (amendment 2): bond fills and coupons ---------------------------------------------------
def _note(coupon_rate: str = "0.0675") -> Bond:
    zero = Decimal(coupon_rate).is_zero()
    return Bond(
        id=NOTE,
        quote_currency="USD",
        price_increment=Decimal("0.0078125"),
        quantity_increment=Decimal(1000),
        multiplier=Decimal("0.01"),
        valid_from_ns=0,
        knowledge_ns=0,
        identifiers=Identifiers(),
        face_value=Decimal(1000),
        coupon_rate=Decimal(coupon_rate),
        coupon_frequency=0 if zero else 2,
        day_count=DayCount.ACT_ACT_TREASURY,
        issue_ns=0,
        maturity_ns=10**18,
    )


def test_bond_fill_posts_clean_notional_and_reported_accrued() -> None:
    """$150,000 at 101.5: notional -152,250.00; accrued $2,531.25 as reported (31 CFR 356)."""
    buy = _fill(NOTE, Side.BUY, "150000", "101.5", accrued_interest="2531.25")
    assert [(p.amount, p.kind) for p in bond_fill_postings(buy, _note(), ACCOUNT)] == [
        (Decimal("-152250.000"), PostingKind.TRADE),
        (Decimal("-2531.25"), PostingKind.ACCRUED),
        (Decimal("-1.00"), PostingKind.FEE),
    ]
    sell = _fill(NOTE, Side.SELL, "150000", "101.5", accrued_interest="2531.25")
    assert [p.amount for p in bond_fill_postings(sell, _note(), ACCOUNT)][:2] == [
        Decimal("152250.000"),
        Decimal("2531.25"),
    ]


def test_bond_fill_without_accrued_interest() -> None:
    """Refused with a coupon; a zero-coupon fill is accepted without it (F-E2)."""
    with pytest.raises(PortfolioError, match="F-E2"):
        bond_fill_postings(_fill(NOTE, Side.BUY, "1000", "99"), _note(), ACCOUNT)
    postings = bond_fill_postings(_fill(NOTE, Side.BUY, "1000", "95"), _note("0"), ACCOUNT)
    assert [p.kind for p in postings] == [PostingKind.TRADE, PostingKind.FEE]


def test_coupon_is_amount_per_unit_times_units_held() -> None:
    """Coupon = amount_per_unit x units held, in the event's currency."""
    event = CouponPayment(
        id=uuid.UUID(int=40),
        source="ingest/test",
        ts_event_ns=EX,
        ts_ingest_ns=EX,
        instrument_id=NOTE,
        payment_ns=EX,
        amount_per_unit=Decimal("0.03375"),
        currency="USD",
    )
    posting = coupon_posting(event, _note(), Decimal(150000), ACCOUNT)
    assert posting == Posting(ACCOUNT, "USD", Decimal("5062.50000"), PostingKind.COUPON, event.id)
    assert coupon_posting(event, _note(), Decimal(0), ACCOUNT) is None
    with pytest.raises(PortfolioError, match=">= 0"):
        coupon_posting(event, _note(), Decimal(-1), ACCOUNT)
