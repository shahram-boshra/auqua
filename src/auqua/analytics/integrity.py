# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Research Integrity Report (BP5 section 4, R-7; ADR 0032).

A JSON document per run, rendered to static HTML by
:mod:`auqua.visualization.integrity`. It separates the hypothesis, the
configuration, the input data and their terms, the assumptions and the
evidence (Strategy Module 3), and lists every check as ``pass``, ``warn`` or
``fail`` with the value it rests on. Outputs carry the strictest sharing rule
and the attributions of their inputs (ADR 0027): when any input prohibits
sharing, the report says "not for redistribution". The JSON is written with
sorted keys, so the same run gives the same bytes, and is validated against
the report schema (:data:`Report`, exported by :func:`report_schema`) before
it is written.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import msgspec

from auqua.analytics.inputs import InputSnapshot
from auqua.analytics.spec import ExperimentSpec
from auqua.contracts.events.research import Period, RunStartedV2
from auqua.contracts.instruments import AnyInstrument
from auqua.ingestion.sources import Sharing, SourceProfile, attributions, strictest_sharing
from auqua.storage.files import write_private
from auqua.visualization.integrity import render

PASS, WARN, FAIL = "pass", "warn", "fail"
NOT_FOR_REDISTRIBUTION = (
    "Not for redistribution: an input's terms prohibit sharing data or works derived from it."
)
_DECLARED_BASES = {"declared", "unknown", "unrecorded"}


class ReportError(ValueError):
    """A report does not match the report schema."""


