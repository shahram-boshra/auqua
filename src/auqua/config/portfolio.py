# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Portfolio section of the root configuration (BP6 section 4.11; ADR 0020).

Placed in ``auqua.config`` like the storage and ingestion sections, so that the
root model never imports a component. The repository bundles no currency list
and no margin or funding values (BP1 R5): the operator declares each one with
its published source and the date it was read (BP6 PC-6, R6, D3). The section
is empty by default; a portfolio computation that needs a missing entry
reports it as unavailable, never as zero.
"""

import datetime
import zoneinfo
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator

from auqua.config.base import ConfigModel
from auqua.contracts.currency import Currency, CurrencyKind
from auqua.contracts.identity import InstrumentId, SeriesId
from auqua.contracts.instruments import CURRENCY_CODE

PositiveInt = Annotated[int, Field(gt=0)]
PositiveDecimal = Annotated[Decimal, Field(gt=0)]
Label = Annotated[str, Field(min_length=1, pattern=r"^\S(?:.*\S)?$")]
#: Dates arrive as TOML local dates.
ReadOn = Annotated[datetime.date, Field(strict=False)]
CurrencyKindField = Annotated[CurrencyKind, Field(strict=False)]


class CurrencyEntry(ConfigModel):
    """One currency the account uses, with the source of its minor units.

    Attributes:
        kind: Fiat (ISO 4217) or crypto asset.
        minor_units: Decimal digits of money in this currency.
        source: Where the minor units were read (document or URL).
        read_on: The date they were read.
    """

    kind: CurrencyKindField
    minor_units: int = Field(ge=0)
    source: Label
    read_on: ReadOn

    def currency(self, code: str) -> Currency:
        """The contract value for ``code``.

        Raises:
            InvalidFieldError: ``code`` does not fit the kind, or the minor units are out of range.
        """
        return Currency(code=code, kind=self.kind, minor_units=self.minor_units)


class RateSource(ConfigModel):
    """Where the USD rate of one currency comes from (BP6 section 4.4): exactly one of them.

    Attributes:
        series: A reference series, ``PUBLISHER:CODE`` (FRB H.10).
        instrument: A traded pair quoted in USD, ``VENUE:SYMBOL``; its bar close is the rate.
    """

    series: str | None = None
    instrument: str | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if (self.series is None) == (self.instrument is None):
            raise ValueError("give exactly one of series and instrument")
        if self.series is not None:
            SeriesId.parse(self.series)
        if self.instrument is not None:
            InstrumentId.parse(self.instrument)
        return self


class MarginBasis(StrEnum):
    """How a margin value applies (BP6 section 4.6)."""

    PER_CONTRACT = "per_contract"
    NOTIONAL_FRACTION = "notional_fraction"


class MarginEntry(ConfigModel):
    """The published margin requirement of one instrument (BP6 PC-6).

    Attributes:
        basis: An amount per contract (futures) or a fraction of notional (perpetuals).
        initial: Initial requirement.
        maintenance: Maintenance requirement; not above ``initial``.
        currency: Currency of a per-contract amount, or of the notional.
        source: The published document the values were read from.
        read_on: The date they were read.
    """

    basis: Annotated[MarginBasis, Field(strict=False)]
    initial: PositiveDecimal
    maintenance: PositiveDecimal
    currency: str
    source: Label
    read_on: ReadOn

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if CURRENCY_CODE.fullmatch(self.currency) is None:
            raise ValueError(f"currency {self.currency!r} is not a currency code")
        if self.maintenance > self.initial:
            raise ValueError("maintenance must not exceed initial")
        if self.basis is MarginBasis.NOTIONAL_FRACTION and self.initial > 1:
            raise ValueError("a notional fraction is at most 1")
        return self


class FundingEntry(ConfigModel):
    """The funding-rate period of one perpetual (BP6 section 4.5 E, D3).

    Attributes:
        rate_period_ns: The period the published funding rate is expressed for
            (Deribit: "an 8-hour interest rate").
        source: The published document it was read from.
        read_on: The date it was read.
    """

    rate_period_ns: PositiveInt
    source: Label
    read_on: ReadOn


class FuturesEntry(ConfigModel):
    """The trading day of one future (BP6 amendment 6, F-N).

    A fill belongs to the settlement of trading date D when it falls after the
    end of the previously settled trading date and at or before the end of D;
    the end of D is ``trading_day_end`` on D in ``time_zone``.

    Attributes:
        time_zone: IANA time-zone name of the venue's trading day (e.g. ``America/Chicago``).
        trading_day_end: Local time at which a trading date ends.
        source: The published document the values were read from.
        read_on: The date they were read.
    """

    time_zone: Label
    trading_day_end: Annotated[datetime.time, Field(strict=False)]
    source: Label
    read_on: ReadOn

    @field_validator("time_zone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        try:
            zoneinfo.ZoneInfo(value)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError(f"unknown time zone {value!r}") from error
        return value

    def end_ns(self, trading_date: datetime.date) -> int:
        """The end of ``trading_date`` as UTC nanoseconds since the epoch."""
        local = datetime.datetime.combine(
            trading_date, self.trading_day_end, tzinfo=zoneinfo.ZoneInfo(self.time_zone)
        )
        delta = local - datetime.datetime(1970, 1, 1, tzinfo=datetime.UTC)
        return (delta.days * 86_400 + delta.seconds) * 10**9 + delta.microseconds * 1_000


class PortfolioConfig(ConfigModel):
    """The account's base currency, currencies, rate sources, margin and funding.

    Attributes:
        account_id: The one account of this environment (BP6 PC-2).
        base_currency: USD (BP6 Q-BP6-1).
        currencies: Currency code → entry.
        rates: Currency code → source of its USD rate.
        margin: Instrument ``VENUE:SYMBOL`` → requirement.
        funding: Perpetual ``VENUE:SYMBOL`` → funding-rate period.
        futures: Future ``VENUE:SYMBOL`` → trading day (amendment 6); a future
            without an entry is refused by the account fold.
    """

    account_id: Label = "primary"
    base_currency: Literal["USD"] = "USD"
    currencies: dict[str, CurrencyEntry] = Field(default_factory=dict)
    rates: dict[str, RateSource] = Field(default_factory=dict)
    margin: dict[str, MarginEntry] = Field(default_factory=dict)
    funding: dict[str, FundingEntry] = Field(default_factory=dict)
    futures: dict[str, FuturesEntry] = Field(default_factory=dict)

    @field_validator("margin", "funding", "futures")
    @classmethod
    def _instrument_keys(cls, value: dict[str, object]) -> dict[str, object]:
        for key in value:
            InstrumentId.parse(key)
        return value

    @model_validator(mode="after")
    def _references(self) -> Self:
        for code, entry in self.currencies.items():
            entry.currency(code)
        for code in self.rates:
            if code == self.base_currency:
                raise ValueError(f"{code} is the base currency; it has no rate")
            if code not in self.currencies:
                raise ValueError(f"rate for {code}, which is not among the currencies")
        for key, margin in self.margin.items():
            if margin.currency not in self.currencies:
                raise ValueError(f"margin of {key} in {margin.currency}, not among the currencies")
        return self
