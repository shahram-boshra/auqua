# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""The ingestion run (BP4 sections 4.3 and 4.5; ADR 0026).

1. Permit: the source is enabled with the current terms, and its profile allows
   the purpose of the environment (:mod:`auqua.ingestion.sources`).
2. Run key (profile, raw SHA-256, parser id): a completed run with that key
   makes this run a recorded no-op (``already_ingested``).
3. Parse; a provider checksum mismatch or a parse error is recorded as a
   ``rejected`` run and raised.
4. Revisions: a record whose id is stored with the same content is a
   duplicate; with different content it is stored as the next revision under
   a new id, so nothing is overwritten and the log never sees a conflict.
5. Append in batches of ``batch_events``; append the :class:`IngestRun` last.
   A crash before it leaves complete batches; the re-run finds them as
   duplicates and completes the run.

The wall clock is read once for the records of a run and once for its run
record (ADR 0009: only at the ingestion boundary).
"""

import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass

import msgspec

from auqua.clock.base import Clock
from auqua.config.base import Environment
from auqua.config.ingestion import IngestionConfig
from auqua.contracts.events.base import Event
from auqua.contracts.events.ingest import (
    AvailabilityBasis,
    ChecksumResult,
    IngestRun,
    RunOutcome,
)
from auqua.contracts.events.market import Bar, ReferenceRate
from auqua.contracts.identity import derived_id, new_id
from auqua.ingestion.errors import ChecksumError, IngestionError
from auqua.ingestion.parsers import ParseContext, ParseResult, ParserSpec
from auqua.ingestion.raw import RawFile
from auqua.ingestion.sources import ENVIRONMENT_PURPOSE, SourceProfile, acknowledged, permit
from auqua.storage.eventlog import EventLog
from auqua.storage.replay import EventLogReader

RUN_SOURCE = "ingest/runner"
#: Namespace of completed-run ids (name-based on the run key).
RUN_NAMESPACE = derived_id(uuid.NAMESPACE_URL, b"https://github.com/shahram-boshra/auqua#run")
#: Namespace of revision ids (name-based on the revision-0 id and the revision).
REVISION_NAMESPACE = derived_id(
    uuid.NAMESPACE_URL, b"https://github.com/shahram-boshra/auqua#revision"
)

_MESSAGE_CHARS = 500


@dataclass(frozen=True)
class RawInput:
    """A raw object about to be ingested and how it was obtained.

    Attributes:
        raw: The stored object.
        data: Its bytes as given to the parser (an archive's member, else the object).
        retrieval: URL or local path it came from.
        checksum: Result of the provider checksum check.
    """

    raw: RawFile
    data: bytes
    retrieval: str
    checksum: ChecksumResult


def run_id(profile: str, raw_sha256: str, parser_id: str) -> uuid.UUID:
    """Id of the completed run with this run key."""
    return derived_id(RUN_NAMESPACE, profile.encode(), raw_sha256.encode(), parser_id.encode())


def revision_id(base: uuid.UUID, revision: int) -> uuid.UUID:
    """Id of ``revision`` of the record whose revision 0 has id ``base``."""
    if revision == 0:
        return base
    return derived_id(REVISION_NAMESPACE, base.bytes, revision.to_bytes(4, "big"))


def _same_content(stored: Event, new: Event) -> bool:
    changes: dict[str, object] = {"id": new.id, "ts_ingest_ns": new.ts_ingest_ns}
    if isinstance(stored, Bar | ReferenceRate) and isinstance(new, Bar | ReferenceRate):
        changes["revision"] = new.revision
    return bool(msgspec.structs.replace(stored, **changes) == new)


@dataclass
class _Counts:
    appended: int = 0
    duplicates: int = 0
    revisions: int = 0


class IngestRunner:
    """Runs ingestion into one environment's event log."""

    def __init__(
        self,
        log: EventLog,
        reader: EventLogReader,
        config: IngestionConfig,
        environment: Environment,
        clock: Clock,
    ) -> None:
        """Ingest into ``log`` (written) through ``reader`` (lookups) for ``environment``."""
        self._log = log
        self._reader = reader
        self._config = config
        self._environment = environment
        self._clock = clock

    def run(
        self,
        profile: SourceProfile,
        parser: ParserSpec,
        raw: RawInput,
        context: Callable[[int], ParseContext],
    ) -> IngestRun:
        """Ingest one raw object; return the appended :class:`IngestRun`.

        ``context`` builds the parse context from the run's ingestion time.

        Raises:
            TermsNotAcknowledgedError, PermitError: before anything is written.
            ChecksumError, ParseError: after a ``rejected`` run is recorded.
        """
        declaration = acknowledged(profile, self._config)
        permit(profile, ENVIRONMENT_PURPOSE[self._environment])
        ingest_ns = self._clock.now_ns()
        parser_id = parser.parser_id
        completed = run_id(profile.name, raw.raw.sha256, parser_id)

        def record(
            outcome: RunOutcome,
            result: ParseResult | None = None,
            counts: _Counts | None = None,
            message: str | None = None,
        ) -> IngestRun:
            counts = counts or _Counts()
            run = IngestRun(
                id=completed if outcome is RunOutcome.COMPLETED else new_id(),
                source=RUN_SOURCE,
                ts_event_ns=ingest_ns,
                ts_ingest_ns=max(ingest_ns, self._clock.now_ns()),
                profile=profile.name,
                terms=profile.terms,
                use_class=declaration.use_class,
                jurisdiction=self._config.jurisdiction,
                raw_sha256=raw.raw.sha256,
                raw_size=raw.raw.size,
                retrieval=raw.retrieval,
                checksum=raw.checksum,
                parser=parser.name,
                parser_version=parser.version,
                parser_id=parser_id,
                outcome=outcome,
                availability=result.availability if result else AvailabilityBasis.UNKNOWN,
                appended=counts.appended,
                duplicates=counts.duplicates,
                revisions=counts.revisions,
                gaps=len(result.gaps) if result else 0,
                unavailable=result.unavailable if result else 0,
                message=message,
            )
            self._log.append([run])
            return run

        if self._reader.find(RUN_SOURCE, completed) is not None:
            return record(RunOutcome.ALREADY_INGESTED)
        if raw.checksum is ChecksumResult.MISMATCH:
            record(RunOutcome.REJECTED, message="provider checksum mismatch")
            raise ChecksumError(f"{profile.name}: {raw.raw.sha256} fails the provider checksum")
        try:
            result = parser.parse(raw.data, context(ingest_ns))
        except IngestionError as error:
            record(RunOutcome.REJECTED, message=str(error)[:_MESSAGE_CHARS])
            raise
        counts = _Counts()
        pending = self._resolve([*result.events, *result.gaps], counts)
        for batch in _batches(pending, self._config.batch_events):
            appended = self._log.append(batch)
            counts.appended += appended.appended
            counts.duplicates += appended.duplicates
        notes = "; ".join(result.notes)[:_MESSAGE_CHARS] or None
        return record(RunOutcome.COMPLETED, result, counts, notes)

    def _resolve(self, events: Sequence[Event], counts: _Counts) -> Iterator[Event]:
        """The events to append: new records and new revisions; duplicates counted."""
        for event in events:
            if not isinstance(event, Bar | ReferenceRate):
                if self._reader.find(event.source, event.id) is None:
                    yield event
                else:
                    counts.duplicates += 1
                continue
            latest: Event | None = None
            revision = 0
            while found := self._reader.find(event.source, revision_id(event.id, revision)):
                latest = found.event
                revision += 1
            if latest is None:
                yield event
            elif _same_content(latest, event):
                counts.duplicates += 1
            else:
                counts.revisions += 1
                yield msgspec.structs.replace(
                    event, id=revision_id(event.id, revision), revision=revision
                )


def _batches(events: Iterator[Event], size: int) -> Iterator[list[Event]]:
    batch: list[Event] = []
    for event in events:
        batch.append(event)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
