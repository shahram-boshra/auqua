# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Tests for the account view read by risk (BP6 sections 4.2, 4.4, 4.6, 4.8; T16)."""

import decimal
import uuid
from decimal import Decimal
from typing import Any

import msgspec
import pytest

from auqua.contracts.errors import InvalidFieldError
from auqua.contracts.identity import InstrumentId
from auqua.contracts.numeric import exact
from auqua.contracts.portfolio import (
    AccountView,
    Balance,
    Conversion,
    LogPosition,
    MarginView,
    PositionView,
    Posting,
    PostingKind,
)

BTC = InstrumentId("COINBASE", "BTC-USD")
ETH = InstrumentId("COINBASE", "ETH-USD")
T0 = 1_788_220_800_000_000_000
HEAD = LogPosition(next_sequence=12, hash="a" * 64)


def _view(**changes: Any) -> AccountView:  # noqa: ANN401 - test overrides
    fields: dict[str, Any] = {
        "account_id": "research",
        "as_of_ns": T0,
        "log_position": HEAD,
        "equity_base": Decimal("100000.00"),
    } | changes
    return AccountView(**fields)


def _eur_conversion() -> Conversion:
    return Conversion(
        amount=Decimal(1000),
        from_currency="EUR",
        to_currency="USD",
        rate=Decimal("1.1211"),
        unit="USD per EUR",
        source="frb/h10/daily",
        rate_available_ns=T0,
        value=Decimal("1121.10"),
    )


def test_posting_rules() -> None:
    """A posting moves a non-zero amount of a known currency for a named account."""
    posting = Posting("research", "USD", Decimal("-9.38"), PostingKind.FEE, uuid.UUID(int=1))
    assert posting.kind is PostingKind.FEE
    with pytest.raises(InvalidFieldError, match="non-zero"):
        Posting("research", "USD", Decimal(0), PostingKind.FEE, uuid.UUID(int=1))
    with pytest.raises(InvalidFieldError, match="currency"):
        Posting("research", "usd", Decimal(1), PostingKind.FEE, uuid.UUID(int=1))
    with pytest.raises(InvalidFieldError, match="trimmed"):
        Posting("", "USD", Decimal(1), PostingKind.FEE, uuid.UUID(int=1))


def test_position_keeps_filled_and_working_apart() -> None:
    """``quantity`` and ``working`` are separate; decisions count both (C7, BP5 L7)."""
    position = PositionView(BTC, "USD", Decimal("0.5"), working=Decimal("-0.2"))
    assert (position.quantity, position.working) == (Decimal("0.5"), Decimal("-0.2"))
    assert position.exposure_quantity == Decimal("0.3")
    marked = PositionView(
        BTC,
        "USD",
        Decimal("0.5"),
        mark=Decimal(60000),
        mark_available_ns=T0,
        unrealized=Decimal(250),
    )
    assert marked.unrealized == Decimal(250)
    with pytest.raises(InvalidFieldError, match="come together"):
        PositionView(BTC, "USD", Decimal("0.5"), mark=Decimal(60000))
    with pytest.raises(InvalidFieldError, match="come together"):
        PositionView(BTC, "USD", Decimal("0.5"), unrealized=Decimal(1))


def test_margin_values_are_consistent_and_exact() -> None:
    """The available margin and the deficit follow from equity and the requirement (BP6 4.6)."""
    ok = MarginView("USD", Decimal(500), Decimal(400), Decimal(450), Decimal(-50), Decimal(0))
    assert ok.deficit == 0
    short = MarginView("USD", Decimal(500), Decimal(400), Decimal(300), Decimal(-200), Decimal(100))
    assert short.deficit == Decimal(100)
    with pytest.raises(InvalidFieldError, match="available"):
        MarginView("USD", Decimal(500), Decimal(400), Decimal(450), Decimal(0), Decimal(0))
    with pytest.raises(InvalidFieldError, match="deficit"):
        MarginView("USD", Decimal(500), Decimal(400), Decimal(300), Decimal(-200), Decimal(0))


def test_conversion_rules() -> None:
    """A conversion names two currencies, a positive rate, its unit, source and time."""
    assert _eur_conversion().value == Decimal("1121.10")
    with pytest.raises(InvalidFieldError, match="two currencies"):
        msgspec.structs.replace(_eur_conversion(), from_currency="USD")
    with pytest.raises(InvalidFieldError, match="rate"):
        msgspec.structs.replace(_eur_conversion(), rate=Decimal(0))
    with pytest.raises(InvalidFieldError, match="unit"):
        msgspec.structs.replace(_eur_conversion(), unit="")


def test_account_view_completeness_markers() -> None:
    """Base equity is unavailable exactly when a currency is unconverted; margin likewise."""
    view = _view(
        balances=(Balance("EUR", Decimal(1000)), Balance("USD", Decimal("98878.90"))),
        positions=(PositionView(BTC, "USD", Decimal(1)),),
        conversions=(_eur_conversion(),),
    )
    assert view.margin_complete
    unconverted = _view(equity_base=None, unconverted=("EUR",))
    assert unconverted.equity_base is None
    with pytest.raises(InvalidFieldError, match="unconverted"):
        _view(equity_base=None)
    with pytest.raises(InvalidFieldError, match="unconverted"):
        _view(unconverted=("EUR",))
    unmarked = _view(equity_base=None, unmarked=(BTC,))  # amendment 7
    assert (unmarked.equity_base, unmarked.unconverted) == (None, ())
    with pytest.raises(InvalidFieldError, match="unmarked"):
        _view(unmarked=(BTC,))
    with pytest.raises(InvalidFieldError, match="increasing"):
        _view(equity_base=None, unmarked=(ETH, BTC))
    assert not _view(
        positions=(PositionView(BTC, "USD", Decimal(1)),), unmargined=(BTC,)
    ).margin_complete
    with pytest.raises(InvalidFieldError, match="into USD"):
        _view(conversions=(msgspec.structs.replace(_eur_conversion(), to_currency="GBP"),))


def test_account_view_rows_are_canonical() -> None:
    """Rows are in strictly increasing key order, so equal views encode equally."""
    with pytest.raises(InvalidFieldError, match="balances"):
        _view(balances=(Balance("USD", Decimal(1)), Balance("EUR", Decimal(1))))
    with pytest.raises(InvalidFieldError, match="positions"):
        _view(
            positions=(PositionView(ETH, "USD", Decimal(1)), PositionView(BTC, "USD", Decimal(1)))
        )
    with pytest.raises(InvalidFieldError, match="receivable"):
        Balance("USD", Decimal(1), receivable=Decimal(-1))
    with pytest.raises(InvalidFieldError, match="hex"):
        LogPosition(next_sequence=0, hash="0")
    with pytest.raises(InvalidFieldError, match="next_sequence"):
        LogPosition(next_sequence=-1, hash="0" * 64)
    encoded = msgspec.json.encode(_view())
    assert msgspec.json.decode(encoded, type=AccountView) == _view()


def test_exact_arithmetic_raises_instead_of_rounding() -> None:
    """``numeric.exact`` traps Inexact: a sum that would round raises (BP6 section 7)."""
    with exact():
        assert Decimal("0.1") + Decimal("0.2") == Decimal("0.3")
    with exact(), pytest.raises(decimal.Inexact):
        _ = Decimal(1) / Decimal(3)
