# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Margin requirements from configured values (BP6 section 4.6; PC-6; T21).

Phase 1 model :class:`PerInstrumentMargin`: per instrument, either an amount
per contract (futures) or a fraction of notional (perpetuals), each value
configured with its published source and the date it was read
(:class:`~auqua.config.portfolio.MarginEntry`). Requirements are exact
products, never posted and not rounded. An instrument with a position and no
usable requirement is reported as unmargined, never as zero (amendment 1).

A notional fraction applies to the notional ``|quantity| x multiplier x
mark`` in the instrument's quote currency, so the entry's currency must be
that currency. An inverse perpetual's notional is in USD while its margin is
held in the coin, and converting it is a division: its requirement is
unavailable in phase 1.
"""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from auqua.config.portfolio import MarginBasis, PortfolioConfig
from auqua.contracts.identity import InstrumentId
from auqua.contracts.instruments import Instrument, PerpetualSwap, PerpetualType
from auqua.contracts.numeric import exact
from auqua.contracts.portfolio import MarginView
from auqua.portfolio.errors import PortfolioError


@dataclass(frozen=True)
class Requirement:
    """Initial and maintenance requirement of one position, in ``currency``."""

    currency: str
    initial: Decimal
    maintenance: Decimal


class PerInstrumentMargin:
    """Requirements from the configured ``margin`` entries (BP6 section 4.6)."""

    def __init__(self, config: PortfolioConfig) -> None:
        """Bind the configured entries."""
        self._entries = config.margin

    def requirement(
        self, instrument: Instrument, quantity: Decimal, mark: Decimal | None
    ) -> Requirement | None:
        """The requirement of ``quantity`` (signed) at ``mark``, or ``None`` if unavailable.

        Unavailable: no entry; a notional fraction without a mark, in a currency
        other than the quote currency, or of an inverse perpetual.
        """
        entry = self._entries.get(str(instrument.id))
        if entry is None:
            return None
        size = abs(quantity)
        if entry.basis is MarginBasis.PER_CONTRACT:
            with exact():
                return Requirement(entry.currency, entry.initial * size, entry.maintenance * size)
        inverse = (
            isinstance(instrument, PerpetualSwap)
            and instrument.contract_type is PerpetualType.INVERSE
        )
        if mark is None or inverse or entry.currency != instrument.quote_currency:
            return None
        with exact():
            notional = size * instrument.multiplier * mark
            return Requirement(
                entry.currency, entry.initial * notional, entry.maintenance * notional
            )


def margin_rows(
    model: PerInstrumentMargin,
    positions: Sequence[tuple[Instrument, Decimal, Decimal | None]],
    equity: Mapping[str, Decimal],
) -> tuple[tuple[MarginView, ...], tuple[InstrumentId, ...]]:
    """Margin per currency and the unmargined instruments, both in increasing key order.

    ``positions`` are (instrument, signed quantity, mark); flat positions need
    no margin. ``equity`` is the equity per currency the requirements are
    compared with.

    Raises:
        PortfolioError: no equity is given for a currency with a requirement.
    """
    initial: defaultdict[str, Decimal] = defaultdict(Decimal)
    maintenance: defaultdict[str, Decimal] = defaultdict(Decimal)
    unmargined: list[InstrumentId] = []
    for instrument, quantity, mark in positions:
        if quantity.is_zero():
            continue
        required = model.requirement(instrument, quantity, mark)
        if required is None:
            unmargined.append(instrument.id)
            continue
        with exact():
            initial[required.currency] += required.initial
            maintenance[required.currency] += required.maintenance
    rows = []
    for currency in sorted(initial):
        if currency not in equity:
            raise PortfolioError(f"no equity in {currency} to compare with its margin")
        with exact():
            rows.append(
                MarginView(
                    currency=currency,
                    initial=initial[currency],
                    maintenance=maintenance[currency],
                    equity=equity[currency],
                    available=equity[currency] - initial[currency],
                    deficit=max(Decimal(0), maintenance[currency] - equity[currency]),
                )
            )
    return tuple(rows), tuple(sorted(unmargined, key=str))
