# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Allocation: target quantities from signals and the account view (BP6 section 4.9; T14).

An :class:`Allocator` is built with its inputs (market data, valuation,
instrument versions, its specification) and is pure: :meth:`Allocator.propose`
reads the signals and an :class:`~auqua.contracts.portfolio.AccountView` and
returns a :class:`Proposal` of plain rows; the caller makes the
``portfolio.target.v1`` and ``portfolio.allocation_unavailable.v1`` events with
its run-scoped ids (ADR 0025; amendment 10, F-Y). The decision time is the
view's ``as_of_ns``; the knowledge time is the allocator's ``knowledge_ns``
when given (a backtest: its input snapshot's; amendment 12, F-AB), else the
decision time (live). A proposal above a risk limit is cut by the pre-trade
gate (BP7), never here.

- :class:`FixedQuantity`: ``quantity x direction`` (the walking skeleton; PC-10).
- :class:`VolatilityTarget`: ``q = direction x C x sigma* / (sigma_hat x N)``, capped by
  ``|q| <= L x C / N``, each quotient one rational in USD rounded once toward
  zero onto the quantity increment (``numeric.divide``, ``Rounding.DOWN``).
  C is ``equity_base``; N is the USD value of one unit: P x M, or M for an
  inverse perpetual (its USD notional per contract, amendment 10, F-X), with
  the quote currency's rate folded into the quotient, never a rounded
  ``Conversion.value`` (E1). sigma_hat is :func:`volatility` (ADR 0037).
- :class:`Optimizer`: a protocol only (a36: ``cvxpy`` and its solvers wait).

No proposal is estimated (P4): checks run in the order equity, price, rate,
volatility, and the first that fails names the reason. A flat target
(direction 0) needs no size and is always proposed.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, Literal, Protocol

from pydantic import Field

from auqua.config.base import ConfigModel
from auqua.contracts.events.market import Bar
from auqua.contracts.events.trading import AllocationUnavailableReason, Signal
from auqua.contracts.identity import InstrumentId
from auqua.contracts.instruments import AnyInstrument, PerpetualSwap, PerpetualType
from auqua.contracts.numeric import Rounding, divide, exact, from_float, on_grid, quantize
from auqua.contracts.portfolio import BASE_CURRENCY, AccountView, Conversion
from auqua.portfolio.errors import PortfolioError
from auqua.portfolio.series import NS_PER_DAY, daily_last, periods_per_year, simple_returns
from auqua.portfolio.valuation import Valuation, latest_close
from auqua.storage.marketview import MarketView, available_ns
from auqua.storage.reference import ReferenceIndex

#: Increment of sigma_hat in trading state, rounded away from zero (ADR 0037; C9).
SIGMA_INCREMENT = Decimal("1E-12")


class FixedQuantitySpec(ConfigModel):
    """``quantity x direction`` (experiment specification version 1's ``quantity``)."""

    method: Literal["fixed_quantity"] = "fixed_quantity"
    quantity: Annotated[Decimal, Field(gt=0)]

    def assumptions(self) -> dict[str, str]:
        """The sizing as reported among a run's assumptions."""
        return {"allocation": self.method, "quantity": str(self.quantity)}


class VolatilityTargetSpec(ConfigModel):
    """Volatility targeting (Harvey et al. 2018; BP6 section 4.9).

    Attributes:
        target_volatility: sigma*, annualised (0.10 = 10 %).
        leverage_cap: L; ``|q| x N <= L x C``.
        half_life_days: H, in daily returns (default 20).
        warmup_days: W, daily returns required before the first proposal (default 270).
    """

    method: Literal["volatility_target"] = "volatility_target"
    target_volatility: Annotated[Decimal, Field(gt=0)]
    leverage_cap: Annotated[Decimal, Field(gt=0)]
    half_life_days: Annotated[int, Field(ge=1)] = 20
    warmup_days: Annotated[int, Field(ge=1)] = 270

    def assumptions(self) -> dict[str, str]:
        """The sizing as reported among a run's assumptions."""
        return {
            "allocation": self.method,
            "target_volatility": str(self.target_volatility),
            "leverage_cap": str(self.leverage_cap),
            "half_life_days": str(self.half_life_days),
            "warmup_days": str(self.warmup_days),
        }


#: The ``allocation`` section of the experiment specification (version 2).
AllocationSpec = Annotated[FixedQuantitySpec | VolatilityTargetSpec, Field(discriminator="method")]


@dataclass(frozen=True)
class ProposedTarget:
    """A target quantity; ``conversion`` records the rate used for N (audit; its value unused)."""

    instrument_id: InstrumentId
    quantity: Decimal
    conversion: Conversion | None = None


@dataclass(frozen=True)
class Unavailable:
    """No target for an instrument, and why."""

    instrument_id: InstrumentId
    reason: AllocationUnavailableReason


@dataclass(frozen=True)
class Proposal:
    """Targets and unavailable instruments, in the order of the signals."""

    targets: tuple[ProposedTarget, ...]
    unavailable: tuple[Unavailable, ...]


class Allocator(Protocol):
    """Turns signals and the account view into target quantities."""

    def propose(self, signals: Sequence[Signal], view: AccountView) -> Proposal:
        """The proposal at ``view.as_of_ns``."""
        ...


class Optimizer(Allocator, Protocol):
    """An allocator that solves a constrained program: a contract only (a36; ADR 0037)."""


def _distinct(signals: Sequence[Signal]) -> None:
    ids = [s.instrument_id for s in signals]
    if len(set(ids)) != len(ids):
        raise PortfolioError("one signal per instrument")


def _on_grid(quantity: Decimal, instrument: AnyInstrument) -> Decimal:
    """``quantity`` (a multiple of the quantity increment) in the increment's scale (PC-9).

    Every target, flat included, is canonical: the same value has the same bytes
    whichever allocator proposed it (BP6 amendment 13, point 6; amendment 14).

    Raises:
        OffGridError: ``quantity`` is not a multiple of the quantity increment.
    """
    increment = instrument.quantity_increment
    on_grid(quantity, increment)
    return quantize(quantity, increment, Rounding.DOWN)


def _instrument(
    reference: ReferenceIndex, instrument_id: InstrumentId, t: int, known: int
) -> AnyInstrument:
    instrument = reference.as_of(instrument_id, t, known)
    if instrument is None:
        raise PortfolioError(f"{instrument_id} is not defined at {t}")
    return instrument


class FixedQuantity:
    """``quantity x direction``, on the instrument's quantity grid."""

    def __init__(
        self,
        spec: FixedQuantitySpec,
        reference: ReferenceIndex,
        *,
        knowledge_ns: int | None = None,
    ) -> None:
        """Bind the quantity, the instrument versions and the knowledge time (``None``: live)."""
        self._spec = spec
        self._reference = reference
        self._knowledge_ns = knowledge_ns

    def propose(self, signals: Sequence[Signal], view: AccountView) -> Proposal:
        """One target per signal.

        Raises:
            PortfolioError: an instrument is not defined, or two signals name one.
            OffGridError: the quantity is not on the instrument's grid.
        """
        _distinct(signals)
        targets = []
        for signal in signals:
            t = view.as_of_ns
            known = t if self._knowledge_ns is None else self._knowledge_ns
            instrument = _instrument(self._reference, signal.instrument_id, t, known)
            on_grid(self._spec.quantity, instrument.quantity_increment)
            with exact():
                signed = self._spec.quantity * signal.direction
            quantity = _on_grid(signed, instrument)
            targets.append(ProposedTarget(signal.instrument_id, quantity))
        return Proposal(tuple(targets), ())


def daily_closes(
    market: MarketView, instrument_id: InstrumentId, t: int, known_ns: int | None = None
) -> list[Decimal]:
    """Closes of the UTC days ended at ``t``, oldest first (BP6 section 4.9 D2; amendment 10, F-V).

    A close's day is that of its interval end, ``(ts_event_ns - 1) // NS_PER_DAY``;
    it is used at ``t`` when it is released (``available_ns <= t``, as known at
    ``known_ns``, default ``t``) and its day has ended (``(day + 1) x NS_PER_DAY
    <= t``). Per day, the close of the bar ending last (at equal end, the later
    available, then the later start) across the instrument's bar series.
    """
    known = t if known_ns is None else known_ns
    bars: list[Bar] = [
        bar
        for listed, interval in market.bar_series()
        if listed == instrument_id
        for bar in market.released_bars(listed, interval, at_ns=t, known_ns=known)
        if ((bar.ts_event_ns - 1) // NS_PER_DAY + 1) * NS_PER_DAY <= t
    ]
    bars.sort(key=lambda bar: (bar.ts_event_ns, available_ns(bar), bar.start_ns))
    days = daily_last([bar.ts_event_ns - 1 for bar in bars], [bar.close for bar in bars])
    return [close for _, close in days]


def volatility(
    closes: Sequence[Decimal], *, half_life: int, window: int, periods_per_year: int
) -> float | AllocationUnavailableReason:
    """sigma_hat: annualised EWMA volatility of the newest ``window`` daily returns.

    ``sigma_d^2 = sum(w_i r_i^2) / sum(w_i)``, ``w_i = 0.5^(i / H)``, ``i = 0`` the newest
    return (zero mean: Harvey et al. 2018, p. 16, fn. 8); ``sigma_hat = sigma_d x sqrt(A)``
    (ADR 0037, D2). Floats stay inside this function (C9).

    Returns:
        sigma_hat, or ``VOLATILITY_WARMUP`` (fewer than ``window`` returns) or
        ``VOLATILITY_INVALID`` (a non-positive close in the window; sigma_hat zero or not finite).
    """
    if len(closes) < window + 1:
        return AllocationUnavailableReason.VOLATILITY_WARMUP
    used = closes[-(window + 1) :]
    if any(close <= 0 for close in used):
        return AllocationUnavailableReason.VOLATILITY_INVALID
    newest_first = list(reversed(simple_returns(used)))
    weights = [0.5 ** (i / half_life) for i in range(window)]
    variance = math.fsum(w * r * r for w, r in zip(weights, newest_first, strict=True))
    sigma = math.sqrt(variance / math.fsum(weights) * periods_per_year)
    if not math.isfinite(sigma) or sigma == 0:
        return AllocationUnavailableReason.VOLATILITY_INVALID
    return sigma


class VolatilityTarget:
    """Volatility targeting in USD with a leverage cap (BP6 section 4.9; Q-BP6-2)."""

    def __init__(
        self,
        spec: VolatilityTargetSpec,
        *,
        market: MarketView,
        valuation: Valuation,
        reference: ReferenceIndex,
        knowledge_ns: int | None = None,
    ) -> None:
        """Bind the parameters, market data, rates, instrument versions and knowledge time.

        ``knowledge_ns`` ``None``: lookups as known at the decision time (live).
        """
        self._spec = spec
        self._market = market
        self._valuation = valuation
        self._reference = reference
        self._knowledge_ns = knowledge_ns

    def propose(self, signals: Sequence[Signal], view: AccountView) -> Proposal:
        """One target or one unavailable row per signal.

        Raises:
            PortfolioError: an instrument is not defined, two signals name one,
                or a linear perpetual (its fills are refused, BP6 R1).
        """
        _distinct(signals)
        targets: list[ProposedTarget] = []
        unavailable: list[Unavailable] = []
        for signal in signals:
            row = self._one(signal, view)
            if isinstance(row, Unavailable):
                unavailable.append(row)
            else:
                targets.append(row)
        return Proposal(tuple(targets), tuple(unavailable))

    def _one(self, signal: Signal, view: AccountView) -> ProposedTarget | Unavailable:
        t = view.as_of_ns
        known = t if self._knowledge_ns is None else self._knowledge_ns
        instrument = _instrument(self._reference, signal.instrument_id, t, known)
        inverse = isinstance(instrument, PerpetualSwap) and (
            instrument.contract_type is PerpetualType.INVERSE
        )
        if isinstance(instrument, PerpetualSwap) and not inverse:
            raise PortfolioError(f"{instrument.id}: linear perpetuals are refused (BP6 R1)")
        if signal.direction == 0:
            return ProposedTarget(signal.instrument_id, _on_grid(Decimal(0), instrument))
        inputs = self._inputs(instrument, view.equity_base, t, known, inverse=inverse)
        if isinstance(inputs, AllocationUnavailableReason):
            return Unavailable(signal.instrument_id, inputs)
        equity, unit, conversion, sigma = inputs
        size = self._size(equity, unit, sigma, conversion, instrument)
        quantity = _on_grid(size if signal.direction > 0 else -size, instrument)  # -0 is +0
        return ProposedTarget(signal.instrument_id, quantity, conversion)

    def _inputs(
        self,
        instrument: AnyInstrument,
        equity: Decimal | None,
        t: int,
        known: int,
        *,
        inverse: bool,
    ) -> tuple[Decimal, Decimal, Conversion | None, Decimal] | AllocationUnavailableReason:
        """C, N in the quote currency, the rate's conversion and sigma_hat; else the reason."""
        if equity is None:
            return AllocationUnavailableReason.EQUITY_INCOMPLETE
        if equity <= 0:
            return AllocationUnavailableReason.EQUITY_NOT_POSITIVE
        if inverse:
            unit = instrument.multiplier
        else:
            price = latest_close(self._market, instrument.id, t, known)
            if price is None:
                return AllocationUnavailableReason.PRICE_UNAVAILABLE
            with exact():
                unit = price.close * instrument.multiplier
        conversion = None
        if instrument.quote_currency != BASE_CURRENCY:
            conversion = self._valuation.convert(
                unit, instrument.quote_currency, at_ns=t, known_ns=known
            )
            if conversion is None:
                return AllocationUnavailableReason.RATE_UNAVAILABLE
        sigma = volatility(
            daily_closes(self._market, instrument.id, t, known),
            half_life=self._spec.half_life_days,
            window=self._spec.warmup_days,
            periods_per_year=periods_per_year(instrument.asset_class),
        )
        if isinstance(sigma, AllocationUnavailableReason):
            return sigma
        return equity, unit, conversion, from_float(sigma, SIGMA_INCREMENT, Rounding.UP)

    def _size(
        self,
        equity: Decimal,
        unit: Decimal,
        sigma: Decimal,
        conversion: Conversion | None,
        instrument: AnyInstrument,
    ) -> Decimal:
        """min(C sigma* / (sigma_hat N), L C / N), one rational each, the rate folded in (E1)."""
        target, cap = self._spec.target_volatility, self._spec.leverage_cap
        with exact():
            if conversion is None:  # USD quote: N = unit
                q_num, q_den = equity * target, sigma * unit
                c_num, c_den = cap * equity, unit
            elif conversion.unit == f"{conversion.from_currency} per {BASE_CURRENCY}":
                q_num, q_den = equity * target * conversion.rate, sigma * unit
                c_num, c_den = cap * equity * conversion.rate, unit
            else:  # "USD per <CCY>" (H.10 or a crypto close)
                q_num, q_den = equity * target, sigma * unit * conversion.rate
                c_num, c_den = cap * equity, unit * conversion.rate
        increment = instrument.quantity_increment
        return min(
            divide(q_num, q_den, increment, Rounding.DOWN),
            divide(c_num, c_den, increment, Rounding.DOWN),
        )


def allocator(
    spec: FixedQuantitySpec | VolatilityTargetSpec,
    *,
    market: MarketView,
    valuation: Valuation,
    reference: ReferenceIndex,
    knowledge_ns: int | None = None,
) -> Allocator:
    """The allocator of ``spec``, looking up as known at ``knowledge_ns`` (``None``: live)."""
    if isinstance(spec, FixedQuantitySpec):
        return FixedQuantity(spec, reference, knowledge_ns=knowledge_ns)
    return VolatilityTarget(
        spec, market=market, valuation=valuation, reference=reference, knowledge_ns=knowledge_ns
    )
