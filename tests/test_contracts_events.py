# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Tests for canonical events and codecs (BP1 X1, X2, X3, X5, X11; ADR 0014; BP6 section 4.1)."""

import datetime
import json
import typing
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import msgspec
import pytest
from hypothesis import given
from hypothesis import strategies as st

from auqua.contracts.codec import CANONICAL, EventCodec, encode
from auqua.contracts.errors import (
    EventDecodeError,
    InvalidDecimalError,
    InvalidFieldError,
    UpcastRefusedError,
)
from auqua.contracts.events import (
    Bar,
    CanonicalEvent,
    CorporateAction,
    CorporateActionV2,
    CouponPayment,
    FundingPayment,
    FundingPaymentV2,
    FuturesExpiry,
    FxSettlement,
    InstrumentDefined,
    OptionAssignment,
    OptionExercise,
    OptionExpiry,
    Quote,
    ReferenceRate,
    SettlementPrice,
    Trade,
)
from auqua.contracts.events.account import (
    AccountSnapshot,
    AccountStatement,
    CashTransfer,
    Difference,
    FxBreak,
    FxSettlementBreak,
    ReconciliationResult,
    ReconciliationStatus,
    StatementBalance,
    StatementPosition,
)
from auqua.contracts.events.base import Event, LogRecord
from auqua.contracts.events.ingest import (
    AvailabilityBasis,
    ChecksumResult,
    DataGap,
    GapReason,
    IngestRun,
    RunOutcome,
    UseClass,
)
from auqua.contracts.events.lifecycle import CorporateActionKind
from auqua.contracts.events.market import Aggressor
from auqua.contracts.events.research import PeriodName, RunStartedV2, Trial
from auqua.contracts.events.trading import (
    AllocationUnavailable,
    AllocationUnavailableReason,
    Fill,
    OrderSubmitted,
    OrderType,
    RiskDecision,
    RunStarted,
    Side,
    Signal,
    Target,
)
from auqua.contracts.identity import InstrumentId, SeriesId, new_id
from auqua.contracts.instruments import CryptoSpot, Identifiers, Settlement
from auqua.contracts.portfolio import AccountView, Balance, LogPosition, PositionView

SNAPSHOT = Path(__file__).parent / "snapshots" / "canonical_events.schema.json"
BTC = InstrumentId("BINANCE", "BTCUSDT")
T0 = 1_735_689_600_000_000_000
HOUR_NS = 3_600_000_000_000
COMMON: dict[str, Any] = {"source": "ingest/test", "ts_event_ns": T0, "ts_ingest_ns": T0 + 1}

PRICES = st.decimals(min_value=Decimal(-100000), max_value=Decimal(100000), places=4).map(
    lambda d: abs(d) if d.is_zero() else d
)
SIZES = st.decimals(min_value=Decimal("0.0001"), max_value=Decimal(1000000), places=4)
IDS = st.builds(uuid.UUID, st.uuids(version=4).map(str))


def _common(event_id: uuid.UUID) -> dict[str, Any]:
    return {**COMMON, "id": event_id}


TRADES = st.builds(
    lambda i, p, q, a: Trade(**_common(i), instrument_id=BTC, price=p, quantity=q, aggressor=a),
    IDS,
    PRICES,
    SIZES,
    st.sampled_from([None, *Aggressor]),
)


@st.composite
def bars(draw: st.DrawFn) -> Bar:
    """Bars whose prices satisfy low <= open, close <= high."""
    prices = sorted(draw(st.lists(PRICES, min_size=4, max_size=4)))
    low, open_, close, high = prices[0], prices[1], prices[2], prices[3]
    return Bar(
        id=draw(IDS),
        source="ingest/test",
        ts_event_ns=T0 + HOUR_NS,
        ts_ingest_ns=T0 + HOUR_NS,
        instrument_id=BTC,
        start_ns=T0,
        interval_ns=HOUR_NS,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=draw(SIZES),
    )


