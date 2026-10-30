# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""The account fold and its view (BP6 sections 4.8, 4.10; amendment 6; T1, P1-P4, P10)."""

import datetime
import uuid
import zoneinfo
from decimal import Decimal
from pathlib import Path
from typing import TypedDict

import msgspec
import pytest

from auqua.config.base import Environment
from auqua.config.identity import config_id
from auqua.config.portfolio import PortfolioConfig
from auqua.contracts.codec import encode
from auqua.contracts.events.account import (
    AccountSnapshot,
    AccountStatement,
    CashTransfer,
    FxBreak,
    FxSettlementBreak,
    ReconciliationResult,
    ReconciliationStatus,
    StatementBalance,
)
from auqua.contracts.events.base import Event, LogRecord
from auqua.contracts.events.lifecycle import (
    CorporateActionKind,
    CorporateActionV2,
    FxSettlement,
)
from auqua.contracts.events.market import Bar, SettlementPrice
from auqua.contracts.events.reference import InstrumentDefined
from auqua.contracts.events.trading import Fill, OrderSubmitted, OrderType, RiskDecision, Side
from auqua.contracts.identity import InstrumentId
from auqua.contracts.instruments import (
    AnyInstrument,
    CryptoSpot,
    Equity,
    ExerciseStyle,
    Future,
    FxDelivery,
    FxPair,
    Identifiers,
    Option,
    OptionRight,
    PerpetualSwap,
    PerpetualType,
    Settlement,
)
from auqua.contracts.portfolio import PostingKind
from auqua.portfolio.account import Portfolio, PortfolioState
from auqua.portfolio.errors import CorporateActionLateError, PortfolioError
from auqua.portfolio.store import MIGRATIONS, catch_up, content_sha256
from auqua.storage.chain import GENESIS, record_hash
from auqua.storage.eventlog import EventLog
from auqua.storage.marketview import MarketView
from auqua.storage.reference import ReferenceIndex
from auqua.storage.replay import EventLogReader
from auqua.storage.store import open_store

CHICAGO = zoneinfo.ZoneInfo("America/Chicago")
CENT = Decimal("0.01")
AAPL = InstrumentId("XNAS", "AAPL")
ES = InstrumentId("CME", "ESZ6")
BTC_USD = InstrumentId("COINBASE", "BTC-USD")
PERP = InstrumentId("DERIBIT", "BTC-PERPETUAL")
EUR_USD = InstrumentId("FX", "EUR/USD")
READ = {"source": "s", "read_on": datetime.date(2026, 10, 29)}


def ct(day: int, hour: int, minute: int = 0) -> int:
    """Nanoseconds of 2026-10-<day> <hour>:<minute> in Chicago."""
    local = datetime.datetime(2026, 10, day, hour, minute, tzinfo=CHICAGO)
    return int(local.timestamp()) * 10**9


def _uuid(n: int) -> uuid.UUID:
    return uuid.UUID(int=n)


class _Common(TypedDict):
    id: uuid.UUID
    source: str
    ts_event_ns: int
    ts_ingest_ns: int


class _Versioned(TypedDict):
    valid_from_ns: int
    knowledge_ns: int
    identifiers: Identifiers


def _common(n: int, at: int) -> _Common:
    return {"id": _uuid(n), "source": "test/scenario", "ts_event_ns": at, "ts_ingest_ns": at}


def _identifiers() -> _Versioned:
    return {"valid_from_ns": 0, "knowledge_ns": 0, "identifiers": Identifiers()}


