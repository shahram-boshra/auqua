# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Point-in-time view of ingested market data (BP4 sections 4.3 and 4.6; ADR 0028).

Derived from the event log and rebuildable from it, like the reference index
(BP3 section 4.5). Two times apply:

- knowledge time ``known_ns`` selects the revision: per record key, the
  highest revision whose ``ts_ingest_ns <= known_ns``; a run pinned to its
  input snapshot never sees a correction ingested later;
- simulated time ``t`` releases a record only when its availability time
  (``ts_published_ns`` when stated, else ``ts_event_ns``) is ``<= t``
  (:func:`released`).

The key of a bar is (source, instrument, interval, start); of a reference
rate (source, series, observation time).

Lookups are logarithmic (BP6 amendment 12, F-AD): the view is immutable after
construction, so each series resolved at a knowledge time is kept, sorted by
availability with its availability times, and the records released at ``t``
are the prefix found by ``bisect_right``. The cache is bounded: one resolved
list per series, for the latest knowledge time asked (a backtest asks one; a
live fold, whose knowledge time advances with every record, replaces it).
"""

import bisect
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field

from auqua.contracts.events.base import Event, LogRecord
from auqua.contracts.events.market import Bar, ReferenceRate
from auqua.contracts.identity import InstrumentId, SeriesId
from auqua.storage.replay import EventLogReader

type MarketEvent = Bar | ReferenceRate
type _BarKey = tuple[str, InstrumentId, int, int]
type _RateKey = tuple[str, SeriesId, int]


def available_ns(event: MarketEvent) -> int:
    """When ``event`` became available for point-in-time use."""
    published = event.ts_published_ns
    return event.ts_event_ns if published is None else published


def released[E: MarketEvent](events: Iterable[E], t: int) -> Iterator[E]:
    """The events of ``events`` available at simulated time ``t``."""
    return (event for event in events if available_ns(event) <= t)


@dataclass
class _Versions[E: MarketEvent]:
    """All stored revisions of one record key."""

    revisions: list[E] = field(default_factory=list)

    def as_of(self, known_ns: int) -> E | None:
        best: E | None = None
        for event in self.revisions:
            if event.ts_ingest_ns <= known_ns and (best is None or event.revision > best.revision):
                best = event
        return best


@dataclass(frozen=True)
class _Resolved[E: MarketEvent]:
    """One series resolved at ``known_ns``, by availability, with its availability times."""

    known_ns: int
    events: tuple[E, ...]
    available: tuple[int, ...]

    def released(self, t: int) -> tuple[E, ...]:
        return self.events[: bisect.bisect_right(self.available, t)]


class MarketView:
    """Revision-resolved bars and reference rates, as of a knowledge time."""

    def __init__(self, records: Iterable[LogRecord[Event]]) -> None:
        """Index the bars and reference rates among ``records``."""
        self._bars: dict[tuple[InstrumentId, int], dict[_BarKey, _Versions[Bar]]] = {}
        self._rates: dict[SeriesId, dict[_RateKey, _Versions[ReferenceRate]]] = {}
        self._resolved_bars: dict[tuple[InstrumentId, int], _Resolved[Bar]] = {}
        self._resolved_rates: dict[SeriesId, _Resolved[ReferenceRate]] = {}
        for record in records:
            event = record.event
            if isinstance(event, Bar):
                group = self._bars.setdefault((event.instrument_id, event.interval_ns), {})
                key: _BarKey = (
                    event.source,
                    event.instrument_id,
                    event.interval_ns,
                    event.start_ns,
                )
                group.setdefault(key, _Versions()).revisions.append(event)
            elif isinstance(event, ReferenceRate):
                series = self._rates.setdefault(event.series_id, {})
                rate_key: _RateKey = (event.source, event.series_id, event.ts_event_ns)
                series.setdefault(rate_key, _Versions()).revisions.append(event)

    @classmethod
    def from_reader(cls, reader: EventLogReader) -> MarketView:
        """Build the view from every verified record of ``reader``."""
        return cls(reader.records())

    def bar_series(self) -> list[tuple[InstrumentId, int]]:
        """The (instrument, interval) pairs that have bars, sorted."""
        return sorted(self._bars, key=lambda key: (str(key[0]), key[1]))

    def rate_series(self) -> list[SeriesId]:
        """The reference-rate series that have observations, sorted."""
        return sorted(self._rates, key=str)

    def cached_series(self) -> int:
        """Resolved series held (at most one per series; amendment 12 bound)."""
        return len(self._resolved_bars) + len(self._resolved_rates)

    def _bar_series(
        self, instrument_id: InstrumentId, interval_ns: int, known_ns: int
    ) -> _Resolved[Bar]:
        key = (instrument_id, interval_ns)
        cached = self._resolved_bars.get(key)
        if cached is None or cached.known_ns != known_ns:
            group = self._bars.get(key, {})
            chosen = [bar for versions in group.values() if (bar := versions.as_of(known_ns))]
            chosen.sort(key=lambda bar: (available_ns(bar), bar.start_ns, bar.source))
            cached = _Resolved(known_ns, tuple(chosen), tuple(available_ns(b) for b in chosen))
            self._resolved_bars[key] = cached
        return cached

    def _rate_series(self, series_id: SeriesId, known_ns: int) -> _Resolved[ReferenceRate]:
        cached = self._resolved_rates.get(series_id)
        if cached is None or cached.known_ns != known_ns:
            series = self._rates.get(series_id, {})
            chosen = [rate for versions in series.values() if (rate := versions.as_of(known_ns))]
            chosen.sort(key=lambda rate: (available_ns(rate), rate.ts_event_ns, rate.source))
            cached = _Resolved(known_ns, tuple(chosen), tuple(available_ns(r) for r in chosen))
            self._resolved_rates[series_id] = cached
        return cached

    def bars(
        self, instrument_id: InstrumentId, interval_ns: int, *, known_ns: int
    ) -> Sequence[Bar]:
        """Bars of one instrument and interval known at ``known_ns``, by availability time."""
        return self._bar_series(instrument_id, interval_ns, known_ns).events

    def released_bars(
        self, instrument_id: InstrumentId, interval_ns: int, *, at_ns: int, known_ns: int
    ) -> Sequence[Bar]:
        """The bars of :meth:`bars` released at ``at_ns`` (``available_ns <= at_ns``)."""
        return self._bar_series(instrument_id, interval_ns, known_ns).released(at_ns)

    def rates(self, series_id: SeriesId, *, known_ns: int) -> Sequence[ReferenceRate]:
        """Observations of one series known at ``known_ns``, by availability time."""
        return self._rate_series(series_id, known_ns).events

    def released_rates(
        self, series_id: SeriesId, *, at_ns: int, known_ns: int
    ) -> Sequence[ReferenceRate]:
        """The observations of :meth:`rates` released at ``at_ns``."""
        return self._rate_series(series_id, known_ns).released(at_ns)