def _examples() -> list[Event]:
    """One valid instance of every canonical event type."""
    return [
        Trade(
            **_common(new_id()),
            instrument_id=BTC,
            price=Decimal("93800.10"),
            quantity=Decimal("0.001"),
        ),
        Quote(
            **_common(new_id()),
            instrument_id=BTC,
            bid_price=Decimal("93800.00"),
            bid_size=Decimal(2),
            ask_price=Decimal("93800.10"),
            ask_size=Decimal(1),
        ),
        Bar(
            id=new_id(),
            source="ingest/test",
            ts_event_ns=T0 + HOUR_NS,
            ts_ingest_ns=T0 + HOUR_NS,
            instrument_id=BTC,
            start_ns=T0,
            interval_ns=HOUR_NS,
            open=Decimal(10),
            high=Decimal(12),
            low=Decimal(9),
            close=Decimal(11),
            volume=Decimal(5),
        ),
        CorporateActionV2(
            **_common(new_id()),
            instrument_id=InstrumentId("XNAS", "AAPL"),
            kind=CorporateActionKind.SPLIT,
            effective_ns=T0 + HOUR_NS,
            ratio=Decimal(4),
        ),
        SettlementPrice(
            **_common(new_id()),
            instrument_id=InstrumentId("XCME", "ES-202612"),
            trading_date=datetime.date(2025, 1, 1),
            price=Decimal("6000.25"),
            ts_published_ns=T0 + HOUR_NS,
        ),
        FuturesExpiry(
            **_common(new_id()),
            instrument_id=InstrumentId("XCME", "ES-202612"),
            settlement=Settlement.CASH,
            settlement_price=Decimal("6000.25"),
        ),
        OptionExercise(
            **_common(new_id()),
            instrument_id=InstrumentId("CBOE", "SPX-C5000"),
            quantity=Decimal(1),
        ),
        OptionAssignment(
            **_common(new_id()),
            instrument_id=InstrumentId("CBOE", "SPX-C5000"),
            quantity=Decimal(1),
        ),
        OptionExpiry(
            **_common(new_id()),
            instrument_id=InstrumentId("CBOE", "SPX-C5000"),
            underlying_price=Decimal("5100.5"),
        ),
        FxSettlement(
            **_common(new_id()),
            instrument_id=InstrumentId("DUKASCOPY", "EURUSD"),
            value_date_ns=T0 + 2 * 24 * HOUR_NS,
            base_amount=Decimal(1000000),
            quote_amount=Decimal("1085000.00"),
        ),
        FundingPaymentV2(
            **_common(new_id()),
            instrument_id=InstrumentId("BINANCE", "BTCUSDT-PERP"),
            funding_rate=Decimal("-0.0001"),
            mark_price=Decimal("93800.1"),
            interval_ns=8 * HOUR_NS,
        ),
        CouponPayment(
            **_common(new_id()),
            instrument_id=InstrumentId("UST", "T-4.25-2035"),
            payment_ns=T0 + HOUR_NS,
            amount_per_unit=Decimal("2.125"),
            currency="USD",
        ),
        InstrumentDefined(**_common(new_id()), instrument=_crypto_spot(knowledge_ns=T0)),
        *_trading_examples(),
        *_ingestion_examples(),
        *_research_examples(),
        *_account_examples(),
    ]


def _account_examples() -> list[Event]:
    """One instance of each BP6 account event type."""
    return [
        CashTransfer(**_common(new_id()), currency="USD", amount=Decimal(100000), reference="t-1"),
        AccountStatement(
            **_common(new_id()),
            statement_id="s-1",
            as_of_ns=T0,
            balances=(StatementBalance("BTC", Decimal("0.5")), StatementBalance("USD", Decimal(1))),
            positions=(StatementPosition(BTC, Decimal("0.5")),),
        ),
        ReconciliationResult(
            **_common(new_id()),
            statement_id="s-1",
            statement_sha256="0" * 64,
            status=ReconciliationStatus.BREAK,
            differences=(Difference("balance:USD", Decimal(1), Decimal(2)),),
        ),
        FxSettlementBreak(
            **_common(new_id()),
            settlement_id=new_id(),
            reason=FxBreak.AMOUNTS,
            fill_id=new_id(),
            differences=(Difference("quote_amount", Decimal(1085000), Decimal(1084999)),),
        ),
        AccountSnapshot(
            **_common(new_id()),
            view=AccountView(
                account_id="research",
                as_of_ns=T0,
                log_position=LogPosition(next_sequence=3, hash="0" * 64),
                equity_base=Decimal("100000.00"),
                balances=(Balance("USDT", Decimal("100000.00")),),
                positions=(PositionView(BTC, "USDT", Decimal(0)),),
            ),
        ),
    ]