INSTRUMENTS: tuple[AnyInstrument, ...] = (
    Equity(
        id=AAPL,
        quote_currency="USD",
        price_increment=CENT,
        quantity_increment=Decimal(1),
        multiplier=Decimal(1),
        is_fund=False,
        **_identifiers(),
    ),
    Future(
        id=ES,
        quote_currency="USD",
        price_increment=Decimal("0.25"),
        quantity_increment=Decimal(1),
        multiplier=Decimal(50),
        underlying="SPX",
        expiry_ns=ct(31, 8),
        last_trade_ns=ct(31, 8),
        settlement=Settlement.CASH,
        **_identifiers(),
    ),
    CryptoSpot(
        id=BTC_USD,
        quote_currency="USD",
        price_increment=CENT,
        quantity_increment=Decimal("0.00000001"),
        multiplier=Decimal(1),
        base_currency="BTC",
        **_identifiers(),
    ),
    PerpetualSwap(
        id=PERP,
        quote_currency="USD",
        price_increment=Decimal("0.5"),
        quantity_increment=Decimal(1),
        multiplier=Decimal(10),
        base_currency="BTC",
        settlement_currency="BTC",
        contract_type=PerpetualType.INVERSE,
        funding_interval_ns=8 * 3_600 * 10**9,
        **_identifiers(),
    ),
    FxPair(
        id=EUR_USD,
        quote_currency="USD",
        price_increment=Decimal("0.0001"),
        quantity_increment=Decimal(1),
        multiplier=Decimal(1),
        base_currency="EUR",
        delivery=FxDelivery.SPOT,
        settlement_lag_days=2,
        **_identifiers(),
    ),
)


def _config(**changes: object) -> PortfolioConfig:
    document: dict[str, object] = {
        "currencies": {
            "USD": {"kind": "fiat", "minor_units": 2, **READ},
            "EUR": {"kind": "fiat", "minor_units": 2, **READ},
            "BTC": {"kind": "crypto", "minor_units": 8, **READ},
        },
        "rates": {"BTC": {"instrument": str(BTC_USD)}},
        "futures": {
            str(ES): {"time_zone": "America/Chicago", "trading_day_end": "16:00:00", **READ}
        },
        "margin": {
            str(ES): {
                "basis": "per_contract",
                "initial": "12000",
                "maintenance": "11000",
                "currency": "USD",
                **READ,
            }
        },
    }
    document.update(changes)
    return PortfolioConfig.model_validate(document, strict=False)


def _bar(n: int, instrument_id: InstrumentId, close: str, at: int) -> Bar:
    price = Decimal(close)
    return Bar(
        **_common(n, at),
        instrument_id=instrument_id,
        start_ns=at - 60 * 10**9,
        interval_ns=60 * 10**9,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=Decimal(1),
    )


def _fill(  # noqa: PLR0913, PLR0917 - one fill, field by field
    n: int,
    instrument_id: InstrumentId,
    side: Side,
    quantity: str,
    price: str,
    at: int,
    order: int = 0,
    fee: str = "0",
) -> Fill:
    return Fill(
        **_common(n, at),
        causation_id=_uuid(order or 10_000 + n),
        instrument_id=instrument_id,
        side=side,
        price=Decimal(price),
        quantity=Decimal(quantity),
        fee=Decimal(fee),
        fee_currency="USD",
    )


def _settlement(n: int, day: int, price: str, revision: int = 0) -> SettlementPrice:
    at = ct(day, 16, 30)
    return SettlementPrice(
        **_common(n, at),
        instrument_id=ES,
        trading_date=datetime.date(2026, 10, day),
        price=Decimal(price),
        ts_published_ns=at,
        revision=revision,
    )


