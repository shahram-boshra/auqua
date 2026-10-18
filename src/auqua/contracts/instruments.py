# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Instrument model for the six asset classes (ADR 0005, ADR 0017).

Each instrument version is immutable: a changed specification is a new
version with later ``valid_from_ns`` / ``knowledge_ns``, never an edit.
External identifiers (FIGI, ISIN, CFI, DTI) are optional attributes, never keys.
"""

import re
from decimal import Decimal
from enum import StrEnum
from typing import ClassVar

import msgspec

from auqua.contracts.currency import CRYPTO_CODE, DTI_CODE, FIAT_CODE
from auqua.contracts.errors import InvalidFieldError
from auqua.contracts.identity import InstrumentId
from auqua.contracts.numeric import require_increment
from auqua.contracts.validation import (
    require_match,
    require_non_negative,
    require_positive,
    require_positive_int,
    require_time_ns,
)

#: Currency or asset code (fiat or crypto).
CURRENCY_CODE = re.compile(f"{FIAT_CODE.pattern}|{CRYPTO_CODE.pattern}")
#: Twelve upper-case alphanumerics; the check character is verified by the source.
FIGI_PATTERN = re.compile(r"[A-Z0-9]{12}")
#: ISO 6166: country code, nine alphanumerics, one check digit.
ISIN_PATTERN = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")
#: ISO 10962: six upper-case letters.
CFI_PATTERN = re.compile(r"[A-Z]{6}")
#: Coupon payments per year accepted for bonds (0: zero-coupon).
COUPON_FREQUENCIES = frozenset({0, 1, 2, 4, 12})


class AssetClass(StrEnum):
    """The six asset classes (ADR 0005)."""

    EQUITY = "equity"
    FUTURE = "future"
    OPTION = "option"
    FX = "fx"
    CRYPTO = "crypto"
    FIXED_INCOME = "fixed_income"


class Settlement(StrEnum):
    """Settlement of a derivative at expiry or exercise."""

    CASH = "cash"
    PHYSICAL = "physical"


class OptionRight(StrEnum):
    """Call or put."""

    CALL = "call"
    PUT = "put"


class ExerciseStyle(StrEnum):
    """When an option may be exercised."""

    EUROPEAN = "european"
    AMERICAN = "american"


class FxDelivery(StrEnum):
    """Spot or forward delivery."""

    SPOT = "spot"
    FORWARD = "forward"


class PerpetualType(StrEnum):
    """Linear (quote-margined) or inverse (coin-margined) perpetual swap."""

    LINEAR = "linear"
    INVERSE = "inverse"


class DayCount(StrEnum):
    """Day-count convention names; the arithmetic belongs to BP6."""

    ACT_360 = "act_360"
    ACT_365_FIXED = "act_365_fixed"
    ACT_ACT_ICMA = "act_act_icma"
    ACT_ACT_ISDA = "act_act_isda"
    THIRTY_360 = "thirty_360"


class Identifiers(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Optional external identifiers, each recorded only when a source supplies it."""

    figi: str | None = None
    isin: str | None = None
    cfi: str | None = None
    dti: str | None = None

    def __post_init__(self) -> None:
        """Validate the format of each identifier present."""
        for value, pattern, name in (
            (self.figi, FIGI_PATTERN, "figi"),
            (self.isin, ISIN_PATTERN, "isin"),
            (self.cfi, CFI_PATTERN, "cfi"),
            (self.dti, DTI_CODE, "dti"),
        ):
            if value is not None:
                require_match(value, pattern, name)


class Instrument(msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag_field="type"):
    """Fields common to every instrument version."""

    asset_class: ClassVar[AssetClass]

    id: InstrumentId
    quote_currency: str
    price_increment: Decimal
    quantity_increment: Decimal
    multiplier: Decimal
    valid_from_ns: int
    knowledge_ns: int
    identifiers: Identifiers

    def __post_init__(self) -> None:
        """Validate the fields common to every instrument."""
        require_match(self.quote_currency, CURRENCY_CODE, "quote_currency")
        require_increment(self.price_increment, "price_increment")
        require_increment(self.quantity_increment, "quantity_increment")
        require_positive(self.multiplier, "multiplier")
        require_time_ns(self.valid_from_ns, "valid_from_ns")
        require_time_ns(self.knowledge_ns, "knowledge_ns")


class Equity(Instrument, frozen=True, tag="instrument.equity.v1"):
    """Share or exchange-traded fund (``is_fund``)."""

    asset_class: ClassVar[AssetClass] = AssetClass.EQUITY

    is_fund: bool


class Future(Instrument, frozen=True, tag="instrument.future.v1"):
    """Listed future."""

    asset_class: ClassVar[AssetClass] = AssetClass.FUTURE

    underlying: str
    expiry_ns: int
    last_trade_ns: int
    settlement: Settlement

    def __post_init__(self) -> None:
        """Validate expiry against the last trading time."""
        super().__post_init__()
        require_time_ns(self.last_trade_ns, "last_trade_ns")
        if self.expiry_ns < self.last_trade_ns:
            raise InvalidFieldError("expiry_ns must not precede last_trade_ns")