class _Strict(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Closed record: an unknown field is an error."""


class Check(_Strict, frozen=True):
    """One integrity check and the value it rests on."""

    check: str
    status: Literal["pass", "warn", "fail"]
    value: Any
    note: str


class Terms(_Strict, frozen=True):
    """Sharing rule and attributions of the inputs (ADR 0027)."""

    sharing: str
    attributions: list[str]
    notice: str | None
    machine_learning: dict[str, str]


class Lineage(_Strict, frozen=True):
    """What makes the run reproducible."""

    run_id: str
    input_head_hash: str
    input_records: int
    knowledge_ns: int | None
    instrument_id: str
    interval_ns: int | None
    instrument_sha256: str
    config_id: str
    spec_id: str
    risk_limits_sha256: str
    code_id: str
    code_version: str
    python_version: str | None
    uv_lock_sha256: str | None
    seed: int | None


class ExperimentReport(_Strict, frozen=True, tag="experiment", tag_field="kind"):
    """Report of :func:`auqua.analytics.experiment.run_experiment`."""

    title: str
    hypothesis: dict[str, str]
    terms: Terms
    lineage: Lineage
    inputs: list[dict[str, Any]]
    assumptions: dict[str, str]
    checks: list[Check]
    selected: dict[str, Any]
    ranking: list[dict[str, Any]]
    pbo: dict[str, Any]
    selection_validation: dict[str, Any]


class TestReport(_Strict, frozen=True, tag="test evaluation", tag_field="kind"):
    """Report of :func:`auqua.analytics.experiment.evaluate_test`."""

    title: str
    terms: Terms
    lineage: Lineage
    evaluation: dict[str, Any]
    checks: list[Check]


Report = ExperimentReport | TestReport


def report_schema() -> dict[str, Any]:
    """JSON Schema of the report documents."""
    return msgspec.json.schema(Report)


_PBO_LIMIT = 0.5


def _check(name: str, status: str, value: object, note: str) -> dict[str, Any]:
    return {"check": name, "status": status, "value": value, "note": note}


def _terms(profiles: Sequence[SourceProfile]) -> dict[str, Any]:
    sharing = strictest_sharing(list(profiles))
    return {
        "sharing": sharing.value,
        "attributions": attributions(list(profiles)),
        "notice": NOT_FOR_REDISTRIBUTION if sharing is Sharing.PROHIBITED else None,
        "machine_learning": {p.name: p.ml_use.value for p in profiles},
    }


def _lineage(started: RunStartedV2) -> dict[str, Any]:
    return {
        "run_id": str(started.id),
        "input_head_hash": started.input_head_hash,
        "input_records": started.input_next_sequence,
        "knowledge_ns": started.knowledge_ns,
        "instrument_id": str(started.instrument_id),
        "interval_ns": started.interval_ns,
        "instrument_sha256": started.instrument_sha256,
        "config_id": started.config_id,
        "spec_id": started.spec_id,
        "risk_limits_sha256": started.risk_limits_sha256,
        "code_id": started.code_id,
        "code_version": started.code_version,
        "python_version": started.python_version,
        "uv_lock_sha256": started.uv_lock_sha256,
        "seed": started.seed,
    }


@dataclass(frozen=True)
class ReportInputs:
    """Everything the experiment report is built from."""

    started: RunStartedV2
    spec: ExperimentSpec
    instrument: AnyInstrument
    inputs: InputSnapshot
    profiles: Sequence[SourceProfile]
    candidates: list[dict[str, Any]]
    selected: dict[str, Any]
    trials: int
    trial_variance: float
    test_uses: int
    pbo: dict[str, Any]
    selection_validation: dict[str, Any]
    similar_hypotheses: list[str]
    gaps_in_periods: int


def _checks(r: ReportInputs) -> list[dict[str, Any]]:
    threshold = float(r.spec.dsr_threshold)
    dsr = r.selected.get("dsr")
    bases = sorted({i.availability for i in r.inputs.inputs})
    robust = bool(r.candidates and r.candidates[0]["robust"])
    pbo = r.pbo.get("probability")
    return [
        _check(
            "point_in_time_inputs",
            PASS,
            r.inputs.knowledge_ns,
            "bars read through the point-in-time view pinned to the snapshot; each released "
            "at its availability time",
        ),
        _check(
            "inputs_permitted",
            PASS,
            [i.profile for i in r.inputs.inputs],
            "every input is cleared for the jurisdiction and permits backtests",
        ),
        _check(
            "availability_basis",
            WARN if set(bases) & _DECLARED_BASES else PASS,
            bases,
            "a declared, unknown or unrecorded availability rule is an assumption",
        ),
        _check(
            "input_gaps",
            WARN if r.gaps_in_periods else PASS,
            r.gaps_in_periods,
            "recorded gaps inside train and validation",
        ),
        _check(
            "period_separation",
            PASS,
            [[p.name.value, p.start_ns, p.end_ns] for p in r.spec.periods()],
            "train < validation < test, disjoint; the test period was not read",
        ),
        _check(
            "test_period_use",
            PASS if r.test_uses == 0 else WARN,
            r.test_uses,
            "test evaluations recorded for this hypothesis before this run",
        ),
        _check(
            "multiple_testing",
            PASS,
            {"trials": r.trials, "variance_of_trial_sharpe": r.trial_variance},
            "N counts every configuration of the hypothesis across runs (ledger)",
        ),
        _check(
            "deflated_sharpe_ratio",
            PASS if dsr is not None and dsr >= threshold else FAIL,
            dsr,
            f"selected configuration; threshold {threshold}",
        ),
        _check(
            "backtest_overfitting",
            WARN if pbo is None else (FAIL if pbo >= _PBO_LIMIT else PASS),
            pbo,
            "PBO by CSCV over this run's configurations; >= 0.5 means the in-sample choice "
            "is no better than chance out of sample",
        ),
        _check(
            "robustness",
            PASS if robust else FAIL,
            r.candidates[0]["sweeps"] if r.candidates else {},
            "costs x0.5/x2, latency +1 bar, data perturbation, validation halves, grid neighbours",
        ),
        _check(
            "execution_assumptions",
            WARN if r.spec.slippage_bps == 0 else PASS,
            {"fee_bps": str(r.spec.fee_bps), "slippage_bps": str(r.spec.slippage_bps)},
            "fills at the next bar's open; no market impact (BP8)",
        ),
        _check(
            "universe",
            WARN,
            str(r.instrument.id),
            "single instrument: survivorship and universe selection are not assessed",
        ),
        _check(
            "hypothesis_renaming",
            WARN if r.similar_hypotheses else PASS,
            r.similar_hypotheses,
            "other hypotheses that evaluated the same configurations",
        ),
    ]


def build_report(r: ReportInputs) -> dict[str, Any]:
    """The experiment report document."""
    return {
        "kind": "experiment",
        "title": f"Research Integrity Report — {r.spec.hypothesis_id}",
        "hypothesis": {"id": r.spec.hypothesis_id, "text": r.spec.hypothesis},
        "terms": _terms(r.profiles),
        "lineage": _lineage(r.started),
        "inputs": [
            {
                "profile": i.profile,
                "event_source": i.event_source,
                "terms": i.terms,
                "use_class": i.use_class,
                "jurisdiction": i.jurisdiction,
                "availability": i.availability,
                "ingestion_runs": i.runs,
                "runs_sha256": i.runs_sha256,
                "bars": i.bars,
                "gaps": i.gaps,
            }
            for i in r.inputs.inputs
        ],
        "assumptions": {
            "fee_bps": str(r.spec.fee_bps),
            "slippage_bps": str(r.spec.slippage_bps),
            "fill": "next bar open",
            **r.spec.allocation.assumptions(),
            "initial_cash": str(r.spec.initial_cash),
            "dsr_threshold": str(r.spec.dsr_threshold),
        },
        "checks": _checks(r),
        "selected": r.selected,
        "ranking": r.candidates,
        "pbo": r.pbo,
        "selection_validation": r.selection_validation,
    }


def build_test_report(  # noqa: PLR0913 - the evaluation's facts
    *,
    started: RunStartedV2,
    profiles: Sequence[SourceProfile],
    configuration: str,
    config_hash: str,
    test: Period,
    values: Sequence[float],
    sharpe: float,
    psr: float | None,
    uses: int,
) -> dict[str, Any]:
    """The report of one test-period evaluation."""
    return {
        "kind": "test evaluation",
        "title": f"Test evaluation — {started.hypothesis_id}",
        "terms": _terms(profiles),
        "lineage": _lineage(started),
        "evaluation": {
            "configuration": configuration,
            "config_hash": config_hash,
            "test_period": [test.start_ns, test.end_ns],
            "observations": len(values),
            "sharpe_per_bar": sharpe,
            "psr_vs_0": psr,
        },
        "checks": [
            _check(
                "test_period_use",
                PASS if uses == 1 else FAIL,
                uses,
                "the first use is out of sample; any reuse is in sample (Dwork et al. 2015)",
            )
        ],
    }


def write_report(directory: Path, stem: str, report: dict[str, Any]) -> tuple[Path, Path]:
    """Write ``<stem>.json`` (sorted keys) and ``<stem>.html`` (``0600``); return both paths.

    Raises:
        ReportError: the report does not match the report schema.
    """
    data = (json.dumps(report, indent=2, sort_keys=True) + "\n").encode()
    try:
        msgspec.json.decode(data, type=Report)
    except msgspec.ValidationError as error:
        raise ReportError(f"{stem}: {error}") from error
    json_path = directory / f"{stem}.json"
    html_path = directory / f"{stem}.html"
    write_private(json_path, data)
    write_private(html_path, render(report).encode())
    return json_path, html_path