def _scenario() -> list[Event]:
    """Every class the fold supports, in log order (ingest times increasing)."""
    events: list[Event] = [
        InstrumentDefined(**_common(i + 1, ct(25, 0)), instrument=instrument)
        for i, instrument in enumerate(INSTRUMENTS)
    ]
    order = OrderSubmitted(
        **_common(20, ct(26, 8)),
        causation_id=_uuid(19),
        instrument_id=AAPL,
        side=Side.BUY,
        quantity=Decimal(100),
        order_type=OrderType.MARKET,
    )
    accepted = RiskDecision(
        **_common(21, ct(26, 8)),
        causation_id=order.id,
        instrument_id=AAPL,
        accepted=True,
        rule="",
    )
    events += [
        CashTransfer(
            **_common(10, ct(25, 1)), currency="USD", amount=Decimal(500000), reference="t1"
        ),
        order,
        accepted,
        _fill(22, AAPL, Side.BUY, "60", "200.00", ct(26, 9), order=20, fee="1.00"),
        _fill(30, ES, Side.BUY, "2", "4000.00", ct(26, 10)),
        _settlement(31, 26, "4010.25"),
        _fill(32, ES, Side.SELL, "1", "4012.00", ct(27, 9)),
        CorporateActionV2(
            **_common(40, ct(27, 10)),
            instrument_id=AAPL,
            kind=CorporateActionKind.CASH_DIVIDEND,
            effective_ns=ct(28, 0),
            cash_amount=Decimal("0.26"),
            currency="USD",
            ex_date_ns=ct(28, 0),
            record_date_ns=ct(28, 0),
            pay_date_ns=ct(30, 0),
        ),
        _settlement(33, 27, "4005.00"),
        _fill(50, BTC_USD, Side.BUY, "0.5", "60000.00", ct(27, 17), fee="3.00"),
        _bar(51, BTC_USD, "61000.00", ct(27, 18)),
        _bar(52, AAPL, "210.00", ct(27, 18)),
        _fill(60, PERP, Side.BUY, "800", "8000.0", ct(27, 19)),
        _fill(61, PERP, Side.SELL, "800", "10000.0", ct(27, 20)),
        _fill(70, EUR_USD, Side.BUY, "1000000", "1.0850", ct(28, 9)),
        FxSettlement(
            **_common(71, ct(30, 9)),
            instrument_id=EUR_USD,
            value_date_ns=ct(30, 0),
            base_amount=Decimal(1000000),
            quote_amount=Decimal("1084999.00"),
        ),
        AccountStatement(
            **_common(80, ct(30, 18)),
            statement_id="venue-2026-10-30",
            as_of_ns=ct(30, 17),
            balances=(StatementBalance("USD", Decimal("1.00")),),
        ),
    ]
    return events


def _fold(
    events: list[Event], config: PortfolioConfig | None = None
) -> tuple[Portfolio, list[Event]]:
    records = [LogRecord(sequence=i, event=e) for i, e in enumerate(events)]
    portfolio = Portfolio(config or _config(), MarketView(records))
    derived: list[Event] = []
    previous = GENESIS
    for record in records:
        previous = record_hash(previous, record.sequence, encode(record.event))
        derived += portfolio.apply(record, previous)
    return portfolio, derived


# --- T1, P1: determinism --------------------------------------------------------------------------
def test_replay_is_deterministic() -> None:
    """Two folds of one log: identical postings, lots, derived events and snapshot bytes."""
    first, derived_first = _fold(_scenario())
    second, derived_second = _fold(_scenario())
    assert first.postings == second.postings
    assert derived_first == derived_second
    for instrument in INSTRUMENTS:
        mine, theirs = first.lots(instrument.id), second.lots(instrument.id)
        assert (mine and mine.lots()) == (theirs and theirs.lots())
    snapshot = [
        encode(AccountSnapshot(**_common(99, ct(30, 18)), view=p.view())) for p in (first, second)
    ]
    assert snapshot[0] == snapshot[1]


def test_scenario_postings_follow_each_class() -> None:
    """Fee in its currency, futures variation, receivable, spot base, realized coin P&L."""
    portfolio, derived = _fold(_scenario())
    kinds = [(p.currency, p.kind, p.amount) for p in portfolio.postings]
    assert ("USD", PostingKind.TRADE, Decimal("-12000.00")) in kinds
    assert ("USD", PostingKind.FEE, Decimal("-1.00")) in kinds
    assert ("USD", PostingKind.VARIATION, Decimal("1025.00")) in kinds
    # day 2: start-of-day +2 from 4,010.25 to 4,005.00, a sale of 1 at 4,012.00
    assert ("USD", PostingKind.VARIATION, Decimal("-175.00")) in kinds
    assert ("USD", PostingKind.RECEIVABLE, Decimal("15.60")) in kinds  # 60 x 0.26
    assert ("BTC", PostingKind.TRADE, Decimal("0.5")) in kinds
    assert ("BTC", PostingKind.REALIZED, Decimal("0.20000000")) in kinds  # Deribit, T9
    assert ("EUR", PostingKind.TRADE, Decimal(1000000)) in kinds
    receivable = [p for p in portfolio.postings if p.kind is PostingKind.RECEIVABLE]
    assert len(receivable) == 2  # noqa: PLR2004 - fixed at the ex-date, moved to cash at the pay date
    assert ("USD", PostingKind.DIVIDEND, Decimal("15.60")) in kinds
    fx_break, result = derived
    assert isinstance(fx_break, FxSettlementBreak)
    assert fx_break.reason is FxBreak.UNMATCHED
    assert isinstance(result, ReconciliationResult)
    assert result.status is ReconciliationStatus.BREAK


