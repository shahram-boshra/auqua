# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Research events (BP5 sections 4 and 4.2; ADRs 0030 and 0031).

:class:`RunStartedV2` is the experiment record (Strategy Module 3): what a run
read (input snapshot, every input's source profile, use class, jurisdiction and
availability basis), how it was configured (hypothesis, spec, periods, seed),
and which code and environment ran. Runs recorded as ``research.run.v1`` (the
walking skeleton) are upcast; their BP5 fields are ``None`` / empty, meaning
"not recorded". :class:`Trial` is one evaluation of one configuration on one
period, the unit of the multiple-testing ledger. Statistics are estimates and
are stored as decimal text of their float value, never used as trading values.
"""

import re
import uuid
from decimal import Decimal
from enum import StrEnum

import msgspec

from auqua.contracts.errors import InvalidFieldError
from auqua.contracts.events.base import Event
from auqua.contracts.events.trading import RunStarted
from auqua.contracts.identity import InstrumentId
from auqua.contracts.validation import require_match, require_time_ns

_SHA256_HEX = 64
#: Hypothesis ids: lower-case slugs, e.g. ``ma-crossover-btc``.
_SLUG_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def _require_sha(value: str, name: str) -> None:
    if len(value) != _SHA256_HEX or value.strip("0123456789abcdef"):
        raise InvalidFieldError(f"{name} must be 64 lower-case hex digits")


class PeriodName(StrEnum):
    """Role of a period in the experiment (BP5 section 4.1)."""

    TRAIN = "train"
    VALIDATION = "validation"
    TEST = "test"


class Period(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """A half-open UTC interval ``[start_ns, end_ns)`` with its role."""

    name: PeriodName
    start_ns: int
    end_ns: int

    def __post_init__(self) -> None:
        """Validate the interval."""
        require_time_ns(self.start_ns, "start_ns")
        if self.end_ns <= self.start_ns:
            raise InvalidFieldError(f"{self.name} period must end after it starts")


class InputRef(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """One input source of a run and its terms (ADRs 0027-0029).

    ``runs_sha256`` hashes the sorted raw SHA-256s of the source's completed
    ingestion runs in the snapshot; ``bars`` counts the bars used.
    """

    event_source: str
    profile: str
    terms: str
    use_class: str
    jurisdiction: str
    availability: str
    runs: int
    runs_sha256: str
    bars: int
    gaps: int

    def __post_init__(self) -> None:
        """Validate the digest and the counts."""
        _require_sha(self.runs_sha256, "runs_sha256")
        if min(self.runs, self.bars, self.gaps) < 0:
            raise InvalidFieldError("counts must be >= 0")


class RunStartedV2(Event, frozen=True, tag="research.run.v2"):
    """Experiment record: the identity of one research or backtest run.

    The event ``id`` is the run id. ``knowledge_ns`` is the snapshot's
    knowledge time (the latest ingestion time in the input log).
    """

    input_head_hash: str
    input_next_sequence: int
    instrument_id: InstrumentId
    instrument_sha256: str
    config_id: str
    spec_id: str
    risk_limits_sha256: str
    code_id: str
    code_version: str
    hypothesis_id: str | None = None
    spec_version: int | None = None
    interval_ns: int | None = None
    knowledge_ns: int | None = None
    periods: tuple[Period, ...] = ()
    inputs: tuple[InputRef, ...] = ()
    uv_lock_sha256: str | None = None
    python_version: str | None = None
    seed: int | None = None

    def __post_init__(self) -> None:
        """Validate the hashes, the hypothesis id and the input position."""
        super().__post_init__()
        for name in (
            "input_head_hash",
            "instrument_sha256",
            "config_id",
            "spec_id",
            "risk_limits_sha256",
            "code_id",
        ):
            _require_sha(getattr(self, name), name)
        if self.uv_lock_sha256 is not None:
            _require_sha(self.uv_lock_sha256, "uv_lock_sha256")
        if self.hypothesis_id is not None:
            require_match(self.hypothesis_id, _SLUG_PATTERN, "hypothesis_id")
        if self.input_next_sequence < 0:
            raise InvalidFieldError("input_next_sequence must be >= 0")


def upcast_run_started(event: RunStarted) -> RunStartedV2:
    """``research.run.v1`` → v2: the same fields; BP5 fields not recorded."""
    return RunStartedV2(
        id=event.id,
        source=event.source,
        ts_event_ns=event.ts_event_ns,
        ts_ingest_ns=event.ts_ingest_ns,
        input_head_hash=event.input_head_hash,
        input_next_sequence=event.input_next_sequence,
        instrument_id=event.instrument_id,
        instrument_sha256=event.instrument_sha256,
        config_id=event.config_id,
        spec_id=event.spec_id,
        risk_limits_sha256=event.risk_limits_sha256,
        code_id=event.code_id,
        code_version=event.code_version,
    )


class Trial(Event, frozen=True, tag="research.trial.v1"):
    """One configuration evaluated on one period of one hypothesis.

    ``config_hash`` identifies the configuration (signal parameters and cost
    model); ``returns_sha256`` the per-period return series as text; ``sharpe``
    is the per-period (not annualised) Sharpe ratio.
    """

    hypothesis_id: str
    run_id: uuid.UUID
    config_hash: str
    period: PeriodName
    observations: int
    sharpe: Decimal
    returns_sha256: str

    def __post_init__(self) -> None:
        """Validate the ids, hashes and counts."""
        super().__post_init__()
        require_match(self.hypothesis_id, _SLUG_PATTERN, "hypothesis_id")
        _require_sha(self.config_hash, "config_hash")
        _require_sha(self.returns_sha256, "returns_sha256")
        if self.observations < 2:  # noqa: PLR2004 - a Sharpe ratio needs two returns
            raise InvalidFieldError("observations must be >= 2")
        if not self.sharpe.is_finite():
            raise InvalidFieldError("sharpe must be finite")
