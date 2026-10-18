# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Tests for canonical events and codecs (BP1 X1, X2, X3, X5, X11; ADR 0014)."""

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
from auqua.contracts.errors import EventDecodeError, InvalidDecimalError, InvalidFieldError
from auqua.contracts.events import (
    Bar,
    CanonicalEvent,
    CorporateAction,
    CouponPayment,
    FundingPayment,
    FuturesExpiry,
    FxSettlement,
    OptionAssignment,
    OptionExercise,
    OptionExpiry,
    Quote,
    Trade,
)
from auqua.contracts.events.base import Event, LogRecord
from auqua.contracts.events.lifecycle import CorporateActionKind
from auqua.contracts.events.market import Aggressor
from auqua.contracts.identity import InstrumentId, new_id
from auqua.contracts.instruments import Settlement

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
        CorporateAction(
            **_common(new_id()),
            instrument_id=InstrumentId("XNAS", "AAPL"),
            kind=CorporateActionKind.SPLIT,
            effective_ns=T0 + HOUR_NS,
            ratio=Decimal(4),
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
        FundingPayment(
            **_common(new_id()),
            instrument_id=InstrumentId("BINANCE", "BTCUSDT-PERP"),
            funding_rate=Decimal("-0.0001"),
            mark_price=Decimal("93800.1"),
        ),
        CouponPayment(
            **_common(new_id()),
            instrument_id=InstrumentId("UST", "T-4.25-2035"),
            payment_ns=T0 + HOUR_NS,
            amount_per_unit=Decimal("2.125"),
            currency="USD",
        ),
    ]


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
