# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Experiment specification (BP5 sections 4 and 4.1; ADR 0030).

The hypothesis, the periods, the configurations to try, the execution
assumptions and the validation method are declared before a run, separately
from the platform configuration and from the evidence (Strategy Module 3).
Periods are half-open UTC intervals and must be ordered and disjoint:
train < validation < test. Selection uses train and validation only; the test
period is read only by :func:`auqua.analytics.experiment.evaluate_test`.

Version 2 (BP6 section 4.9, amendment 10, F-Z) adds ``allocation``, the
position sizing: ``fixed_quantity`` (version 1's ``quantity``) or
``volatility_target``. Every document is upgraded before validation through
:data:`SPEC_MIGRATIONS` (ADR 0020's registry, keyed by ``spec_version``), so
the command line, the API and the tests take one path. A document without
``spec_version`` is version 1 (the version-1 model's default); version 2 states
``spec_version = 2``.
"""

import datetime
import itertools
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, Field, model_validator

from auqua.config.base import ConfigModel
from auqua.config.errors import UnsupportedVersionError
from auqua.config.migrate import Document, Migrations
from auqua.contracts.events.research import Period, PeriodName
from auqua.portfolio.allocation import AllocationSpec

PositiveInt = Annotated[int, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]
_NS_PER_SECOND = 1_000_000_000
SPEC_VERSION_KEY = "spec_version"
#: Experiment specification versions (ADR 0020 migrations, keyed by ``spec_version``).
SPEC_MIGRATIONS = Migrations("experiment spec", current=2, key=SPEC_VERSION_KEY)


def _v1_to_v2(document: Document) -> Document:
    """Version 1's ``quantity`` becomes ``allocation = {method = "fixed_quantity", quantity}``."""
    if "allocation" in document:
        raise UnsupportedVersionError(
            "experiment spec: version 1 has no allocation (state spec_version = 2)"
        )
    upgraded = {k: v for k, v in document.items() if k != "quantity"}
    allocation: dict[str, Any] = {"method": "fixed_quantity"}
    if "quantity" in document:
        allocation["quantity"] = document["quantity"]
    return upgraded | {SPEC_VERSION_KEY: 2, "allocation": allocation}


SPEC_MIGRATIONS.register(1, _v1_to_v2)


def _ns(moment: datetime.datetime) -> int:
    delta = moment - datetime.datetime(1970, 1, 1, tzinfo=datetime.UTC)
    return (delta.days * 86_400 + delta.seconds) * _NS_PER_SECOND + delta.microseconds * 1_000


class PeriodSpec(ConfigModel):
    """A half-open interval ``[start, end)`` with an explicit UTC offset."""

    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def _ordered(self) -> PeriodSpec:
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self

    def period(self, name: PeriodName) -> Period:
        """The period as recorded in the run record."""
        return Period(name, _ns(self.start), _ns(self.end))


class SignalGrid(ConfigModel):
    """Configurations of the baseline crossover: every ``fast < slow`` pair."""

    provider: Literal["baseline/ma-crossover"] = "baseline/ma-crossover"
    fast: list[PositiveInt] = Field(min_length=1)
    slow: list[PositiveInt] = Field(min_length=1)

    def configurations(self) -> list[tuple[int, int]]:
        """The ``(fast, slow)`` pairs with ``fast < slow``, sorted.

        Raises:
            ValueError: no valid pair.
        """
        pairs = sorted({(f, s) for f, s in itertools.product(self.fast, self.slow) if f < s})
        if not pairs:
            raise ValueError("the grid holds no pair with fast < slow")
        return pairs


class WalkForward(ConfigModel):
    """Walk-forward validation of the selection procedure (in bars)."""

    method: Literal["walk_forward"] = "walk_forward"
    window_bars: PositiveInt
    step_bars: PositiveInt
    anchored: bool = False


class PurgedKFold(ConfigModel):
    """Purged k-fold validation with embargo (in bars)."""

    method: Literal["purged_kfold"] = "purged_kfold"
    k: Annotated[int, Field(ge=2)]
    horizon_bars: NonNegativeInt = 0
    embargo_bars: NonNegativeInt = 0


class ExperimentSpec(ConfigModel):
    """One experiment: a hypothesis tested over a grid of configurations.

    Attributes:
        spec_version: Version of this model.
        hypothesis_id: Slug grouping all trials of the hypothesis (the
            multiple-testing ledger counts across runs by this id).
        hypothesis: The hypothesis in words.
        instrument_id: ``VENUE:SYMBOL``.
        interval_ns: Bar interval; required when the instrument has several.
        event_source: Source of the bars; required when there are several.
        train, validation, test: The periods.
        signal: The configurations to try.
        allocation: Position sizing (BP6 section 4.9): ``fixed_quantity`` or
            ``volatility_target``.
        initial_cash: Starting cash in the quote currency.
        fee_bps, slippage_bps, fee_increment: The cost model (ADR 0032).
        validation_method: Walk-forward or purged k-fold.
        dsr_threshold: Minimum Deflated Sharpe Ratio (default 0.95).
        pbo_groups: Row groups of CSCV (even; the paper uses 16).
        perturbation_drop: Share of bars dropped in the data-perturbation sweep.
        seed: Seed of every random choice.
    """

    spec_version: Literal[2]
    hypothesis_id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    hypothesis: str = Field(min_length=1)
    instrument_id: str = Field(pattern=r"^[A-Z0-9][A-Z0-9_]*:[^\s:]+$")
    interval_ns: PositiveInt | None = None
    event_source: str | None = None
    train: PeriodSpec
    validation: PeriodSpec
    test: PeriodSpec
    signal: SignalGrid
    allocation: AllocationSpec
    initial_cash: Annotated[Decimal, Field(gt=0)]
    fee_bps: Annotated[Decimal, Field(ge=0)]
    slippage_bps: Annotated[Decimal, Field(ge=0)]
    fee_increment: Annotated[Decimal, Field(gt=0)]
    validation_method: WalkForward | PurgedKFold = Field(discriminator="method")
    dsr_threshold: Annotated[Decimal, Field(gt=0, lt=1)] = Decimal("0.95")
    pbo_groups: Annotated[int, Field(ge=2)] = 16
    perturbation_drop: Annotated[Decimal, Field(gt=0, lt=1)] = Decimal("0.01")
    seed: NonNegativeInt = 0

    @model_validator(mode="before")
    @classmethod
    def _upgrade(cls, data: Any) -> Any:  # noqa: ANN401 - raw input, as pydantic passes it
        """Upgrade a document to the current version; one without ``spec_version`` is version 1."""
        if not isinstance(data, dict):
            return data
        document = data if SPEC_VERSION_KEY in data else {**data, SPEC_VERSION_KEY: 1}
        try:
            return SPEC_MIGRATIONS.upgrade(document)
        except UnsupportedVersionError as error:
            raise ValueError(str(error)) from error

    @model_validator(mode="after")
    def _periods(self) -> ExperimentSpec:
        if self.train.end > self.validation.start or self.validation.end > self.test.start:
            raise ValueError("periods must be ordered and disjoint: train < validation < test")
        if self.pbo_groups % 2:
            raise ValueError("pbo_groups must be even")
        return self

    def periods(self) -> tuple[Period, Period, Period]:
        """The three periods as recorded."""
        return (
            self.train.period(PeriodName.TRAIN),
            self.validation.period(PeriodName.VALIDATION),
            self.test.period(PeriodName.TEST),
        )
