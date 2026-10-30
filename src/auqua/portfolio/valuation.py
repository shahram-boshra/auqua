# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Conversion into the base currency USD at a point in time (BP6 section 4.4; PC-4; T12).

Each conversion reads the rate available at the decision time ``at_ns`` as
known at ``known_ns`` (ADR 0028) from the source the operator configured
(:class:`~auqua.config.portfolio.RateSource`):

- a reference series (FRB H.10): the latest released observation; its
  published ``unit`` decides the direction — ``"<CCY> per USD"`` divides the
  amount by the rate, ``"USD per <CCY>"`` multiplies it;
- a traded pair: the latest released bar close of a crypto spot pair whose base
  is the currency and whose quote is USD (USD per unit, multiplied).

The value is rounded once to the base currency's minor units (``HALF_EVEN``)
and returned with the rate, its unit, source and availability as a
:class:`~auqua.contracts.portfolio.Conversion`. Without a usable rate the
result is ``None``: unavailable, never estimated.

:func:`latest_close` is the one rule for "the latest available price" of an
instrument (BP6 amendment 10, F-Y), used by the conversion from a traded pair,
the account view's marks and allocation.
"""

from decimal import Decimal

from auqua.config.portfolio import PortfolioConfig
from auqua.contracts.events.market import Bar
from auqua.contracts.identity import InstrumentId, SeriesId
from auqua.contracts.instruments import CryptoSpot
from auqua.contracts.numeric import Rounding, divide, exact, quantize
from auqua.contracts.portfolio import BASE_CURRENCY, Conversion
from auqua.portfolio.errors import ConfigurationError
from auqua.storage.marketview import MarketView, available_ns
from auqua.storage.reference import ReferenceIndex


def latest_close(
    market: MarketView, instrument_id: InstrumentId, at_ns: int, known_ns: int
) -> Bar | None:
    """The bar with the latest available close of ``instrument_id`` at ``at_ns``, or ``None``.

    Across the instrument's bar series, the last bar released at ``at_ns`` as
    known at ``known_ns`` (ADR 0028); at equal availability the bar that started
    last (the shortest interval ending then). A close that is not positive is
    not a usable price: ``None`` (BP6 amendment 10, F-W, F-Y).
    """
    candidates = [
        bars[-1]
        for listed, interval in market.bar_series()
        if listed == instrument_id
        and (bars := market.released_bars(listed, interval, at_ns=at_ns, known_ns=known_ns))
    ]
    if not candidates:
        return None
    latest = max(candidates, key=lambda bar: (available_ns(bar), bar.start_ns))
    return latest if latest.close > 0 else None


class Valuation:
    """Converts amounts into USD from the configured rate sources."""

    def __init__(
        self, config: PortfolioConfig, market: MarketView, reference: ReferenceIndex
    ) -> None:
        """Bind the configuration, the market data and the instrument versions.

        Raises:
            ConfigurationError: the base currency is not among the configured
                currencies (its minor units are needed to round).
        """
        base = config.currencies.get(BASE_CURRENCY)
        if base is None:
            raise ConfigurationError(f"{BASE_CURRENCY} is not among the configured currencies")
        self._increment = Decimal(1).scaleb(-base.minor_units)
        self._config = config
        self._market = market
        self._reference = reference

    def convert(
        self, amount: Decimal, currency: str, *, at_ns: int, known_ns: int
    ) -> Conversion | None:
        """``amount`` of ``currency`` in USD at ``at_ns``, or ``None`` without a usable rate.

        Raises:
            ValueError: ``currency`` is the base currency (nothing to convert).
        """
        if currency == BASE_CURRENCY:
            raise ValueError(f"{BASE_CURRENCY} needs no conversion")
        source = self._config.rates.get(currency)
        if source is None:
            return None
        if source.series is not None:
            return self._from_series(
                amount, currency, SeriesId.parse(source.series), at_ns, known_ns
            )
        if source.instrument is not None:
            instrument_id = InstrumentId.parse(source.instrument)
            return self._from_pair(amount, currency, instrument_id, at_ns, known_ns)
        return None  # pragma: no cover - RateSource requires exactly one source

    def _from_series(
        self, amount: Decimal, currency: str, series: SeriesId, at_ns: int, known_ns: int
    ) -> Conversion | None:
        observations = self._market.released_rates(series, at_ns=at_ns, known_ns=known_ns)
        if not observations:
            return None
        rate = observations[-1]
        if rate.value <= 0:
            return None
        if rate.unit == f"{currency} per {BASE_CURRENCY}":
            value = divide(amount, rate.value, self._increment, Rounding.HALF_EVEN)
        elif rate.unit == f"{BASE_CURRENCY} per {currency}":
            value = self._times(amount, rate.value)
        else:
            return None
        return Conversion(
            amount=amount,
            from_currency=currency,
            to_currency=BASE_CURRENCY,
            rate=rate.value,
            unit=rate.unit,
            source=str(series),
            rate_available_ns=available_ns(rate),
            value=value,
        )

    def _from_pair(
        self, amount: Decimal, currency: str, instrument_id: InstrumentId, at_ns: int, known_ns: int
    ) -> Conversion | None:
        instrument = self._reference.as_of(instrument_id, at_ns, known_ns)
        if not (
            isinstance(instrument, CryptoSpot)
            and instrument.base_currency == currency
            and instrument.quote_currency == BASE_CURRENCY
        ):
            return None
        latest = latest_close(self._market, instrument_id, at_ns, known_ns)
        if latest is None:
            return None
        unit = f"{BASE_CURRENCY} per {currency}"
        value = self._times(amount, latest.close)
        return Conversion(
            amount=amount,
            from_currency=currency,
            to_currency=BASE_CURRENCY,
            rate=latest.close,
            unit=unit,
            source=str(instrument_id),
            rate_available_ns=available_ns(latest),
            value=value,
        )

    def _times(self, amount: Decimal, rate: Decimal) -> Decimal:
        with exact():
            product = amount * rate
        return quantize(product, self._increment, Rounding.HALF_EVEN)