def test_view_marks_working_equity_and_margin() -> None:
    """Working = accepted minus filled; equity per currency (F-R); futures margin (T21)."""
    portfolio, _ = _fold(_scenario())
    view = portfolio.view()
    rows = {str(p.instrument_id): p for p in view.positions}
    assert set(rows) == {str(AAPL), str(ES), str(BTC_USD), str(EUR_USD)}  # amendment 7
    btc = rows[str(BTC_USD)]
    assert (btc.quantity, btc.currency, btc.mark) == (Decimal("0.5"), "USD", Decimal(61000))
    assert btc.unrealized == Decimal(500)  # 0.5 x (61,000 - 60,000)
    eur = rows[str(EUR_USD)]
    assert (eur.quantity, eur.mark) == (Decimal(1000000), None)  # no bar: unmarked
    assert view.unmarked == ()  # a spot pair's value is its base balance
    assert view.unmargined == ()  # equities and spot are outside margin scope
    assert view.margin_complete
    aapl = rows[str(AAPL)]
    assert (aapl.quantity, aapl.working, aapl.mark) == (Decimal(60), Decimal(40), Decimal(210))
    assert aapl.unrealized == Decimal(60) * Decimal(10)
    es = rows[str(ES)]
    assert (es.quantity, es.mark, es.unrealized) == (Decimal(1), Decimal("4005.00"), Decimal(0))
    (usd,) = view.margin
    assert (usd.currency, usd.initial, usd.maintenance) == ("USD", Decimal(12000), Decimal(11000))
    balances = {b.currency: b for b in view.balances}
    expected_usd = balances["USD"].cash + balances["USD"].receivable + Decimal(60 * 210)
    assert usd.equity == expected_usd
    assert view.unconverted == ("EUR",)  # no EUR rate configured
    assert view.equity_base is None
    assert {c.from_currency for c in view.conversions} == {"BTC"}


def test_derived_events_read_back_must_equal_the_derivation() -> None:
    """The caller appends derived events; the fold checks them and never applies them (F-P)."""
    events = _scenario()
    _, derived = _fold(events)
    _fold([*events, *derived])  # read back: accepted
    tampered = msgspec.structs.replace(derived[0], ts_ingest_ns=derived[0].ts_ingest_ns + 1)
    with pytest.raises(PortfolioError, match="differs"):
        _fold([*events, tampered])


# --- F-N: futures trading days ----------------------------------------------------------------
def test_fills_belong_to_their_trading_date() -> None:
    """A fill after the trading-day end waits for the next settlement; settled dates are closed."""
    head = [InstrumentDefined(**_common(1, ct(25, 0)), instrument=INSTRUMENTS[1])]
    late_session = _fill(30, ES, Side.BUY, "1", "4000.00", ct(26, 17))  # after 16:00 CT on the 26th
    portfolio, _ = _fold([*head, late_session, _settlement(31, 26, "4010.00")])
    assert not [p for p in portfolio.postings if p.kind is PostingKind.VARIATION]
    portfolio, _ = _fold(
        [*head, late_session, _settlement(31, 26, "4010.00"), _settlement(32, 27, "4002.00")]
    )
    variation = [p.amount for p in portfolio.postings if p.kind is PostingKind.VARIATION]
    assert variation == [Decimal("100.00")]  # (4002 - 4000) x 50 on the 27th
    early = _fill(33, ES, Side.BUY, "1", "4000.00", ct(26, 15))
    with pytest.raises(PortfolioError, match="settled trading date"):
        _fold([*head, _settlement(31, 26, "4010.00"), early])