def _research_examples() -> list[Event]:
    """One instance of each BP5 event type (beyond the run record)."""
    return [
        Trial(
            **_common(new_id()),
            hypothesis_id="ma-crossover-btc",
            run_id=new_id(),
            config_hash="0" * 64,
            period=PeriodName.VALIDATION,
            observations=100,
            sharpe=Decimal("0.0123"),
            returns_sha256="0" * 64,
        ),
    ]


def test_run_started_v1_upcasts_to_v2() -> None:
    """A walking-skeleton run record decodes as ``research.run.v2`` (BP5 K11)."""
    digest = "0" * 64
    legacy = RunStarted(
        **_common(new_id()),
        input_head_hash=digest,
        input_next_sequence=0,
        instrument_id=BTC,
        instrument_sha256=digest,
        config_id=digest,
        spec_id=digest,
        risk_limits_sha256=digest,
        code_id=digest,
        code_version="0.1.0",
    )
    decoded = CANONICAL.decode(encode(legacy))
    assert isinstance(decoded, RunStartedV2)
    assert (decoded.id, decoded.code_id, decoded.hypothesis_id, decoded.inputs) == (
        legacy.id,
        digest,
        None,
        (),
    )


def _ingestion_examples() -> list[Event]:
    """One instance of each BP4 event type."""
    digest = "0" * 64
    return [
        ReferenceRate(
            **_common(new_id()),
            series_id=SeriesId("FRB", "H10/RXI$US_N.B.EU"),
            value=Decimal("1.1211"),
            unit="USD per EUR",
            ts_published_ns=T0 + HOUR_NS,
        ),
        IngestRun(
            **_common(new_id()),
            profile="frb/h10/daily",
            terms="public-domain",
            use_class=UseClass.UNRESTRICTED,
            raw_sha256=digest,
            raw_size=10,
            retrieval="https://www.federalreserve.gov/datadownload/",
            checksum=ChecksumResult.NOT_PUBLISHED,
            parser="frb-h10-ddp-csv",
            parser_version="1",
            parser_id=digest,
            outcome=RunOutcome.COMPLETED,
            availability=AvailabilityBasis.PUBLISHED_SCHEDULE,
            appended=1,
        ),
        DataGap(
            **{**_common(new_id()), "ts_event_ns": T0},
            key=str(BTC),
            start_ns=T0,
            end_ns=T0 + HOUR_NS,
            reason=GapReason.NO_DATA_PUBLISHED,
            interval_ns=HOUR_NS,
        ),
    ]


def _trading_examples() -> list[Event]:
    """One instance of each walking-skeleton event type (ADR 0025)."""
    cause = new_id()
    digest = "0" * 64
    return [
        RunStartedV2(
            **_common(new_id()),
            input_head_hash=digest,
            input_next_sequence=0,
            instrument_id=BTC,
            instrument_sha256=digest,
            config_id=digest,
            spec_id=digest,
            risk_limits_sha256=digest,
            code_id=digest,
            code_version="0.1.0",
        ),
        Signal(
            **_common(new_id()),
            causation_id=cause,
            instrument_id=BTC,
            direction=1,
            provider="baseline/ma-crossover",
            provider_version="1",
        ),
        Target(**_common(new_id()), causation_id=cause, instrument_id=BTC, quantity=Decimal(1)),
        AllocationUnavailable(
            **_common(new_id()),
            causation_id=cause,
            instrument_id=BTC,
            reason=AllocationUnavailableReason.VOLATILITY_WARMUP,
        ),
        OrderSubmitted(
            **_common(new_id()),
            causation_id=cause,
            instrument_id=BTC,
            side=Side.BUY,
            quantity=Decimal(1),
            order_type=OrderType.MARKET,
        ),
        RiskDecision(
            **_common(new_id()),
            causation_id=cause,
            instrument_id=BTC,
            accepted=False,
            rule="max_order_notional",
            limit=Decimal(100),
            value=Decimal(150),
        ),
        Fill(
            **_common(new_id()),
            causation_id=cause,
            instrument_id=BTC,
            side=Side.SELL,
            price=Decimal("93800.10"),
            quantity=Decimal(1),
            fee=Decimal("9.38"),
            fee_currency="USDT",
        ),
    ]


