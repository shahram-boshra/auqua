# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Command line of research runs: ``python -m auqua.analytics`` (BP5 section 6).

``experiment SPEC`` runs train + validation and writes the Research Integrity
Report next to the run log; ``test SPEC --fast F --slow S`` evaluates one
configuration on the test period (counted). The terminal shows only the run
id, the number of input records the run read (``--input-records`` pins a
rerun to it), the report's SHA-256 and each check's status, never figures derived from
the data: some providers' terms prohibit sharing works derived from their
data (ADR 0027), and terminal output is often pasted elsewhere. The full
report stays in the run directory.
"""

import argparse
import hashlib
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from auqua.analytics.engine import EngineError
from auqua.analytics.experiment import ExperimentError, evaluate_test, run_experiment
from auqua.analytics.inputs import InputError
from auqua.analytics.spec import ExperimentSpec
from auqua.config.base import Environment
from auqua.config.errors import ConfigError
from auqua.config.loader import load
from auqua.config.tomlfile import read_toml
from auqua.contracts.identity import InstrumentId
from auqua.contracts.instruments import AnyInstrument
from auqua.ingestion.errors import IngestionError
from auqua.portfolio.errors import PortfolioError
from auqua.risk.gate import TradingLimits
from auqua.storage.errors import StorageError
from auqua.storage.reference import ReferenceIndex
from auqua.storage.replay import EventLogReader

LOG_FILE = "event_log.sqlite3"


class CliError(Exception):
    """The command cannot run (missing instrument, invalid specification)."""


def _spec(path: Path) -> ExperimentSpec:
    document, _ = read_toml(path)
    try:
        return ExperimentSpec.model_validate(document)
    except ValueError as error:
        raise CliError(f"{path}: {error}") from error


def _instrument(
    reader: EventLogReader, spec: ExperimentSpec, input_records: int | None
) -> AnyInstrument:
    """The instrument version in effect at the train start, as known to the input snapshot."""
    records = list(reader.records())[:input_records]
    known = max((r.event.ts_ingest_ns for r in records), default=0)
    index = ReferenceIndex.from_records(records)
    train = spec.periods()[0]
    instrument = index.as_of(InstrumentId.parse(spec.instrument_id), train.start_ns, known)
    if instrument is None:
        raise CliError(f"no instrument definition of {spec.instrument_id} in the input log")
    return instrument


def _print_report(path: Path, root: Path, report: dict[str, Any]) -> None:
    lineage = report["lineage"]
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    name = path.relative_to(root).as_posix()
    sys.stdout.write(f"run_id={lineage['run_id']} input_records={lineage['input_records']}\n")
    sys.stdout.write(f"report={name} sha256={digest}\n")
    for check in report["checks"]:
        sys.stdout.write(f"{check['status']:5} {check['check']}\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m auqua.analytics", description=__doc__)
    parser.add_argument("--config-root", type=Path, required=True)
    parser.add_argument("--secrets-root", type=Path, required=True)
    parser.add_argument("--environment", type=Environment, required=True)
    parser.add_argument(
        "--input-records",
        type=int,
        help="pin the input to the first N log records (an earlier run's input_records)",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    experiment = commands.add_parser("experiment", help="train + validation, integrity report")
    experiment.add_argument("spec", type=Path)
    test = commands.add_parser("test", help="evaluate one configuration on the test period")
    test.add_argument("spec", type=Path)
    test.add_argument("--fast", type=int, required=True)
    test.add_argument("--slow", type=int, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command; return the process exit status."""
    args = _parser().parse_args(argv)
    try:
        loaded = load(
            args.environment,
            args.config_root,
            args.secrets_root,
            limits_model=TradingLimits,
            secret_names=(),
            environ=os.environ,
        )
        spec = _spec(args.spec)
        path = loaded.config.paths.data_root / LOG_FILE
        timeout = loaded.config.storage.busy_timeout_ms
        root = loaded.config.paths.output_root
        with EventLogReader(path, args.environment, busy_timeout_ms=timeout) as reader:
            pin = args.input_records
            instrument = _instrument(reader, spec, pin)
            if args.command == "experiment":
                result = run_experiment(reader, instrument, spec, loaded, input_records=pin)
                _print_report(result.report_json, root, result.report)
            else:
                configuration = (args.fast, args.slow)
                done = evaluate_test(
                    reader, instrument, spec, loaded, configuration=configuration, input_records=pin
                )
                _print_report(done.report_json, root, done.report)
    except (ConfigError, StorageError, OSError, CliError, IngestionError, InputError,
            ExperimentError, EngineError, PortfolioError) as error:  # fmt: skip
        sys.stderr.write(f"error: {type(error).__name__}: {error}\n")
        return 1
    return 0
