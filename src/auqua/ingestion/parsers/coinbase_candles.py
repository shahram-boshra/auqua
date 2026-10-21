# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Coinbase Exchange candles and product specifications (BP4 section 4.4).

Candles: ``GET https://api.exchange.coinbase.com/products/{product_id}/candles``
(public, no authentication); each bucket is ``[time, low, high, open, close,
volume]`` with ``time`` the "bucket start time"; granularity one of 60, 300,
900, 3600, 21600 or 86400 seconds; at most 300 candles per request. "No data
is published for intervals where there are no ticks": missing buckets in the
requested window become :class:`DataGap` events with reason
``no_data_published`` and are never filled. Numbers are read from the JSON
text as exact decimals (``parse_float=Decimal``), never as binary floats.
Every bucket must lie on the window's grid; repeated buckets are refused. The
request asks for the window ``[start, end]``; a bucket outside ``[start, end)``
is not part of this window (the next window covers it) and is counted in the
run's message, never stored twice.

Product: ``GET /products/{product_id}`` gives ``base_currency``,
``quote_currency``, ``base_increment`` (quantity grid), ``quote_increment``
(price grid) and ``status``. Coinbase publishes only the current
specification, so the version is recorded as valid from the epoch and known at
retrieval; a historical price off the current grid is refused, which exposes a
changed grid instead of hiding it (BP4 section 10).

Data use is governed by the Coinbase Market Data Terms (profile
``coinbase/exchange/candles``: personal or research use; no machine learning;
no sharing of derived works; decision log a28).
"""

import datetime
import hashlib
import json
import sys
from decimal import Decimal, InvalidOperation
from typing import Any

from auqua.contracts.errors import ContractError
from auqua.contracts.events.ingest import AvailabilityBasis, GapReason
from auqua.contracts.events.market import Bar
from auqua.contracts.events.reference import InstrumentDefined
from auqua.contracts.identity import InstrumentId, derived_id
from auqua.contracts.instruments import CryptoSpot, Identifiers
from auqua.contracts.numeric import on_grid
from auqua.ingestion.errors import ParseError
from auqua.ingestion.parsers import (
    INSTRUMENT_NAMESPACE,
    ParseContext,
    ParseResult,
    ParserSpec,
    TimeUnit,
    bar_id,
)
from auqua.ingestion.quality import interval_gaps

API = "https://api.exchange.coinbase.com"
VENUE = "COINBASE"
GRANULARITIES_S = frozenset({60, 300, 900, 3600, 21600, 86400})
MAX_CANDLES = 300
#: Buckets per window: one fewer than the limit, so a server that treats ``end``
#: as inclusive still returns at most 300 candles.
MAX_WINDOW_BUCKETS = MAX_CANDLES - 1
_FIELDS = 6
_SECOND = int(TimeUnit.SECONDS)


def candles_url(product_id: str, granularity_s: int, start_s: int, end_s: int) -> str:
    """Request URL for the buckets starting in ``[start_s, end_s)`` (at most 299).

    ``start`` and ``end`` are sent as ISO 8601 UTC; :func:`parse` keeps only
    buckets inside the window.

    Raises:
        ValueError: unsupported granularity, empty or oversized window.
    """
    if granularity_s not in GRANULARITIES_S:
        raise ValueError(f"granularity {granularity_s}s not in {sorted(GRANULARITIES_S)}")
    count = (end_s - start_s) // granularity_s
    if end_s <= start_s or (end_s - start_s) % granularity_s or count > MAX_WINDOW_BUCKETS:
        raise ValueError(f"window must be 1 to {MAX_WINDOW_BUCKETS} whole buckets")
    return (
        f"{API}/products/{product_id}/candles?granularity={granularity_s}"
        f"&start={_iso(start_s)}&end={_iso(end_s)}"
    )


def _iso(seconds: int) -> str:
    moment = datetime.datetime.fromtimestamp(seconds, tz=datetime.UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def product_url(product_id: str) -> str:
    """Request URL of one product's specification."""
    return f"{API}/products/{product_id}"


def _json(data: bytes) -> Any:  # noqa: ANN401 - provider JSON before validation
    try:
        return json.loads(data, parse_float=Decimal, parse_int=Decimal)
    except (ValueError, UnicodeDecodeError) as error:
        raise ParseError(f"not JSON: {error}") from error


def _value(raw: object, step: Decimal, where: str) -> Decimal:
    try:
        value = Decimal(raw) if isinstance(raw, str) else raw
        if not isinstance(value, Decimal):
            raise ParseError(f"{where}: {raw!r} is not a number")
        return on_grid(value, step)
    except (InvalidOperation, ContractError) as error:
        raise ParseError(f"{where}: {error}") from error