@pytest.mark.parametrize(
    ("settlements", "match"),
    [
        ((_settlement(31, 27, "1"), _settlement(32, 26, "1")), "already settled"),
        ((_settlement(31, 26, "1", revision=1),), "R16"),
    ],
)
def test_settlement_refusals(settlements: tuple[SettlementPrice, ...], match: str) -> None:
    """An earlier or equal trading date, or a revision, is refused (R16)."""
    head = [InstrumentDefined(**_common(1, ct(25, 0)), instrument=INSTRUMENTS[1])]
    with pytest.raises(PortfolioError, match=match):
        _fold([*head, *settlements])


def test_future_without_trading_day_is_refused() -> None:
    """No `futures` entry: the fold refuses the future (F-N (c))."""
    head = [InstrumentDefined(**_common(1, ct(25, 0)), instrument=INSTRUMENTS[1])]
    with pytest.raises(PortfolioError, match="F-N"):
        _fold([*head, _fill(30, ES, Side.BUY, "1", "4000", ct(26, 9))], _config(futures={}))


# --- F-O: working orders ----------------------------------------------------------------------
def test_rejected_order_never_works() -> None:
    """An order with a rejected risk decision is not working; an accepted one is until filled."""
    head = [InstrumentDefined(**_common(1, ct(25, 0)), instrument=INSTRUMENTS[0])]
    order = OrderSubmitted(
        **_common(20, ct(26, 8)),
        causation_id=_uuid(19),
        instrument_id=AAPL,
        side=Side.SELL,
        quantity=Decimal(5),
        order_type=OrderType.MARKET,
    )
    rejected = RiskDecision(
        **_common(21, ct(26, 8)),
        causation_id=order.id,
        instrument_id=AAPL,
        accepted=False,
        rule="max_notional",
        limit=Decimal(1),
        value=Decimal(2),
    )
    portfolio, _ = _fold([*head, order, rejected])
    assert portfolio.view().positions == ()


# --- P10, T4: corporate action timing ---------------------------------------------------------
def test_dividend_and_split_timing() -> None:
    """Units bought on the ex-date are not entitled; a split applies at its effective time."""
    head = [InstrumentDefined(**_common(1, ct(25, 0)), instrument=INSTRUMENTS[0])]
    dividend = CorporateActionV2(
        **_common(40, ct(26, 10)),
        instrument_id=AAPL,
        kind=CorporateActionKind.CASH_DIVIDEND,
        effective_ns=ct(27, 0),
        cash_amount=Decimal("1.00"),
        currency="USD",
        ex_date_ns=ct(27, 0),
        record_date_ns=ct(27, 0),
        pay_date_ns=ct(28, 0),
    )
    split = CorporateActionV2(
        **_common(41, ct(26, 11)),
        instrument_id=AAPL,
        kind=CorporateActionKind.SPLIT,
        effective_ns=ct(29, 0),
        ratio=Decimal(2),
    )
    events = [
        *head,
        _fill(30, AAPL, Side.BUY, "100", "10.00", ct(26, 9)),
        dividend,
        split,
        _fill(31, AAPL, Side.BUY, "50", "10.00", ct(27, 9)),  # on the ex-date: not entitled
        _bar(32, AAPL, "10.00", ct(28, 9)),
        _bar(33, AAPL, "5.00", ct(29, 9)),
    ]
    portfolio, _ = _fold(events)
    receivable = [p.amount for p in portfolio.postings if p.kind is PostingKind.RECEIVABLE]
    assert receivable == [Decimal("100.00"), Decimal("-100.00")]
    lots = portfolio.lots(AAPL)
    assert lots is not None
    assert (lots.position, lots.cost) == (Decimal(300), Decimal(1500))
    late = msgspec.structs.replace(split, id=_uuid(42), effective_ns=ct(26, 10))
    with pytest.raises(CorporateActionLateError):
        _fold([*head, late])


