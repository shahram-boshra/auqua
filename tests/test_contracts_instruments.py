# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Tests for the instrument and currency model (BP1 X9; ADR 0005, ADR 0017).

One fixture per asset class. Field values marked "spec" are taken from the
cited contract specification; all other values are synthetic and only exercise
the schema. Classes verified on real data (V1) get real reference data from
their sources in BP4.
"""

from decimal import Decimal
from typing import Any

import msgspec
import pytest

from auqua.contracts.currency import Currency, CurrencyKind
from auqua.contracts.errors import InvalidDecimalError, InvalidFieldError
from auqua.contracts.identity import InstrumentId
from auqua.contracts.instruments import (
    AnyInstrument,
    AssetClass,
    Bond,
    CryptoSpot,
    DayCount,
    Equity,
    ExerciseStyle,
    Future,
    FxDelivery,
    FxPair,
    Identifiers,
    Instrument,
    Option,
    OptionRight,
    PerpetualSwap,
    PerpetualType,
    Settlement,
)
from auqua.contracts.temporal import from_rfc3339

KNOWN = from_rfc3339("2026-10-18T00:00:00Z")
HOUR_NS = 3_600_000_000_000
DECODER = msgspec.json.Decoder(AnyInstrument)


def _common(instrument_id: str, currency: str, increments: tuple[str, str, str]) -> dict[str, Any]:
    """Common fields; ``increments`` is (price increment, quantity increment, multiplier)."""
    price, quantity, multiplier = increments
    return {
        "id": InstrumentId.parse(instrument_id),
        "quote_currency": currency,
        "price_increment": Decimal(price),
        "quantity_increment": Decimal(quantity),
        "multiplier": Decimal(multiplier),
        "valid_from_ns": KNOWN,
        "knowledge_ns": KNOWN,
        "identifiers": Identifiers(),
    }


# Equity. spec: price increment $0.01 for quotes >= $1.00 (SEC Regulation NMS Rule 612).
EQUITY = Equity(**_common("XNAS:AAPL", "USD", ("0.01", "1", "1")), is_fund=False)

# Future: E-mini S&P 500. spec (CME Rulebook ch. 358): $50 x index; tick 0.25 index points;
# cash settlement; trading ends at the NYSE open on the third Friday of the contract month.
FUTURE = Future(
    **_common("XCME:ES-202612", "USD", ("0.25", "1", "50")),
    underlying="S&P 500 Index",
    expiry_ns=from_rfc3339("2026-12-18T14:30:00Z"),
    last_trade_ns=from_rfc3339("2026-12-18T14:30:00Z"),
    settlement=Settlement.CASH,
)

# Option: SPX. spec: multiplier 100 (Cboe); European exercise, cash settlement (Alpaca index
# options documentation). Strike, expiry and tick are synthetic.
OPTION = Option(
    **_common("CBOE:SPX-202612-C5000", "USD", ("0.05", "1", "100")),
    underlying="S&P 500 Index",
    right=OptionRight.CALL,
    strike=Decimal(5000),
    expiry_ns=from_rfc3339("2026-12-18T21:00:00Z"),
    exercise_style=ExerciseStyle.EUROPEAN,
    settlement=Settlement.CASH,
)

# FX spot. spec: standard spot settlement T+2. Increments are synthetic.
FX = FxPair(
    **_common("DUKASCOPY:EURUSD", "USD", ("0.00001", "1", "1")),
    base_currency="EUR",
    delivery=FxDelivery.SPOT,
    settlement_lag_days=2,
)

# Crypto spot: all values synthetic (V1 class; real filters come from the exchange in BP4).
CRYPTO_SPOT = CryptoSpot(
    **_common("BINANCE:BTCUSDT", "USDT", ("0.01", "0.00001", "1")), base_currency="BTC"
)

# Perpetual swap: Binance BTCUSDT USD-M. spec: min price movement 0.10 USDT, min trade amount
# 0.001 BTC (Binance trading rules); linear, quoted and settled in USDT; default funding
# interval 8 hours (Binance funding-rate FAQ).
PERPETUAL = PerpetualSwap(
    **_common("BINANCE:BTCUSDT-PERP", "USDT", ("0.10", "0.001", "1")),
    base_currency="BTC",
    settlement_currency="USDT",
    contract_type=PerpetualType.LINEAR,
    funding_interval_ns=8 * HOUR_NS,
)

# Bond: US Treasury note. spec: interest paid every six months, $100 purchase increments
# (TreasuryDirect); actual/actual day count over the coupon period. Rate and dates synthetic.
BOND = Bond(
    **_common("UST:T-4.25-2035", "USD", ("0.0001", "100", "1")),
    face_value=Decimal(100),
    coupon_rate=Decimal("0.0425"),
    coupon_frequency=2,
    day_count=DayCount.ACT_ACT_ICMA,
    issue_ns=from_rfc3339("2025-01-15T00:00:00Z"),
    maturity_ns=from_rfc3339("2035-01-15T00:00:00Z"),
    first_coupon_ns=from_rfc3339("2025-07-15T00:00:00Z"),
)

FIXTURES: list[Instrument] = [EQUITY, FUTURE, OPTION, FX, CRYPTO_SPOT, PERPETUAL, BOND]


def test_one_fixture_per_asset_class() -> None:
    """The fixtures cover all six asset classes (ADR 0005)."""
    assert {type(f).asset_class for f in FIXTURES} == set(AssetClass)


@pytest.mark.parametrize("instrument", FIXTURES, ids=lambda i: type(i).__name__)
def test_instrument_round_trip(instrument: Instrument) -> None:
    """Every instrument type round-trips through JSON through the tagged union."""
    data = msgspec.json.encode(instrument)
    assert DECODER.decode(data) == instrument


def _replace(instrument: Instrument, **changes: Any) -> Instrument:  # noqa: ANN401
    return msgspec.structs.replace(instrument, **changes)


@pytest.mark.parametrize(
    ("instrument", "changes", "error"),
    [
        (EQUITY, {"price_increment": Decimal(0)}, InvalidDecimalError),
        (EQUITY, {"price_increment": Decimal("-0.01")}, InvalidDecimalError),
        (EQUITY, {"multiplier": Decimal(0)}, InvalidFieldError),
        (EQUITY, {"quote_currency": "usd"}, InvalidFieldError),
        (FUTURE, {"expiry_ns": FUTURE.last_trade_ns - 1}, InvalidFieldError),
        (OPTION, {"strike": Decimal(0)}, InvalidFieldError),
        (FX, {"settlement_lag_days": None}, InvalidFieldError),
        (FX, {"delivery": FxDelivery.FORWARD}, InvalidFieldError),
        (FX, {"base_currency": "USD"}, InvalidFieldError),
        (CRYPTO_SPOT, {"base_currency": "USDT"}, InvalidFieldError),
        (PERPETUAL, {"funding_interval_ns": 0}, InvalidFieldError),
        (BOND, {"coupon_frequency": 3}, InvalidFieldError),
        (BOND, {"coupon_frequency": 0}, InvalidFieldError),
        (BOND, {"maturity_ns": BOND.issue_ns}, InvalidFieldError),
        (BOND, {"first_coupon_ns": BOND.issue_ns}, InvalidFieldError),
    ],
)
def test_invalid_instruments_refused(
    instrument: Instrument, changes: dict[str, Any], error: type[Exception]
) -> None:
    """Contract violations are refused at construction."""
    with pytest.raises(error):
        _replace(instrument, **changes)


def test_identifiers_formats() -> None:
    """Identifier formats are checked when present."""
    Identifiers(isin="US0378331005", cfi="ESVUFR", figi="BBG000B9XRY4")
    for bad in ({"isin": "US037833100"}, {"cfi": "ESVUF"}, {"figi": "bbg000b9xry4"}):
        with pytest.raises(InvalidFieldError):
            Identifiers(**bad)


def test_currencies() -> None:
    """Fiat codes are ISO 4217 alphabetic; DTIs belong to digital tokens."""
    assert Currency("USD", CurrencyKind.FIAT, 2).minor_units == 2  # noqa: PLR2004
    Currency("USDT", CurrencyKind.CRYPTO, 8, dti="X9J9K872S")
    for args in (
        ("US", CurrencyKind.FIAT, 2),
        ("USD", CurrencyKind.FIAT, 19),
        ("usdt", CurrencyKind.CRYPTO, 8),
    ):
        with pytest.raises(InvalidFieldError):
            Currency(*args)
    with pytest.raises(InvalidFieldError):
        Currency("USD", CurrencyKind.FIAT, 2, dti="ABC")