def test_trading_event_rules() -> None:
    """Directions, rejections and hashes are validated (ADR 0025)."""
    with pytest.raises(InvalidFieldError, match="direction"):
        Signal(
            **_common(new_id()),
            causation_id=new_id(),
            instrument_id=BTC,
            direction=2,
            provider="p",
            provider_version="1",
        )
    with pytest.raises(InvalidFieldError, match="rejection"):
        RiskDecision(
            **_common(new_id()),
            causation_id=new_id(),
            instrument_id=BTC,
            accepted=False,
            rule="max_position",
        )
    with pytest.raises(InvalidFieldError, match="hex"):
        msgspec.structs.replace(_trading_examples()[0], config_id="X" * 64)


def _crypto_spot(knowledge_ns: int) -> CryptoSpot:
    return CryptoSpot(
        id=BTC,
        quote_currency="USDT",
        price_increment=Decimal("0.01"),
        quantity_increment=Decimal("0.00001"),
        multiplier=Decimal(1),
        valid_from_ns=T0,
        knowledge_ns=knowledge_ns,
        identifiers=Identifiers(),
        base_currency="BTC",
    )


def test_instrument_defined_refuses_later_knowledge() -> None:
    """A version cannot be known after the platform observed it (BP3 Z13)."""
    with pytest.raises(InvalidFieldError, match="knowledge_ns"):
        InstrumentDefined(**_common(new_id()), instrument=_crypto_spot(knowledge_ns=T0 + 2))


def test_every_canonical_type_has_an_example() -> None:
    """The examples cover the whole union (guards new types without tests)."""
    assert {type(e) for e in _examples()} == set(typing.get_args(CanonicalEvent))


@pytest.mark.parametrize("event", _examples(), ids=lambda e: type(e).__name__)
def test_round_trip_every_type(event: Event) -> None:
    """decode(encode(e)) == e and the encoding is byte-stable."""
    data = encode(event)
    decoded = CANONICAL.decode(data)
    assert decoded == event
    assert encode(decoded) == data


@given(st.one_of(TRADES, bars()))
def test_round_trip_generated(event: Event) -> None:
    """Generated trades and bars round-trip exactly."""
    data = encode(event)
    assert CANONICAL.decode(data) == event
    assert encode(CANONICAL.decode(data)) == data


def test_decimals_are_encoded_as_strings() -> None:
    """No decimal travels as a JSON number."""
    payload = json.loads(encode(_examples()[0]))
    assert payload["price"] == "93800.10"
    assert payload["quantity"] == "0.001"


def test_log_record_round_trip() -> None:
    """A log record carries the sequence; the event is unchanged."""
    event = _examples()[0]
    record = LogRecord(sequence=42, event=event)
    decoded = CANONICAL.decode_record(encode(record))
    assert decoded == record
    assert decoded.event == event
    with pytest.raises(InvalidFieldError):
        LogRecord(sequence=-1, event=event)


def _payload(**changes: Any) -> bytes:  # noqa: ANN401
    payload = json.loads(encode(_examples()[0]))
    payload.update(changes)
    return json.dumps({k: v for k, v in payload.items() if v is not ...}).encode()