def test_records_out_of_sequence_are_refused() -> None:
    """The fold applies each sequence once, in order."""
    portfolio = Portfolio(_config(), MarketView([]))
    event = CashTransfer(**_common(1, ct(25, 0)), currency="USD", amount=Decimal(1), reference="t")
    with pytest.raises(PortfolioError, match="expected 0"):
        portfolio.apply(LogRecord(sequence=1, event=event), GENESIS)


def test_trading_day_end_follows_the_venue_time_zone() -> None:
    """16:00 in Chicago is 21:00 UTC under daylight time and 22:00 UTC after it ends."""
    entry = _config().futures[str(ES)]
    assert entry.end_ns(datetime.date(2026, 10, 30)) == ct(30, 16)
    utc = datetime.datetime.fromtimestamp(
        entry.end_ns(datetime.date(2026, 11, 2)) // 10**9, datetime.UTC
    )
    assert (utc.hour, utc.minute) == (22, 0)
    with pytest.raises(ValueError, match="unknown time zone"):
        _config(futures={str(ES): {"time_zone": "Mars/Base", "trading_day_end": "16:00", **READ}})


def test_symbol_change_moves_the_lots() -> None:
    """At its effective time the lots move to the new instrument, which must be defined."""
    renamed = InstrumentId("XNAS", "AAPL2")
    second = msgspec.structs.replace(INSTRUMENTS[0], id=renamed)
    change = CorporateActionV2(
        **_common(40, ct(26, 10)),
        instrument_id=AAPL,
        kind=CorporateActionKind.SYMBOL_CHANGE,
        effective_ns=ct(27, 0),
        new_instrument_id=renamed,
    )
    events = [
        InstrumentDefined(**_common(1, ct(25, 0)), instrument=INSTRUMENTS[0]),
        InstrumentDefined(**_common(2, ct(25, 0)), instrument=second),
        _fill(30, AAPL, Side.BUY, "10", "10.00", ct(26, 9)),
        change,
        _bar(31, renamed, "11.00", ct(27, 9)),
    ]
    portfolio, _ = _fold(events)
    (row,) = portfolio.view().positions
    assert (row.instrument_id, row.quantity, row.unrealized) == (renamed, Decimal(10), Decimal(10))
    assert portfolio.lots(AAPL) is None


def test_a_non_positive_close_leaves_the_position_unmarked() -> None:
    """Amendment 10 (F-Y): the view's mark is ``latest_close``; a zero close is no price."""
    events = [
        InstrumentDefined(**_common(1, ct(25, 0)), instrument=INSTRUMENTS[0]),
        CashTransfer(**_common(9, ct(25, 1)), currency="USD", amount=Decimal(10000), reference="t"),
        _fill(30, AAPL, Side.BUY, "10", "10.00", ct(26, 9)),
        _bar(31, AAPL, "11.00", ct(26, 10)),
    ]
    marked = _fold(events)[0].view()
    assert (marked.positions[0].mark, marked.unmarked) == (Decimal("11.00"), ())
    view = _fold([*events, _bar(32, AAPL, "0", ct(26, 11))])[0].view()
    assert (view.positions[0].mark, view.unmarked, view.equity_base) == (None, (AAPL,), None)