def parse(data: bytes, context: ParseContext) -> ParseResult:
    """Bars and gaps of one candles response.

    Raises:
        ParseError: not a list of 6-field buckets, a bucket off the window's
            grid, a repeated bucket, a value off the instrument grid, or a
            context without instrument, interval and window.
    """
    instrument, interval_ns, window = context.instrument, context.interval_ns, context.window
    if instrument is None or interval_ns is None or window is None:
        raise ParseError("candles need the instrument, the interval and the window")
    payload = _json(data)
    if not isinstance(payload, list):
        raise ParseError("candles response is not a list")
    by_start: dict[int, Bar] = {}
    outside = 0
    for index, bucket in enumerate(payload):
        where = f"candle {index}"
        if not isinstance(bucket, list) or len(bucket) != _FIELDS:
            raise ParseError(f"{where}: expected {_FIELDS} fields")
        time_s = bucket[0]
        if not isinstance(time_s, Decimal) or time_s != time_s.to_integral_value():
            raise ParseError(f"{where}: time must be an integer")
        start_ns = int(time_s) * _SECOND
        if (start_ns - window[0]) % interval_ns:
            raise ParseError(f"{where}: bucket {start_ns} off the window's grid")
        if not window[0] <= start_ns < window[1]:
            outside += 1
            continue
        if start_ns in by_start:
            raise ParseError(f"{where}: bucket {start_ns} repeated")
        price, size = instrument.price_increment, instrument.quantity_increment
        low, high, open_, close = (_value(bucket[i], price, where) for i in (1, 2, 3, 4))
        try:
            by_start[start_ns] = Bar(
                id=bar_id(context.source, str(instrument.id), interval_ns, start_ns),
                source=context.source,
                ts_event_ns=start_ns + interval_ns,
                ts_ingest_ns=context.ingest_ns,
                instrument_id=instrument.id,
                start_ns=start_ns,
                interval_ns=interval_ns,
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=_value(bucket[5], size, where),
            )
        except ContractError as error:
            raise ParseError(f"{where}: {error}") from error
    starts = sorted(by_start)
    notes = [f"{outside} bucket(s) outside the window not used"] if outside else []
    return ParseResult(
        events=[by_start[start] for start in starts],
        gaps=list(
            interval_gaps(
                starts,
                first_ns=window[0],
                end_ns=window[1],
                interval_ns=interval_ns,
                source=context.source,
                key=str(instrument.id),
                reason=GapReason.NO_DATA_PUBLISHED,
                ingest_ns=context.ingest_ns,
            )
        ),
        availability=AvailabilityBasis.EVENT_END,
        notes=notes,
    )


def _text(product: dict[str, Any], name: str) -> str:
    value = product.get(name)
    if not isinstance(value, str) or not value:
        raise ParseError(f"product field {name!r} missing or not a string")
    return value


def parse_product(data: bytes, context: ParseContext) -> ParseResult:
    """One :class:`InstrumentDefined` from a product specification.

    Raises:
        ParseError: missing fields, invalid increments or currencies.
    """
    product = _json(data)
    if not isinstance(product, dict):
        raise ParseError("product response is not an object")
    try:
        instrument = CryptoSpot(
            id=InstrumentId(VENUE, _text(product, "id")),
            quote_currency=_text(product, "quote_currency"),
            price_increment=Decimal(_text(product, "quote_increment")).normalize(),
            quantity_increment=Decimal(_text(product, "base_increment")).normalize(),
            multiplier=Decimal(1),
            valid_from_ns=0,
            knowledge_ns=context.ingest_ns,
            identifiers=Identifiers(),
            base_currency=_text(product, "base_currency"),
        )
    except (ContractError, InvalidOperation) as error:
        raise ParseError(f"product {product.get('id')!r}: {error}") from error
    status = _text(product, "status")
    spec = hashlib.sha256(
        f"{instrument.id}|{instrument.price_increment}|{instrument.quantity_increment}|"
        f"{instrument.quote_currency}|{instrument.base_currency}|{status}".encode()
    ).digest()
    event = InstrumentDefined(
        id=derived_id(INSTRUMENT_NAMESPACE, context.source.encode(), spec),
        source=context.source,
        ts_event_ns=context.ingest_ns,
        ts_ingest_ns=context.ingest_ns,
        instrument=instrument,
    )
    return ParseResult(events=[event], availability=AvailabilityBasis.EVENT_END)


PARSER = ParserSpec("coinbase-candles", "1", sys.modules[__name__], parse)
PRODUCT_PARSER = ParserSpec("coinbase-product", "1", sys.modules[__name__], parse_product)