@pytest.mark.parametrize(
    "data",
    [
        _payload(extra=1),
        _payload(type="market.trade.v9"),
        _payload(type="market.unknown.v1"),
        _payload(price="NaN"),
        _payload(price="Infinity"),
        _payload(price="-0"),
        _payload(quantity="0"),
        _payload(source="Bad Source"),
        _payload(ts_event_ns=-1),
        _payload(price=...),
    ],
    ids=[
        "unknown-field",
        "unknown-version",
        "unknown-type",
        "nan",
        "inf",
        "neg-zero",
        "zero-qty",
        "bad-source",
        "pre-epoch",
        "missing-field",
    ],
)
def test_invalid_events_refused(data: bytes) -> None:
    """Unknown fields, tags and versions, and contract violations are refused."""
    with pytest.raises(EventDecodeError):
        CANONICAL.decode(data)


def test_constructor_validates() -> None:
    """Direct construction applies the same contract checks."""
    with pytest.raises(InvalidDecimalError):
        Trade(**_common(new_id()), instrument_id=BTC, price=Decimal("NaN"), quantity=Decimal(1))
    with pytest.raises(InvalidFieldError):
        Quote(
            **_common(new_id()),
            instrument_id=BTC,
            bid_price=Decimal(2),
            bid_size=Decimal(1),
            ask_price=Decimal(1),
            ask_size=Decimal(1),
        )
    with pytest.raises(InvalidFieldError):
        CorporateAction(
            **_common(new_id()),
            instrument_id=BTC,
            kind=CorporateActionKind.CASH_DIVIDEND,
            effective_ns=T0,
            ratio=Decimal(2),
        )


# --- BP6 section 4.1: new versions and account events ------------------------------------------
AAPL = InstrumentId("XNAS", "AAPL")
DAY_NS = 24 * HOUR_NS


def _dividend(**changes: Any) -> CorporateActionV2:  # noqa: ANN401 - test overrides
    fields: dict[str, Any] = {
        "instrument_id": AAPL,
        "kind": CorporateActionKind.CASH_DIVIDEND,
        "effective_ns": T0 + DAY_NS,
        "cash_amount": Decimal("0.26"),
        "currency": "USD",
        "ex_date_ns": T0 + DAY_NS,
        "record_date_ns": T0 + DAY_NS,
        "pay_date_ns": T0 + 15 * DAY_NS,
    } | changes
    return CorporateActionV2(**_common(new_id()), **fields)


def test_cash_dividend_needs_its_three_dates() -> None:
    """v2 cash dividends carry ex, record and pay dates; other kinds may not (BP6 C1)."""
    assert _dividend().pay_date_ns == T0 + 15 * DAY_NS
    assert _dividend(pay_date_ns=T0).pay_date_ns == T0, "FINRA 11140(b)(2): pay may precede ex"
    with pytest.raises(InvalidFieldError, match="ex, record and pay"):
        _dividend(record_date_ns=None)
    with pytest.raises(InvalidFieldError, match="effective_ns must equal ex_date_ns"):
        _dividend(effective_ns=T0)
    with pytest.raises(InvalidFieldError, match="forbids dividend dates"):
        CorporateActionV2(
            **_common(new_id()),
            instrument_id=AAPL,
            kind=CorporateActionKind.SPLIT,
            effective_ns=T0,
            ratio=Decimal(2),
            ex_date_ns=T0,
        )


def test_corporate_action_v1_upcasts_except_cash_dividends() -> None:
    """v1 non-dividend kinds upcast unchanged; a v1 cash dividend is refused with its sequence."""
    split = CorporateAction(
        **_common(new_id()),
        instrument_id=AAPL,
        kind=CorporateActionKind.SPLIT,
        effective_ns=T0,
        ratio=Decimal(4),
    )
    upcast = CANONICAL.decode(encode(split))
    assert isinstance(upcast, CorporateActionV2)
    assert (upcast.id, upcast.ratio, upcast.ex_date_ns) == (split.id, Decimal(4), None)
    dividend = CorporateAction(
        **_common(new_id()),
        instrument_id=AAPL,
        kind=CorporateActionKind.CASH_DIVIDEND,
        effective_ns=T0,
        cash_amount=Decimal("0.26"),
        currency="USD",
    )
    with pytest.raises(UpcastRefusedError, match="no ex, record and pay dates"):
        CANONICAL.decode(encode(dividend))
    with pytest.raises(UpcastRefusedError, match="sequence 9: "):
        CANONICAL.decode_record(encode(LogRecord(sequence=9, event=dividend)))