def test_unmarked_position_and_written_option() -> None:
    """An unmarked equity: listed, its currency's margin incomplete, no base total (a72)."""
    head = [
        InstrumentDefined(**_common(i + 1, ct(25, 0)), instrument=INSTRUMENTS[i]) for i in (0, 1)
    ]
    events = [
        *head,
        CashTransfer(**_common(9, ct(25, 1)), currency="USD", amount=Decimal(10000), reference="t"),
        _fill(30, AAPL, Side.BUY, "10", "10.00", ct(26, 9)),
        _fill(31, ES, Side.BUY, "1", "4000.00", ct(26, 10)),
        _settlement(32, 26, "4001.00"),
    ]
    view = _fold(events)[0].view()
    assert view.unmarked == (AAPL,)
    assert (view.equity_base, view.unconverted) == (None, ())
    assert view.unmargined == (ES,)  # USD equity unavailable: margin incomplete
    assert view.margin == ()
    option = Option(
        id=InstrumentId("OCC", "AAPL-C200"),
        quote_currency="USD",
        price_increment=CENT,
        quantity_increment=Decimal(1),
        multiplier=Decimal(100),
        underlying="AAPL",
        right=OptionRight.CALL,
        strike=Decimal(200),
        expiry_ns=ct(31, 15),
        exercise_style=ExerciseStyle.AMERICAN,
        settlement=Settlement.PHYSICAL,
        **_identifiers(),
    )
    written = [
        InstrumentDefined(**_common(5, ct(25, 0)), instrument=option),
        CashTransfer(**_common(9, ct(25, 1)), currency="USD", amount=Decimal(10000), reference="t"),
        _fill(40, option.id, Side.SELL, "1", "2.50", ct(26, 9)),
        _bar(41, option.id, "2.00", ct(26, 10)),
    ]
    view = _fold(written)[0].view()
    (row,) = view.positions
    assert (row.quantity, row.unrealized) == (Decimal(-1), Decimal(50))  # 250 received, 200 owed
    assert view.unmargined == (option.id,)  # no margin model for written options
    assert view.equity_base == Decimal(10000) + Decimal(250) - Decimal(200)


# --- T19: the projection (S9b; amendment 8) ---------------------------------------------------
TIMEOUT_MS = 5_000


def _append(path: Path, events: list[Event]) -> None:
    with EventLog(path, Environment.TEST, busy_timeout_ms=TIMEOUT_MS) as log:
        log.append(events)


CODE = "c" * 64


def _catch_up(
    log_path: Path, store_path: Path, config: PortfolioConfig | None = None, code: str = CODE
) -> tuple[Portfolio, tuple[Event, ...], bool]:
    with EventLogReader(log_path, Environment.TEST, busy_timeout_ms=TIMEOUT_MS) as reader:
        market = MarketView.from_reader(reader)
        connection = open_store(store_path, MIGRATIONS, busy_timeout_ms=TIMEOUT_MS)
        try:
            return catch_up(connection, reader, config or _config(), market, code_id=code)
        finally:
            connection.close()


def _rebuild(log_path: Path, tmp_path: Path) -> PortfolioState:
    portfolio, _, rebuilt = _catch_up(log_path, tmp_path / "fresh.sqlite3")
    assert rebuilt
    return portfolio.state()


@pytest.mark.parametrize("cut", range(1, len(_scenario())))
def test_incremental_fold_equals_a_rebuild(tmp_path: Path, cut: int) -> None:
    """Resume at every position of the scenario: the state equals a rebuild from 0 (T19)."""
    events = _scenario()
    log_path, store_path = tmp_path / "log.sqlite3", tmp_path / "state.sqlite3"
    _append(log_path, events[:cut])
    first, derived, rebuilt = _catch_up(log_path, store_path)
    assert rebuilt
    _append(log_path, [*derived, *events[cut:]])
    applied: list[int] = []
    original = Portfolio.apply

    def counting(self: Portfolio, record: LogRecord[Event], digest: bytes) -> tuple[Event, ...]:
        applied.append(record.sequence)
        return original(self, record, digest)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Portfolio, "apply", counting)
        _, more, rebuilt = _catch_up(log_path, store_path)
    assert not rebuilt
    assert applied[0] == first.log_position.next_sequence  # nothing before it re-applied
    _append(log_path, list(more))
    final, _, _ = _catch_up(log_path, store_path)
    assert final.state() == _rebuild(log_path, tmp_path)


