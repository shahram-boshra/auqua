# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Reconciliation with synthetic statements (BP6 section 4.7; PC-7; T13)."""

import uuid
from decimal import Decimal

import msgspec
import pytest

from auqua.contracts.events.account import (
    AccountStatement,
    Difference,
    ReconciliationStatus,
    StatementBalance,
    StatementPosition,
)
from auqua.contracts.identity import InstrumentId
from auqua.contracts.portfolio import AccountView, Balance, LogPosition, PositionView
from auqua.portfolio.errors import PortfolioError, ReconciliationConflictError
from auqua.portfolio.reconcile import Reconciler, statement_sha256

T0 = 1_788_220_800 * 10**9
BTC_USD = InstrumentId("COINBASE", "BTC-USD")
ES = InstrumentId("CME", "ESZ6")


def _view(usd: str = "1000.00", btc_position: str = "0.5") -> AccountView:
    return AccountView(
        account_id="primary",
        as_of_ns=T0,
        log_position=LogPosition(next_sequence=7, hash="a" * 64),
        equity_base=None,
        balances=(
            Balance("BTC", Decimal("0.5")),
            Balance("EUR", Decimal(0)),
            Balance("USD", Decimal(usd), receivable=Decimal("26.00")),
        ),
        positions=(PositionView(BTC_USD, "USD", Decimal(btc_position)),),
        unconverted=("BTC", "EUR"),
    )


def _statement(
    statement_id: str = "venue-2026-10-29",
    usd: str = "1000.00",
    btc_position: str = "0.5",
    extra: tuple[StatementPosition, ...] = (),
) -> AccountStatement:
    return AccountStatement(
        id=uuid.UUID(int=1),
        source="venue/sim",
        ts_event_ns=T0,
        ts_ingest_ns=T0 + 1,
        statement_id=statement_id,
        as_of_ns=T0,
        balances=(StatementBalance("BTC", Decimal("0.5")), StatementBalance("USD", Decimal(usd))),
        positions=tuple(
            sorted(
                (StatementPosition(BTC_USD, Decimal(btc_position)), *extra),
                key=lambda p: str(p.instrument_id),
            )
        ),
    )


def test_matched_statement() -> None:
    """Equal cash and positions match; a zero balance and a missing one agree."""
    reconciler = Reconciler()
    result = reconciler.reconcile(_view(), _statement(), recorded_ns=T0 + 1)
    assert (result.status, result.differences) == (ReconciliationStatus.MATCHED, ())
    assert result.statement_sha256 == statement_sha256(_statement())
    assert (result.ts_event_ns, result.ts_ingest_ns) == (T0, T0 + 1)
    assert reconciler.open_breaks == ()


def test_break_lists_every_difference() -> None:
    """Cash compared without the receivable; positions by filled quantity; absent counts as 0."""
    reported = _statement(
        usd="999.99", btc_position="0.4", extra=(StatementPosition(ES, Decimal(1)),)
    )
    result = Reconciler().reconcile(_view(), reported, recorded_ns=T0 + 1)
    assert result.status is ReconciliationStatus.BREAK
    assert result.differences == (
        Difference("balance:USD", Decimal("1000.00"), Decimal("999.99")),
        Difference("position:CME:ESZ6", None, Decimal(1)),
        Difference("position:COINBASE:BTC-USD", Decimal("0.5"), Decimal("0.4")),
    )


def test_repeat_yields_one_result_and_a_conflict_is_refused() -> None:
    """The same statement twice: the first result again (same id); other content: refused."""
    reconciler = Reconciler()
    first = reconciler.reconcile(_view(usd="1.00"), _statement(), recorded_ns=T0 + 1)
    again = reconciler.reconcile(_view(), _statement(), recorded_ns=T0 + 9)
    assert again == first
    assert reconciler.open_breaks == (first,)
    with pytest.raises(ReconciliationConflictError):
        reconciler.reconcile(_view(), _statement(usd="2.00"), recorded_ns=T0 + 2)
    other = Reconciler().reconcile(_view(usd="1.00"), _statement(), recorded_ns=T0 + 5)
    assert other.id == first.id  # derived from (statement_id, statement_sha256)


def test_resolution_closes_an_open_break() -> None:
    """A later matched result naming the break in ``resolves`` closes it (operator, BP11)."""
    reconciler = Reconciler()
    broken = reconciler.reconcile(_view(), _statement(usd="999.99"), recorded_ns=T0 + 1)
    assert reconciler.open_breaks == (broken,)
    with pytest.raises(PortfolioError, match="resolves nothing"):
        reconciler.reconcile(
            _view(), _statement("s2", usd="999.98"), recorded_ns=T0 + 2, resolves=broken.id
        )
    with pytest.raises(PortfolioError, match="not an open break"):
        reconciler.reconcile(
            _view(), _statement("s3"), recorded_ns=T0 + 3, resolves=uuid.UUID(int=9)
        )
    fixed = reconciler.reconcile(_view(), _statement("s4"), recorded_ns=T0 + 4, resolves=broken.id)
    assert (fixed.status, fixed.resolves) == (ReconciliationStatus.MATCHED, broken.id)
    assert not reconciler.open_breaks


def test_results_replay_into_the_same_state() -> None:
    """A reconciler rebuilt from recorded results keeps the open breaks and refuses conflicts."""
    first = Reconciler()
    broken = first.reconcile(_view(), _statement(usd="999.99"), recorded_ns=T0 + 1)
    replayed = Reconciler([broken, broken])
    assert replayed.open_breaks == (broken,)
    conflicting = msgspec.structs.replace(broken, statement_sha256="b" * 64)
    with pytest.raises(ReconciliationConflictError):
        replayed.apply(conflicting)