def test_funding_payment_v1_is_refused_and_v2_validated() -> None:
    """v1 has no period, so it is refused; v2 needs a positive period (BP6 D3, E2)."""
    legacy = FundingPayment(
        **_common(new_id()),
        instrument_id=BTC,
        funding_rate=Decimal("0.0005"),
        mark_price=Decimal(10000),
    )
    with pytest.raises(UpcastRefusedError, match="no funding period"):
        CANONICAL.decode(encode(legacy))
    with pytest.raises(InvalidFieldError, match="interval_ns"):
        FundingPaymentV2(
            **_common(new_id()),
            instrument_id=BTC,
            funding_rate=Decimal("0.0005"),
            mark_price=Decimal(10000),
            interval_ns=0,
        )
    reported = FundingPaymentV2(
        **_common(new_id()),
        instrument_id=BTC,
        funding_rate=Decimal("0.0005"),
        mark_price=Decimal(10000),
        interval_ns=8 * HOUR_NS,
        amount=Decimal("-0.0005"),
    )
    assert CANONICAL.decode(encode(reported)) == reported


def test_account_event_rules() -> None:
    """Transfers are non-zero; statement rows are canonical; results are consistent."""
    with pytest.raises(InvalidFieldError, match="zero"):
        CashTransfer(**_common(new_id()), currency="USD", amount=Decimal(0), reference="t")
    with pytest.raises(InvalidFieldError, match="trimmed"):
        CashTransfer(**_common(new_id()), currency="USD", amount=Decimal(1), reference=" t")
    usd, btc = StatementBalance("USD", Decimal(1)), StatementBalance("BTC", Decimal(1))
    with pytest.raises(InvalidFieldError, match="increasing"):
        AccountStatement(**_common(new_id()), statement_id="s", as_of_ns=T0, balances=(usd, btc))
    with pytest.raises(InvalidFieldError, match="increasing"):
        AccountStatement(**_common(new_id()), statement_id="s", as_of_ns=T0, balances=(usd, usd))
    key: dict[str, Any] = {"statement_id": "s", "statement_sha256": "0" * 64}
    with pytest.raises(InvalidFieldError, match="lists"):
        ReconciliationResult(**_common(new_id()), **key, status=ReconciliationStatus.BREAK)
    differs = (Difference("balance:USD", Decimal(1), None),)
    settled = new_id()
    with pytest.raises(InvalidFieldError, match="exactly for an amounts break"):
        FxSettlementBreak(
            **_common(new_id()), settlement_id=settled, reason=FxBreak.AMOUNTS, fill_id=new_id()
        )
    with pytest.raises(InvalidFieldError, match="exactly for an amounts break"):
        FxSettlementBreak(
            **_common(new_id()),
            settlement_id=settled,
            reason=FxBreak.UNMATCHED,
            differences=differs,
        )
    with pytest.raises(InvalidFieldError, match="fill_id"):
        FxSettlementBreak(
            **_common(new_id()), settlement_id=settled, reason=FxBreak.AMBIGUOUS, fill_id=new_id()
        )
    with pytest.raises(InvalidFieldError, match="fill_id"):
        FxSettlementBreak(
            **_common(new_id()), settlement_id=settled, reason=FxBreak.AMOUNTS, differences=differs
        )
    with pytest.raises(InvalidFieldError, match="lists"):
        ReconciliationResult(
            **_common(new_id()), **key, status=ReconciliationStatus.MATCHED, differences=differs
        )
    with pytest.raises(InvalidFieldError, match="only a matched"):
        ReconciliationResult(
            **_common(new_id()),
            **key,
            status=ReconciliationStatus.BREAK,
            differences=differs,
            resolves=new_id(),
        )
    with pytest.raises(InvalidFieldError, match="do not differ"):
        Difference("balance:USD", Decimal(1), Decimal("1.0"))
    with pytest.raises(InvalidFieldError, match="hex"):
        ReconciliationResult(
            **_common(new_id()),
            statement_id="s",
            statement_sha256="X",
            status=ReconciliationStatus.MATCHED,
        )