def _tamper(store_path: Path, sql: str, *, rehash: bool) -> None:
    connection = open_store(store_path, MIGRATIONS, busy_timeout_ms=TIMEOUT_MS)
    try:
        connection.execute(sql)
        if rehash:
            connection.execute(
                "UPDATE portfolio_meta SET value = ? WHERE key = 'content_sha256'",
                (content_sha256(connection),),
            )
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("sql", "rehash"),
    [
        ("UPDATE portfolio_postings SET amount = '1' WHERE position = 0", False),
        ("UPDATE portfolio_postings SET amount = '0' || amount WHERE position = 0", True),
        ("UPDATE portfolio_balances SET cash = '1.00' WHERE currency = 'USD'", True),
        ("UPDATE portfolio_meta SET value = '0' WHERE key = 'projection_schema'", True),
        ("UPDATE portfolio_meta SET value = '1' WHERE key = 'next_sequence'", True),
        ("UPDATE portfolio_meta SET value = '999' WHERE key = 'next_sequence'", True),
    ],
    ids=["contents", "non-canonical", "balance-sum", "schema", "off-chain", "ahead"],
)
def test_invalid_projection_is_rebuilt(tmp_path: Path, sql: str, rehash: bool) -> None:
    """Corrupted, inconsistent, of another schema, off the chain or ahead: rebuilt from 0."""
    log_path, store_path = tmp_path / "log.sqlite3", tmp_path / "state.sqlite3"
    _append(log_path, _scenario())
    expected, derived, _ = _catch_up(log_path, store_path)
    _tamper(store_path, sql, rehash=rehash)
    rebuilt_portfolio, _, rebuilt = _catch_up(log_path, store_path)
    assert rebuilt
    assert rebuilt_portfolio.state() == expected.state()
    assert derived  # the scenario derives events


def test_projection_of_another_configuration_or_code_is_rebuilt(tmp_path: Path) -> None:
    """The configuration identity and the code identifier are part of validity (a76)."""
    log_path, store_path = tmp_path / "log.sqlite3", tmp_path / "state.sqlite3"
    _append(log_path, _scenario())
    _catch_up(log_path, store_path)
    assert not _catch_up(log_path, store_path)[2]  # same configuration and code: resumed
    connection = open_store(store_path, MIGRATIONS, busy_timeout_ms=TIMEOUT_MS)
    try:
        stamped = dict(connection.execute("SELECT key, value FROM portfolio_meta").fetchall())
    finally:
        connection.close()
    assert (stamped["config_id"], stamped["code_id"]) == (config_id(_config()), CODE)
    assert _catch_up(log_path, store_path, _config(account_id="other"))[2]
    assert _catch_up(log_path, store_path, code="d" * 64)[2]


def test_later_version_known_earlier_does_not_change_folded_state(tmp_path: Path) -> None:
    """A version appended after the projection, known earlier: folded state unchanged (F-T)."""
    log_path, store_path = tmp_path / "log.sqlite3", tmp_path / "state.sqlite3"
    v1 = INSTRUMENTS[0]
    head = [
        InstrumentDefined(**_common(1, ct(25, 0)), instrument=v1),
        CashTransfer(**_common(9, ct(25, 1)), currency="USD", amount=Decimal(1000), reference="t"),
        _fill(30, AAPL, Side.BUY, "10", "10.00", ct(26, 9)),
    ]
    _append(log_path, head)
    folded, _, _ = _catch_up(log_path, store_path)
    v2 = msgspec.structs.replace(v1, multiplier=Decimal(2), knowledge_ns=ct(26, 0))
    _append(
        log_path,
        [
            InstrumentDefined(**_common(2, ct(27, 0)), instrument=v2),
            _fill(31, AAPL, Side.BUY, "10", "10.00", ct(27, 9)),
        ],
    )
    with EventLogReader(log_path, Environment.TEST, busy_timeout_ms=TIMEOUT_MS) as reader:
        full = ReferenceIndex.from_records(reader.records())
    assert full.as_of(AAPL, ct(26, 9), ct(26, 9)) == v2  # a full-log index would see v2
    resumed, _, rebuilt = _catch_up(log_path, store_path)
    assert not rebuilt
    assert resumed.state().postings[: len(folded.postings)] == folded.postings
    notional = [p.amount for p in resumed.postings if p.kind is PostingKind.TRADE]
    assert notional == [Decimal("-100.00"), Decimal("-200.00")]  # v1 then v2
    assert resumed.state() == _rebuild(log_path, tmp_path)