class Option(Instrument, frozen=True, tag="instrument.option.v1"):
    """Listed option."""

    asset_class: ClassVar[AssetClass] = AssetClass.OPTION

    underlying: str
    right: OptionRight
    strike: Decimal
    expiry_ns: int
    exercise_style: ExerciseStyle
    settlement: Settlement

    def __post_init__(self) -> None:
        """Validate the strike and expiry."""
        super().__post_init__()
        require_positive(self.strike, "strike")
        require_time_ns(self.expiry_ns, "expiry_ns")


class FxPair(Instrument, frozen=True, tag="instrument.fx.v1"):
    """Currency pair: price of one ``base_currency`` unit in ``quote_currency``.

    Spot carries ``settlement_lag_days``; a forward carries ``value_date_ns``.
    """

    asset_class: ClassVar[AssetClass] = AssetClass.FX

    base_currency: str
    delivery: FxDelivery
    settlement_lag_days: int | None = None
    value_date_ns: int | None = None

    def __post_init__(self) -> None:
        """Validate the currencies and the delivery terms."""
        super().__post_init__()
        require_match(self.base_currency, FIAT_CODE, "base_currency")
        require_match(self.quote_currency, FIAT_CODE, "quote_currency")
        if self.base_currency == self.quote_currency:
            raise InvalidFieldError("base_currency and quote_currency must differ")
        if self.delivery is FxDelivery.SPOT:
            if self.settlement_lag_days is None or self.value_date_ns is not None:
                raise InvalidFieldError("spot needs settlement_lag_days and no value_date_ns")
            if self.settlement_lag_days < 0:
                raise InvalidFieldError("settlement_lag_days must be >= 0")
        else:
            if self.value_date_ns is None or self.settlement_lag_days is not None:
                raise InvalidFieldError("a forward needs value_date_ns and no settlement_lag_days")
            require_time_ns(self.value_date_ns, "value_date_ns")


class CryptoSpot(Instrument, frozen=True, tag="instrument.crypto_spot.v1"):
    """Crypto spot pair: price of one ``base_currency`` unit in ``quote_currency``."""

    asset_class: ClassVar[AssetClass] = AssetClass.CRYPTO

    base_currency: str

    def __post_init__(self) -> None:
        """Validate the base asset."""
        super().__post_init__()
        require_match(self.base_currency, CRYPTO_CODE, "base_currency")
        if self.base_currency == self.quote_currency:
            raise InvalidFieldError("base_currency and quote_currency must differ")


class PerpetualSwap(Instrument, frozen=True, tag="instrument.perpetual_swap.v1"):
    """Crypto perpetual swap; ``contract_type`` selects linear or inverse arithmetic (BP6)."""

    asset_class: ClassVar[AssetClass] = AssetClass.CRYPTO

    base_currency: str
    settlement_currency: str
    contract_type: PerpetualType
    funding_interval_ns: int

    def __post_init__(self) -> None:
        """Validate the assets and the funding interval."""
        super().__post_init__()
        require_match(self.base_currency, CRYPTO_CODE, "base_currency")
        require_match(self.settlement_currency, CURRENCY_CODE, "settlement_currency")
        require_positive_int(self.funding_interval_ns, "funding_interval_ns")


class Bond(Instrument, frozen=True, tag="instrument.bond.v1"):
    """Fixed-rate bond; ``coupon_frequency`` 0 means zero-coupon."""

    asset_class: ClassVar[AssetClass] = AssetClass.FIXED_INCOME

    face_value: Decimal
    coupon_rate: Decimal
    coupon_frequency: int
    day_count: DayCount
    issue_ns: int
    maturity_ns: int
    first_coupon_ns: int | None = None

    def __post_init__(self) -> None:
        """Validate face value, coupon terms and dates."""
        super().__post_init__()
        require_positive(self.face_value, "face_value")
        require_non_negative(self.coupon_rate, "coupon_rate")
        if self.coupon_frequency not in COUPON_FREQUENCIES:
            raise InvalidFieldError(f"coupon_frequency must be one of {sorted(COUPON_FREQUENCIES)}")
        if (self.coupon_frequency == 0) != (self.coupon_rate == 0):
            raise InvalidFieldError("zero-coupon bonds need coupon_rate 0 and coupon_frequency 0")
        require_time_ns(self.issue_ns, "issue_ns")
        if self.maturity_ns <= self.issue_ns:
            raise InvalidFieldError("maturity_ns must be after issue_ns")
        if self.first_coupon_ns is not None and not (
            self.issue_ns < self.first_coupon_ns <= self.maturity_ns
        ):
            raise InvalidFieldError("first_coupon_ns must fall after issue and not after maturity")


AnyInstrument = Equity | Future | Option | FxPair | CryptoSpot | PerpetualSwap | Bond