def test_settlement_price_is_published_after_its_event() -> None:
    """A settlement price cannot be available before it happened (ADR 0028)."""
    with pytest.raises(InvalidFieldError, match="ts_published_ns"):
        SettlementPrice(
            **_common(new_id()),
            instrument_id=BTC,
            trading_date=datetime.date(2025, 1, 1),
            price=Decimal(1),
            ts_published_ns=T0 - 1,
        )


def test_events_are_immutable() -> None:
    """Frozen events cannot be modified."""
    event = _examples()[0]
    with pytest.raises(AttributeError):
        event.source = "other"  # type: ignore[misc]


def test_no_float_fields_and_unique_versioned_tags() -> None:
    """No canonical field is a float; every tag is unique and versioned."""
    tags = []
    for event_type in typing.get_args(CanonicalEvent):
        info = msgspec.inspect.type_info(event_type)
        assert isinstance(info, msgspec.inspect.StructType)
        for field in info.fields:
            assert not isinstance(field.type, msgspec.inspect.FloatType), field.name
        tags.append(info.tag)
    assert len(tags) == len(set(tags))
    assert all(isinstance(t, str) and t.rsplit(".", 1)[-1].startswith("v") for t in tags)


def test_json_schema_snapshot() -> None:
    """The exported JSON Schema matches the committed snapshot (schema changes are deliberate)."""
    schema = json.dumps(CANONICAL.json_schema(), indent=2, sort_keys=True) + "\n"
    assert schema == SNAPSHOT.read_text(encoding="utf-8")


class _PingV1(Event, frozen=True, tag="test.ping.v1"):
    """Legacy version: ``note``."""

    note: str


class _PingV2(Event, frozen=True, tag="test.ping.v2"):
    """Current version: ``message`` replaced ``note``."""

    message: str


def _ping_v1_to_v2(old: _PingV1) -> _PingV2:
    return _PingV2(
        id=old.id,
        source=old.source,
        ts_event_ns=old.ts_event_ns,
        ts_ingest_ns=old.ts_ingest_ns,
        message=old.note,
    )


def test_upcaster_converts_a_stored_legacy_version() -> None:
    """A stored v1 event decodes and upcasts to v2; v2 decodes unchanged."""
    codec = EventCodec((_PingV2,), {_PingV1: _ping_v1_to_v2})
    old = _PingV1(**_common(new_id()), note="hello")
    upcast = codec.decode(encode(old))
    assert upcast == _ping_v1_to_v2(old)
    record = codec.decode_record(encode(LogRecord(sequence=7, event=old)))
    assert record == LogRecord(sequence=7, event=_ping_v1_to_v2(old))
    current = _PingV2(**_common(new_id()), message="hi")
    assert codec.decode(encode(current)) == current


def test_codec_configuration_errors() -> None:
    """A codec needs current types, and a type cannot be current and legacy."""
    with pytest.raises(ValueError, match="at least one"):
        EventCodec(())
    with pytest.raises(ValueError, match="both current and legacy"):
        EventCodec((_PingV2,), {_PingV2: lambda e: e})


