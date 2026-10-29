# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Portfolio ledger and lots (BP6 sections 4.2 and 4.3; T2-T5)."""

import uuid
from collections import deque
from decimal import Decimal
from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from auqua.contracts.portfolio import Posting, PostingKind
from auqua.portfolio.errors import LotError, PortfolioError
from auqua.portfolio.ledger import Ledger
from auqua.portfolio.lots import Lots

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


def test_relief_without_a_split_never_rounds() -> None:
    """A lot opened at a fill price relieves unit_cost * u exactly (PC-10)."""
    lots = Lots(CENT)
    lots.trade(Decimal("0.5"), Decimal("93800.10"))
    assert lots.trade(Decimal("-0.001"), Decimal(1)).relieved_cost == Decimal("93.80010")
