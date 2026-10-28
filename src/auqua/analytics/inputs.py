# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Point-in-time input snapshot of a run (BP5 sections 3 and 4, R-2; ADR 0030).

A run reads its input log once, up to the head at the start (or up to a
pinned earlier head, ``input_records``, to reproduce an earlier run), and builds the
:class:`~auqua.storage.marketview.MarketView` pinned to the snapshot's
knowledge time (the latest ingestion time in it), so a correction ingested
later is invisible to the run and a rerun reproduces it. Every input source
must have a source profile, be cleared for the operator's jurisdiction and
permit backtests (ADRs 0027-0029); the snapshot records, per source, its
terms, use class, jurisdiction and availability basis as the ingestion runs
recorded them, and a digest of their raw files.
"""

import hashlib
from collections import Counter, defaultdict
from dataclasses import dataclass

from auqua.config.ingestion import IngestionConfig
from auqua.contracts.events.base import Event, LogRecord
from auqua.contracts.events.ingest import DataGap, IngestRun, RunOutcome
from auqua.contracts.events.market import Bar
from auqua.contracts.events.research import InputRef
from auqua.contracts.identity import InstrumentId
from auqua.ingestion.sources import (
    PROFILES_BY_EVENT_SOURCE,
    Purpose,
    SourceProfile,
    require_usable,
)
from auqua.storage.eventlog import Head
from auqua.storage.marketview import MarketView
from auqua.storage.replay import EventLogReader

UNRECORDED = "unrecorded"


class InputError(ValueError):
    """The input log does not hold a single, usable bar series for the request."""


@dataclass(frozen=True)
class InputSnapshot:
    """The bars a run may read and their lineage.

    Attributes:
        head: Input log head at the start of the run.
        knowledge_ns: Latest ingestion time in the snapshot.
        interval_ns: Bar interval of the series.
        bars: Revision-resolved bars, in availability order.
        inputs: One reference per input source.
        profiles: The source profiles of the inputs.
        gaps: Recorded gaps of the series.
    """

    head: Head
    knowledge_ns: int
    interval_ns: int
    bars: tuple[Bar, ...]
    inputs: tuple[InputRef, ...]
    profiles: tuple[SourceProfile, ...]
    gaps: tuple[DataGap, ...]


def _records(reader: EventLogReader, head: Head) -> list[LogRecord[Event]]:
    out = []
    for record in reader.records():
        if record.sequence >= head.next_sequence:
            break
        out.append(record)
    return out


def _head(reader: EventLogReader, input_records: int | None) -> Head:
    head = reader.head()
    if input_records is None:
        return head
    if not 0 < input_records <= head.next_sequence:
        raise InputError(f"input_records {input_records} outside 1..{head.next_sequence}")
    return Head(input_records, reader.hash_at(input_records - 1))


def _input_ref(profile: SourceProfile, runs: list[IngestRun], bars: int, gaps: int) -> InputRef:
    latest = runs[-1] if runs else None
    digest = hashlib.sha256("\n".join(sorted(r.raw_sha256 for r in runs)).encode()).hexdigest()
    return InputRef(
        event_source=profile.event_source,
        profile=profile.name,
        terms=latest.terms if latest else profile.terms,
        use_class=latest.use_class.value if latest else UNRECORDED,
        jurisdiction=(latest.jurisdiction or UNRECORDED) if latest else UNRECORDED,
        availability=latest.availability.value if latest else UNRECORDED,
        runs=len(runs),
        runs_sha256=digest,
        bars=bars,
        gaps=gaps,
    )


def snapshot(  # noqa: PLR0913 - every argument narrows the series
    reader: EventLogReader,
    instrument_id: InstrumentId,
    config: IngestionConfig,
    *,
    interval_ns: int | None = None,
    event_source: str | None = None,
    purpose: Purpose = Purpose.BACKTEST,
    input_records: int | None = None,
) -> InputSnapshot:
    """The point-in-time snapshot of one bar series of ``instrument_id``.

    ``input_records`` pins the snapshot to the first records of the log (the
    ``input_next_sequence`` of an earlier run); records appended later, such
    as corrections, are invisible to it.

    Raises:
        InputError: no bars, several intervals without ``interval_ns``, several
            sources without ``event_source``, a source without a profile, or
            ``input_records`` outside the log.
        NotEligibleError, PermitError: an input not usable for ``purpose``.
    """
    head = _head(reader, input_records)
    records = _records(reader, head)
    if not records:
        raise InputError("the input log is empty")
    knowledge_ns = max(r.event.ts_ingest_ns for r in records)
    view = MarketView(records)
    intervals = sorted(i for key, i in view.bar_series() if key == instrument_id)
    if interval_ns is None:
        if len(intervals) != 1:
            raise InputError(f"{instrument_id}: intervals {intervals}; name one")
        interval_ns = intervals[0]
    elif interval_ns not in intervals:
        raise InputError(f"{instrument_id}: no bars of interval {interval_ns}")
    bars = view.bars(instrument_id, interval_ns, known_ns=knowledge_ns)
    sources = sorted({bar.source for bar in bars})
    if event_source is not None:
        bars = [bar for bar in bars if bar.source == event_source]
        if not bars:
            raise InputError(f"{instrument_id}: no bars from {event_source}")
    elif len(sources) > 1:
        raise InputError(f"{instrument_id}: bars from {sources}; name one source")
    used = sorted({bar.source for bar in bars})
    profiles = []
    for source in used:
        profile = PROFILES_BY_EVENT_SOURCE.get(source)
        if profile is None:
            raise InputError(f"bars from {source!r} have no source profile")
        require_usable(profile, config, purpose)
        profiles.append(profile)
    runs: dict[str, list[IngestRun]] = defaultdict(list)
    gaps: list[DataGap] = []
    for record in records:
        event = record.event
        if isinstance(event, IngestRun) and event.outcome is RunOutcome.COMPLETED:
            runs[event.profile].append(event)
        elif (
            isinstance(event, DataGap)
            and event.key == str(instrument_id)
            and event.interval_ns == interval_ns
            and event.source in used
        ):
            gaps.append(event)
    per_source = Counter(bar.source for bar in bars)
    gap_count = Counter(gap.source for gap in gaps)
    inputs = tuple(
        _input_ref(p, runs[p.name], per_source[p.event_source], gap_count[p.event_source])
        for p in profiles
    )
    return InputSnapshot(
        head, knowledge_ns, interval_ns, tuple(bars), inputs, tuple(profiles), tuple(gaps)
    )