# --- BP6 amendment 2, Rule W: optional fields within a version keep stored bytes ----------------
#: Payloads encoded by the code before amendment 2 (main 5985e6b), byte for byte.
XYZ_SHARES = InstrumentId("XNYS", "XYZ")
PRE_RULE_W = (
    b'{"type":"execution.fill.v1","id":"00000000-0000-0000-0000-000000000001",'
    b'"source":"ingest/test","ts_event_ns":1735689600000000000,'
    b'"ts_ingest_ns":1735689600000000001,"causation_id":"00000000-0000-0000-0000-000000000002",'
    b'"instrument_id":{"venue":"BINANCE","symbol":"BTCUSDT"},"side":"sell","price":"93800.10",'
    b'"quantity":"1","fee":"9.38","fee_currency":"USDT"}',
    b'{"type":"lifecycle.option_exercise.v1","id":"00000000-0000-0000-0000-000000000003",'
    b'"source":"ingest/test","ts_event_ns":1735689600000000000,'
    b'"ts_ingest_ns":1735689600000000001,"instrument_id":{"venue":"CBOE","symbol":"SPX-C5000"},'
    b'"quantity":"1"}',
    b'{"type":"lifecycle.option_assignment.v1","id":"00000000-0000-0000-0000-000000000004",'
    b'"source":"ingest/test","ts_event_ns":1735689600000000000,'
    b'"ts_ingest_ns":1735689600000000001,"instrument_id":{"venue":"CBOE","symbol":"SPX-C5000"},'
    b'"quantity":"1"}',
    b'{"type":"lifecycle.fx_settlement.v1","id":"00000000-0000-0000-0000-000000000005",'
    b'"source":"ingest/test","ts_event_ns":1735689600000000000,'
    b'"ts_ingest_ns":1735689600000000001,"instrument_id":{"venue":"DUKASCOPY","symbol":"EURUSD"},'
    b'"value_date_ns":1735862400000000000,"base_amount":"1000000","quote_amount":"1085000.00"}',
    b'{"type":"reference.instrument.v1","id":"00000000-0000-0000-0000-000000000007",'
    b'"source":"ingest/test","ts_event_ns":1735689600000000000,'
    b'"ts_ingest_ns":1735689600000000001,"instrument":{"type":"instrument.option.v1",'
    b'"id":{"venue":"OCC","symbol":"XYZ-C50"},"quote_currency":"USD","price_increment":"0.01",'
    b'"quantity_increment":"1","multiplier":"100","valid_from_ns":0,"knowledge_ns":0,'
    b'"identifiers":{"figi":null,"isin":null,"cfi":null,"dti":null},"underlying":"XYZ",'
    b'"right":"call","strike":"50","expiry_ns":1767225600000000000,"exercise_style":"american",'
    b'"settlement":"physical"}}',
)


@pytest.mark.parametrize(
    "payload", PRE_RULE_W, ids=["fill", "exercise", "assignment", "fx_settlement", "option"]
)
def test_rule_w_keeps_stored_payloads_byte_identical(payload: bytes) -> None:
    """A stored event without the new field decodes and re-encodes to the same bytes."""
    event = CANONICAL.decode(payload)
    assert encode(event) == payload


def test_rule_w_fields_round_trip_when_present() -> None:
    """Present, the new fields are encoded, decoded and validated (F-D, F-E2)."""
    fill = msgspec.structs.replace(
        CANONICAL.decode(PRE_RULE_W[0]), accrued_interest=Decimal("16.87500")
    )
    data = encode(fill)
    assert b'"accrued_interest":"16.87500"' in data
    assert CANONICAL.decode(data) == fill
    exercise = msgspec.structs.replace(
        CANONICAL.decode(PRE_RULE_W[1]), exercise_settlement_value=Decimal("5100.25")
    )
    assert CANONICAL.decode(encode(exercise)) == exercise
    settlement = msgspec.structs.replace(
        CANONICAL.decode(PRE_RULE_W[3]), causation_id=uuid.UUID(int=6)
    )
    assert b'"causation_id":"00000000-0000-0000-0000-000000000006"' in encode(settlement)
    assert CANONICAL.decode(encode(settlement)) == settlement
    defined = CANONICAL.decode(PRE_RULE_W[4])
    assert isinstance(defined, InstrumentDefined)
    option = msgspec.structs.replace(
        defined, instrument=msgspec.structs.replace(defined.instrument, underlying_id=XYZ_SHARES)
    )
    assert b'"underlying_id":{"venue":"XNYS","symbol":"XYZ"}' in encode(option)
    assert CANONICAL.decode(encode(option)) == option
    with pytest.raises(InvalidDecimalError):
        msgspec.structs.replace(fill, accrued_interest=Decimal("NaN"))
