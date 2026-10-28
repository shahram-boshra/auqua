# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Multiple-testing ledger (BP5 section 4.2; ADR 0031).

Every configuration evaluated for a hypothesis is a :class:`Trial` in one
ledger per environment, ``<output_root>/research/ledger.sqlite3`` (an event
log: append-only, hash-chained, single writer). The ledger spans runs, so the
number of trials N behind a selected result cannot be reduced by splitting
the search into several runs. Trial ids are name-based (run, configuration,
period): rerunning the same experiment adds nothing.
"""

import hashlib
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import msgspec

from auqua.config.base import Environment
from auqua.contracts.events.research import PeriodName, Trial
from auqua.contracts.identity import derived_id
from auqua.storage.eventlog import EventLog
from auqua.storage.replay import EventLogReader

LEDGER = Path("research") / "ledger.sqlite3"
SOURCE = "research/ledger"
TRIAL_NAMESPACE = derived_id(uuid.NAMESPACE_URL, b"https://github.com/shahram-boshra/auqua#trial")


def config_hash(configuration: dict[str, object]) -> str:
    """SHA-256 of a configuration's canonical JSON (sorted keys; decimals as text)."""
    return hashlib.sha256(msgspec.json.encode(configuration, order="sorted")).hexdigest()


def returns_hash(values: list[float]) -> str:
    """SHA-256 of a return series written as ``repr`` text, one value per line."""
    return hashlib.sha256("\n".join(repr(v) for v in values).encode()).hexdigest()


def trial_id(run_id: uuid.UUID, configuration: str, period: PeriodName) -> uuid.UUID:
    """Name-based id of the trial of ``configuration`` on ``period`` in ``run_id``."""
    return derived_id(TRIAL_NAMESPACE, run_id.bytes, configuration.encode(), period.encode())


@dataclass(frozen=True)
class LedgerView:
    """Trials of one hypothesis as recorded in the ledger."""

    trials: tuple[Trial, ...]

    def configurations(self) -> int:
        """N: distinct configurations evaluated on the validation period."""
        return len({t.config_hash for t in self.trials if t.period is PeriodName.VALIDATION})

    def validation_sharpes(self) -> list[float]:
        """The latest validation Sharpe ratio of each configuration."""
        latest: dict[str, Trial] = {}
        for trial in self.trials:
            if trial.period is PeriodName.VALIDATION:
                latest[trial.config_hash] = trial
        return [float(t.sharpe) for t in latest.values()]

    def test_uses(self) -> int:
        """Evaluations on the test period."""
        return sum(1 for t in self.trials if t.period is PeriodName.TEST)


class TrialLedger:
    """Appends trials and reads them back per hypothesis."""

    def __init__(self, output_root: Path, environment: Environment, busy_timeout_ms: int) -> None:
        """Use the ledger under ``output_root``."""
        self._path = output_root / LEDGER
        self._environment = environment
        self._timeout = busy_timeout_ms

    def append(self, trials: list[Trial]) -> int:
        """Store ``trials``; returns how many were new."""
        with EventLog(self._path, self._environment, busy_timeout_ms=self._timeout) as log:
            return log.append(trials).appended

    def view(self, hypothesis_id: str) -> LedgerView:
        """The trials of ``hypothesis_id`` (none when the ledger does not exist yet)."""
        if not self._path.exists():
            return LedgerView(())
        with EventLogReader(self._path, self._environment, busy_timeout_ms=self._timeout) as r:
            return LedgerView(
                tuple(
                    e
                    for e in (rec.event for rec in r.records())
                    if isinstance(e, Trial) and e.hypothesis_id == hypothesis_id
                )
            )

    def hypotheses_like(self, hypothesis_id: str, configurations: set[str]) -> list[str]:
        """Other hypotheses that evaluated any of ``configurations`` (renaming is visible)."""
        if not self._path.exists():
            return []
        with EventLogReader(self._path, self._environment, busy_timeout_ms=self._timeout) as r:
            return sorted(
                {
                    e.hypothesis_id
                    for e in (rec.event for rec in r.records())
                    if isinstance(e, Trial)
                    and e.hypothesis_id != hypothesis_id
                    and e.config_hash in configurations
                }
            )


def make_trial(  # noqa: PLR0913 - every argument is a field of the trial
    *,
    hypothesis_id: str,
    run_id: uuid.UUID,
    configuration: str,
    period: PeriodName,
    values: list[float],
    sharpe: float,
    ts_ns: int,
) -> Trial:
    """A trial record; the Sharpe ratio is stored as the decimal text of its float."""
    return Trial(
        id=trial_id(run_id, configuration, period),
        source=SOURCE,
        ts_event_ns=ts_ns,
        ts_ingest_ns=ts_ns,
        hypothesis_id=hypothesis_id,
        run_id=run_id,
        config_hash=configuration,
        period=period,
        observations=len(values),
        sharpe=Decimal(repr(sharpe)),
        returns_sha256=returns_hash(values),
    )
