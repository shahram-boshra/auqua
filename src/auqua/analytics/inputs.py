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

The snapshot also carries what the run's account reads (BP6 amendment 12,
F-AG): a :class:`~auqua.storage.marketview.MarketView` of the selected bar
series only — plus, when the run needs USD rates, the rate series or traded
pairs named for those currencies, whose sources pass the same checks and are
recorded among the inputs — and the instrument index of the snapshot.
"""

import hashlib
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass

from auqua.config.ingestion import IngestionConfig
from auqua.config.portfolio import RateSource
from auqua.contracts.events.base import Event, LogRecord
from auqua.contracts.events.ingest import DataGap, IngestRun, RunOutcome
from auqua.contracts.events.market import Bar, ReferenceRate
from auqua.contracts.events.research import InputRef
from auqua.contracts.identity import InstrumentId, SeriesId
from auqua.ingestion.sources import (
    PROFILES_BY_EVENT_SOURCE,
    Purpose,
    SourceProfile,
    require_usable,
)
from auqua.storage.eventlog import Head
from auqua.storage.marketview import MarketView
from auqua.storage.reference import ReferenceIndex
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
        market: The selected series and the requested rates, for the run's account.
        reference: The instrument versions of the snapshot.
    """

    head: Head
    knowledge_ns: int
    interval_ns: int
    bars: tuple[Bar, ...]
    inputs: tuple[InputRef, ...]
    profiles: tuple[SourceProfile, ...]
    gaps: tuple[DataGap, ...]
    market: MarketView
    reference: ReferenceIndex


def _profiles(sources: list[str], config: IngestionConfig, purpose: Purpose) -> list[SourceProfile]:
    """The profile of each source, each usable for ``purpose`` (ADRs 0027-0029)."""
    profiles = []
    for source in sources:
        profile = PROFILES_BY_EVENT_SOURCE.get(source)
        if profile is None:
            raise InputError(f"records from {source!r} have no source profile")
        require_usable(profile, config, purpose)
        profiles.append(profile)
    return profiles


def _rate_records(
    records: list[LogRecord[Event]], rates: Mapping[str, RateSource], traded: InstrumentId
) -> list[LogRecord[Event]]:
    """Records of the rate series and traded pairs named in ``rates`` (not the traded series)."""
    series = {SeriesId.parse(r.series) for r in rates.values() if r.series is not None}
    pairs = {InstrumentId.parse(r.instrument) for r in rates.values() if r.instrument is not None}
    pairs.discard(traded)
    out = []
    for record in records:
        event = record.event
        if (isinstance(event, ReferenceRate) and event.series_id in series) or (
            isinstance(event, Bar) and event.instrument_id in pairs
        ):
            out.append(record)
    return out


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


def _input_ref(
    profile: SourceProfile, runs: list[IngestRun], bars: int, gaps: int, rates: int | None
) -> InputRef:
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
        rates=rates,
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
    rates: Mapping[str, RateSource] | None = None,
) -> InputSnapshot:
    """The point-in-time snapshot of one bar series of ``instrument_id``.

    ``input_records`` pins the snapshot to the first records of the log (the
    ``input_next_sequence`` of an earlier run); records appended later, such
    as corrections, are invisible to it. ``rates`` names the USD rate sources
    the run reads (currency → source); their records join the run's market
    view and their sources the inputs.

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
    selected = [
        record
        for record in records
        if isinstance(record.event, Bar)
        and record.event.instrument_id == instrument_id
        and record.event.interval_ns == interval_ns
        and record.event.source in used
    ]
    rate_records = _rate_records(records, rates or {}, instrument_id)
    rate_sources = sorted({r.event.source for r in rate_records} - set(used))
    profiles = _profiles([*used, *rate_sources], config, purpose)
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
    gap_count = Counter(gap.source for gap in gaps)
    # Every series counts the records of its own kind it supplies, resolved at the
    # knowledge time (amendment 13): bars, and the rate observations of a rate source.
    rate_view = MarketView(rate_records)
    per_source = Counter(bar.source for bar in bars)
    for listed, interval in rate_view.bar_series():
        per_source.update(b.source for b in rate_view.bars(listed, interval, known_ns=knowledge_ns))
    rate_count: Counter[str] = Counter()
    for series in rate_view.rate_series():
        rate_count.update(o.source for o in rate_view.rates(series, known_ns=knowledge_ns))
    rate_suppliers = {r.event.source for r in rate_records if isinstance(r.event, ReferenceRate)}
    inputs = tuple(
        _input_ref(
            p,
            runs[p.name],
            per_source[p.event_source],
            gap_count[p.event_source],
            rate_count[p.event_source] if p.event_source in rate_suppliers else None,
        )
        for p in profiles
    )
    return InputSnapshot(
        head,
        knowledge_ns,
        interval_ns,
        tuple(bars),
        inputs,
        tuple(profiles),
        tuple(gaps),
        MarketView([*selected, *rate_records]),
        ReferenceIndex.from_records(records),
    )
