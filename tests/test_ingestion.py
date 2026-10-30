# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Ingestion (BP4 section 9, J1-J12).

Every provider file here is first-party synthetic data written in the
provider's documented layout (decision log a28, I-1): no third-party data is
used or committed.
"""

import dataclasses
import datetime
import email.message
import hashlib
import http.client
import io
import json
import stat
import urllib.error
import urllib.request
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from auqua.config.base import Environment
from auqua.config.errors import ConfigValidationError
from auqua.config.ingestion import IngestionConfig, SourceAcknowledgement
from auqua.config.loader import load_root
from auqua.config.root import CONFIG_MIGRATIONS
from auqua.config.storage import StorageConfig
from auqua.contracts.events.base import Event, LogRecord
from auqua.contracts.events.ingest import (
    AvailabilityBasis,
    ChecksumResult,
    DataGap,
    GapReason,
    IngestRun,
    RunOutcome,
    UseClass,
)
from auqua.contracts.events.market import Bar, ReferenceRate
from auqua.contracts.events.reference import InstrumentDefined
from auqua.contracts.identity import InstrumentId, SeriesId
from auqua.contracts.instruments import CryptoSpot, Identifiers
from auqua.ingestion import availability
from auqua.ingestion.cli import main
from auqua.ingestion.errors import (
    ChecksumError,
    FetchError,
    NotEligibleError,
    ParseError,
    PermitError,
    TermsNotAcknowledgedError,
)
from auqua.ingestion.fetch import Fetcher, HttpsOnlyRedirects, retry_after_s
from auqua.ingestion.parsers import (
    ParseContext,
    ParserSpec,
    TimeUnit,
    bar_id,
    frb_h10,
    ustreasury_par,
)
from auqua.ingestion.parsers import binance_vision_klines as binance
from auqua.ingestion.parsers import coinbase_candles as coinbase
from auqua.ingestion.raw import RawStore, read_single_member, verify_checksum_file
from auqua.ingestion.runner import IngestRunner, RawInput, revision_id
from auqua.ingestion.sources import (
    BINANCE_VISION_KLINES,
    COINBASE_CANDLES,
    FRB_H10,
    USTREASURY_PAR,
    Clearance,
    Purpose,
    Sharing,
    attributions,
    permit,
    strictest_sharing,
)
from auqua.storage.datasets import dataset_schema, export
from auqua.storage.eventlog import EventLog
from auqua.storage.marketview import MarketView, available_ns, released
from auqua.storage.replay import EventLogReader

ENV = Environment.RESEARCH
TIMEOUT_MS = 5_000
SECOND = 1_000_000_000
MINUTE = 60 * SECOND
HOUR = 60 * MINUTE
T0 = 1_788_220_800 * SECOND  # 2026-09-01T00:00:00Z
BTC = CryptoSpot(
    id=InstrumentId("COINBASE", "BTC-USD"),
    quote_currency="USD",
    price_increment=Decimal("0.01"),
    quantity_increment=Decimal("0.00000001"),
    multiplier=Decimal(1),
    valid_from_ns=0,
    knowledge_ns=0,
    identifiers=Identifiers(),
    base_currency="BTC",
)
#: Binance Vision as if it were cleared for the US: only for tests of checksum and
#: purpose rules that run after eligibility (the shipped profile is not cleared, a32).
BINANCE_CLEARED = dataclasses.replace(
    BINANCE_VISION_KLINES, eligibility=(Clearance("US", cleared=True, basis="test"),)
)
ALL_SOURCES = {
    profile.name: SourceAcknowledgement(use_class=use, acknowledged_terms=profile.terms)
    for profile, use in (
        (COINBASE_CANDLES, UseClass.PERSONAL_RESEARCH),
        (BINANCE_VISION_KLINES, UseClass.PERSONAL_RESEARCH),
        (FRB_H10, UseClass.UNRESTRICTED),
        (USTREASURY_PAR, UseClass.UNRESTRICTED),
    )
}
US_CONFIG = IngestionConfig(jurisdiction="US", sources=ALL_SOURCES)
JURISDICTION = '\n[ingestion]\njurisdiction = "US"\n'


class SteppingClock:
    """Ingestion clock for tests: one second per reading."""

    def __init__(self, start_ns: int) -> None:
        """Start at ``start_ns``."""
        self.now = start_ns

    def now_ns(self) -> int:
        """Advance and return the time."""
        self.now += SECOND
        return self.now


@dataclass
class Rig:
    """A research log, its reader, a raw store and a runner."""

    root: Path
    clock: SteppingClock
    config: IngestionConfig
    environment: Environment = ENV
    log: EventLog = field(init=False)
    reader: EventLogReader = field(init=False)
    store: RawStore = field(init=False)
    runner: IngestRunner = field(init=False)

    def __post_init__(self) -> None:
        """Open everything."""
        path = self.root / "event_log.sqlite3"
        self.log = EventLog(path, self.environment, busy_timeout_ms=TIMEOUT_MS)
        self.reader = EventLogReader(path, self.environment, busy_timeout_ms=TIMEOUT_MS)
        self.store = RawStore(self.root / "raw")
        self.runner = IngestRunner(self.log, self.reader, self.config, self.environment, self.clock)

    def close(self) -> None:
        """Close the log and its reader."""
        self.reader.close()
        self.log.close()

    def ingest(
        self,
        profile: object,
        parser: ParserSpec,
        data: bytes,
        context: ParseContext,
        checksum: ChecksumResult = ChecksumResult.NOT_PUBLISHED,
    ) -> IngestRun:
        """Store ``data`` and ingest it."""
        raw = self.store.put([data], max_bytes=1 << 20)
        return self.runner.run(
            profile,  # type: ignore[arg-type]
            parser,
            RawInput(raw, data, "test://fixture", checksum),
            lambda ingest_ns: ParseContext(
                source=context.source,
                ingest_ns=ingest_ns,
                instrument=context.instrument,
                interval_ns=context.interval_ns,
                window=context.window,
                time_unit=context.time_unit,
                business_days=context.business_days,
            ),
        )

    def events(self) -> list[Event]:
        """Every stored event, in log order."""
        return [record.event for record in self.reader.records()]


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    """A rig with every source enabled."""
    opened = Rig(tmp_path, SteppingClock(T0 + 30 * 24 * HOUR), US_CONFIG)
    yield opened
    opened.close()


# --- synthetic provider files (documented layouts) -----------------------------------------------


def candles(starts_s: list[int], price: str = "65000.10") -> bytes:
    """A Coinbase candles response, newest first: [time, low, high, open, close, volume]."""
    rows = [
        f"[{s}, {price}, {price}, {price}, {price}, 1.25000000]"
        for s in sorted(starts_s, reverse=True)
    ]
    return ("[" + ", ".join(rows) + "]").encode()


def product() -> bytes:
    """A Coinbase product specification."""
    return json.dumps(
        {
            "id": "BTC-USD",
            "base_currency": "BTC",
            "quote_currency": "USD",
            "base_increment": "0.00000001",
            "quote_increment": "0.01000000",
            "status": "online",
        }
    ).encode()


H10_HEADER = (
    '"Series Description","Euro-Area Euro","Japanese Yen"\n'
    '"Unit:","Currency","Currency"\n'
    '"Multiplier:","1","1"\n'
    '"Currency:","EUR","JPY"\n'
    '"Unique Identifier: ","H10/H10/RXI$US_N.B.EU","H10/H10/RXI_N.B.JA"\n'
    '"Time Period","RXI$US_N.B.EU","RXI_N.B.JA"\n'
)


def h10(rows: list[str]) -> bytes:
    """An H.10 Data Download Program CSV with two series."""
    return (H10_HEADER + "".join(row + "\n" for row in rows)).encode()


H10_WEEK = [
    "2026-10-05,1.1211,157.9700",
    "2026-10-06,1.1262,158.1000",
    "2026-10-07,1.1250,158.0000",
    "2026-10-08,1.1240,157.5000",
    "2026-10-09,1.1230,157.2500",
    "2026-10-12,ND,ND",
]


def treasury(rows: list[str], header: str = "1 Mo,10 Yr") -> bytes:
    """A Treasury par-yield CSV, newest first."""
    labels = ",".join(f'"{label}"' for label in header.split(","))
    return (f"Date,{labels}\n" + "".join(row + "\n" for row in rows)).encode()


def klines(starts_ms: list[int], interval_ms: int = 60_000) -> bytes:
    """Binance spot klines (milliseconds), 12 columns, no header."""
    return "".join(
        f"{s},100.00,101.00,99.00,100.50,2.00000000,{s + interval_ms - 1},0,1,0,0,0\n"
        for s in starts_ms
    ).encode()


def coinbase_context(window: tuple[int, int], interval: int = MINUTE) -> ParseContext:
    """Parse context of one Coinbase window."""
    return ParseContext(
        source="ingest/coinbase/exchange",
        ingest_ns=0,
        instrument=BTC,
        interval_ns=interval,
        window=window,
    )


H10_CONTEXT = ParseContext(source="ingest/frb/h10", ingest_ns=0)
UST_CONTEXT = ParseContext(source="ingest/ustreasury/par-yield", ingest_ns=0)


# --- J1 raw store --------------------------------------------------------------------------------


def test_raw_store_is_content_addressed_and_immutable(tmp_path: Path) -> None:
    """Object path = SHA-256; re-put keeps it; tampering is detected; size cap; 0600/0700 (J1)."""
    store = RawStore(tmp_path / "raw")
    raw = store.put([b"abc", b"def"], max_bytes=10)
    assert raw.sha256 == hashlib.sha256(b"abcdef").hexdigest()
    assert raw.size == 6  # noqa: PLR2004
    path = store.path(raw.sha256)
    assert path.parent.name == raw.sha256[:2]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600  # noqa: PLR2004
    assert stat.S_IMODE((tmp_path / "raw").stat().st_mode) == 0o700  # noqa: PLR2004
    assert store.put([b"abcdef"], max_bytes=10) == raw
    assert store.read(raw.sha256) == b"abcdef"
    path.write_bytes(b"abcdeX")
    with pytest.raises(ChecksumError, match="does not match"):
        store.read(raw.sha256)
    with pytest.raises(FetchError, match="exceeds"):
        store.put([b"x" * 11], max_bytes=10)
    assert not list((tmp_path / "raw" / "tmp").iterdir())


def test_archive_member_is_read_with_a_budget() -> None:
    """Only the expected member, read as a stream, within the byte budget (J1, section 7)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("a.csv", "x" * 1000)
    data = buffer.getvalue()
    assert read_single_member(data, "a.csv", max_bytes=1000) == b"x" * 1000
    with pytest.raises(ParseError, match="more than"):
        read_single_member(data, "a.csv", max_bytes=999)
    with pytest.raises(ParseError, match="expected only"):
        read_single_member(data, "b.csv", max_bytes=1000)
    with pytest.raises(ParseError, match="unreadable"):
        read_single_member(b"not a zip", "a.csv", max_bytes=1000)


# --- J2, J4 run key and ids ----------------------------------------------------------------------


def test_same_file_twice_is_a_recorded_no_op(rig: Rig) -> None:
    """Second run with the same key: ``already_ingested``, nothing else stored (J2)."""
    data = treasury(["10/02/2026,4.10,5.00", "10/01/2026,4.11,5.01"])
    first = rig.ingest(USTREASURY_PAR, ustreasury_par.PARSER, data, UST_CONTEXT)
    assert (first.outcome, first.appended, first.duplicates) == (RunOutcome.COMPLETED, 4, 0)
    count = len(rig.events())
    second = rig.ingest(USTREASURY_PAR, ustreasury_par.PARSER, data, UST_CONTEXT)
    assert second.outcome is RunOutcome.ALREADY_INGESTED
    assert len(rig.events()) == count + 1


@dataclass(frozen=True)
class _ChangedParser(ParserSpec):
    """The same parser under another source hash (a changed parser module)."""

    @property
    def parser_id(self) -> str:
        """A different hash."""
        return "f" * 64


def test_changed_parser_with_identical_output_stores_duplicates_only(rig: Rig) -> None:
    """A new run key whose records are all stored already: duplicates only (J2, I3)."""
    data = treasury(["10/02/2026,4.10,5.00"])
    rig.ingest(USTREASURY_PAR, ustreasury_par.PARSER, data, UST_CONTEXT)
    spec = ustreasury_par.PARSER
    changed = _ChangedParser(spec.name, "2", spec.module, spec.parse)
    run = rig.ingest(USTREASURY_PAR, changed, data, UST_CONTEXT)
    assert (run.outcome, run.appended, run.duplicates, run.revisions) == (
        RunOutcome.COMPLETED,
        0,
        2,
        0,
    )


def test_bar_ids_include_the_interval() -> None:
    """One-minute and one-hour bars starting together never share an id (J4)."""
    assert bar_id("s", "V:X", MINUTE, T0) != bar_id("s", "V:X", HOUR, T0)
    assert bar_id("s", "V:X", MINUTE, T0) == bar_id("s", "V:X", MINUTE, T0)


# --- J3 revisions --------------------------------------------------------------------------------


def test_correction_is_a_new_revision_resolved_by_knowledge_time(rig: Rig) -> None:
    """A changed value is revision 1 under a new id; MarketView picks by knowledge time (J3)."""
    rig.ingest(FRB_H10, frb_h10.PARSER, h10(H10_WEEK), H10_CONTEXT)
    before_correction = rig.clock.now
    corrected = [*H10_WEEK]
    corrected[2] = "2026-10-07,1.1255,158.0000"
    run = rig.ingest(FRB_H10, frb_h10.PARSER, h10(corrected), H10_CONTEXT)
    assert (run.revisions, run.appended, run.duplicates) == (1, 1, 9)
    euro = SeriesId("FRB", "H10/RXI$US_N.B.EU")
    view = MarketView.from_reader(rig.reader)
    old = view.rates(euro, known_ns=before_correction)
    new = view.rates(euro, known_ns=rig.clock.now)
    assert [r.value for r in old] == [
        Decimal(v) for v in ("1.1211", "1.1262", "1.1250", "1.1240", "1.1230")
    ]
    assert new[2].value == Decimal("1.1255")
    assert new[2].revision == 1
    assert new[2].id == revision_id(old[2].id, 1)
    again = rig.ingest(FRB_H10, frb_h10.PARSER, h10(corrected), H10_CONTEXT)
    assert again.outcome is RunOutcome.ALREADY_INGESTED


@settings(max_examples=40, deadline=None)
@given(
    st.lists(
        st.tuples(st.integers(0, 3), st.integers(0, 50)),  # (revision, ingest time)
        min_size=1,
        max_size=8,
        unique_by=lambda item: item[0],
    ),
    st.integers(0, 60),
)
def test_market_view_matches_a_brute_force_oracle(
    versions: list[tuple[int, int]], known: int
) -> None:
    """Highest revision with ``ts_ingest_ns <= known`` per key (J3, property test)."""
    series = SeriesId("UST", "PAR/10Y")
    events = [
        ReferenceRate(
            id=revision_id(bar_id("s", "k", 1, 1), revision),
            source="ingest/test",
            ts_event_ns=0,
            ts_ingest_ns=ingest,
            series_id=series,
            value=Decimal(revision),
            unit="percent",
            ts_published_ns=0,
            revision=revision,
        )
        for revision, ingest in versions
    ]
    view = MarketView(LogRecord(i, e) for i, e in enumerate(events))
    eligible = [e for e in events if e.ts_ingest_ns <= known]
    expected = [max(eligible, key=lambda e: e.revision)] if eligible else []
    assert list(view.rates(series, known_ns=known)) == expected


@given(
    st.lists(
        st.tuples(st.integers(0, 20), st.integers(0, 3), st.integers(0, 40), st.integers(0, 40)),
        min_size=1,
        max_size=12,
        unique_by=lambda item: (item[0], item[1]),
    ),
    st.integers(0, 40),
    st.integers(0, 60),
)
def test_indexed_release_equals_the_linear_rule(
    rows: list[tuple[int, int, int, int]], known: int, t: int
) -> None:
    """BP6 amendment 12 (F-AD): the bisected prefix equals filtering by availability."""
    series = SeriesId("UST", "PAR/10Y")
    events = [
        ReferenceRate(
            id=revision_id(bar_id("s", str(observed), 1, 1), revision),
            source="ingest/test",
            ts_event_ns=observed,
            ts_ingest_ns=ingest,
            series_id=series,
            value=Decimal(revision),
            unit="percent",
            ts_published_ns=observed + published,
            revision=revision,
        )
        for observed, revision, ingest, published in rows
    ]
    view = MarketView(LogRecord(i, e) for i, e in enumerate(events))
    linear = [r for r in view.rates(series, known_ns=known) if available_ns(r) <= t]
    assert list(view.released_rates(series, at_ns=t, known_ns=known)) == linear


def test_resolved_series_cache_is_bounded() -> None:
    """One resolved list per series, however often the knowledge time advances (F-AD)."""
    instrument = InstrumentId("COINBASE", "BTC-USD")
    bars = [
        Bar(
            id=bar_id("s", "k", 60, i),
            source="ingest/test",
            ts_event_ns=(i + 1) * 60,
            ts_ingest_ns=(i + 1) * 60,
            instrument_id=instrument,
            start_ns=i * 60,
            interval_ns=60,
            open=Decimal(1),
            high=Decimal(1),
            low=Decimal(1),
            close=Decimal(1),
            volume=Decimal(1),
        )
        for i in range(50)
    ]
    view = MarketView(LogRecord(i, b) for i, b in enumerate(bars))
    for known in range(0, 3_000, 60):
        released = view.released_bars(instrument, 60, at_ns=known, known_ns=known)
        assert len(released) == known // 60
    assert view.cached_series() == 1


# --- J5 checksums --------------------------------------------------------------------------------


def test_provider_checksum_verified_and_mismatch_rejected(rig: Rig) -> None:
    """``.CHECKSUM`` lines checked; a mismatch is recorded as rejected and raised (J5)."""
    data = klines([0])
    digest = hashlib.sha256(data).hexdigest()
    verify_checksum_file(data, f"{digest}  X-1m-2026-09.zip\n", "X-1m-2026-09.zip")
    with pytest.raises(ChecksumError, match="provider checksum"):
        verify_checksum_file(data, f"{'0' * 64}  X-1m-2026-09.zip", "X-1m-2026-09.zip")
    with pytest.raises(ChecksumError, match="names"):
        verify_checksum_file(data, f"{digest}  other.zip", "X-1m-2026-09.zip")
    context = ParseContext(
        source="ingest/binance/vision",
        ingest_ns=0,
        instrument=BTC,
        interval_ns=MINUTE,
        time_unit=TimeUnit.MILLISECONDS,
    )
    with pytest.raises(ChecksumError):
        rig.ingest(BINANCE_CLEARED, binance.PARSER, data, context, ChecksumResult.MISMATCH)
    runs = [e for e in rig.events() if isinstance(e, IngestRun)]
    assert [(r.outcome, r.checksum) for r in runs] == [
        (RunOutcome.REJECTED, ChecksumResult.MISMATCH)
    ]
    assert not [e for e in rig.events() if isinstance(e, Bar)]


# --- J6 gaps -------------------------------------------------------------------------------------


def test_coinbase_missing_buckets_become_gaps_never_bars(rig: Rig) -> None:
    """Missing buckets → DataGap(no_data_published); no bar is synthesized (J6, I10)."""
    start_s = T0 // SECOND
    present = [start_s, start_s + 60, start_s + 240]
    window = (T0, T0 + 6 * MINUTE)
    run = rig.ingest(COINBASE_CANDLES, coinbase.PARSER, candles(present), coinbase_context(window))
    assert (run.appended, run.gaps) == (5, 2)
    gaps = [e for e in rig.events() if isinstance(e, DataGap)]
    assert [(g.start_ns, g.end_ns, g.reason) for g in gaps] == [
        (T0 + 2 * MINUTE, T0 + 4 * MINUTE, GapReason.NO_DATA_PUBLISHED),
        (T0 + 5 * MINUTE, T0 + 6 * MINUTE, GapReason.NO_DATA_PUBLISHED),
    ]
    bars = [e for e in rig.events() if isinstance(e, Bar)]
    assert [b.start_ns for b in bars] == [T0, T0 + MINUTE, T0 + 4 * MINUTE]
    assert all(b.open == Decimal("65000.10") for b in bars)


def test_business_day_gaps_skip_weekends_and_holidays() -> None:
    """ND on Columbus Day is not a gap; a missing Wednesday is (J6)."""
    result = frb_h10.parse(h10([*H10_WEEK, "2026-10-13,1.1200,157.0000"]), H10_CONTEXT)
    assert result.gaps == []
    rows = [H10_WEEK[0], H10_WEEK[1], H10_WEEK[3], H10_WEEK[4]]
    result = frb_h10.parse(h10(rows), H10_CONTEXT)
    day = 86_400 * SECOND
    wednesday = datetime.date(2026, 10, 7)
    start = (wednesday - datetime.date(1970, 1, 1)).days * day
    assert {(g.key, g.start_ns, g.end_ns) for g in result.gaps} == {
        ("FRB:H10/RXI$US_N.B.EU", start, start + day),
        ("FRB:H10/RXI_N.B.JA", start, start + day),
    }


# --- J7 fetcher ----------------------------------------------------------------------------------


class FakeResponse:
    """A response body served in chunks."""

    def __init__(self, body: bytes) -> None:
        """Serve ``body``."""
        self._stream = io.BytesIO(body)
        self.status = 200

    def read(self, size: int = -1, /) -> bytes:
        """Read up to ``size`` bytes."""
        return self._stream.read(size)

    def close(self) -> None:
        """Nothing to release."""


def http_error(code: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    """An HTTPError with an optional Retry-After header."""
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError("https://x", code, "error", headers, None)


@dataclass
class Script:
    """Responses served in order, and the sleeps the fetcher asked for."""

    steps: list[Exception | bytes]
    sleeps: list[float] = field(default_factory=list)
    clock: float = 0.0
    requests: list[str] = field(default_factory=list)

    def opener(self, request: urllib.request.Request, timeout: float) -> FakeResponse:
        """Serve the next step."""
        assert timeout > 0
        self.requests.append(request.get_header("User-agent") or "")
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return FakeResponse(step)

    def sleep(self, seconds: float) -> None:
        """Record and advance the fake monotonic clock."""
        self.sleeps.append(seconds)
        self.clock += seconds


def fetcher(tmp_path: Path, script: Script, **settings_: object) -> Fetcher:
    """A fetcher driven by ``script``."""
    config = IngestionConfig.model_validate(
        {"max_attempts": 4, "backoff_base_ms": 100, "backoff_cap_ms": 1000, **settings_}
    )
    return Fetcher(
        config,
        RawStore(tmp_path / "raw"),
        opener=script.opener,
        sleep=script.sleep,
        monotonic=lambda: script.clock,
        wall_s=lambda: 0.0,
    )


def test_fetch_retries_with_jittered_backoff_and_retry_after(tmp_path: Path) -> None:
    """429 honours Retry-After; 503 and timeouts retry within the cap; then success (J7)."""
    script = Script([http_error(429, "2"), http_error(503), TimeoutError(), b"payload"])
    raw = fetcher(tmp_path, script).get("https://example.test/a", profile="p")
    assert raw.sha256 == hashlib.sha256(b"payload").hexdigest()
    assert script.sleeps[0] >= 2  # noqa: PLR2004 - Retry-After
    assert all(0 <= s <= 1.0 for s in script.sleeps[1:])
    assert len(script.sleeps) == 3  # noqa: PLR2004
    assert all("auqua-ingestion" in agent for agent in script.requests)


def test_fetch_gives_up_and_refuses_non_retryable(tmp_path: Path) -> None:
    """Exhausted attempts and a 404 raise FetchError; only https is fetched (J7)."""
    script = Script([http_error(500) for _ in range(4)])
    with pytest.raises(FetchError, match="4 attempts"):
        fetcher(tmp_path, script).get("https://example.test/a", profile="p")
    assert len(script.sleeps) == 3  # noqa: PLR2004 - no sleep after the last attempt
    with pytest.raises(FetchError, match="HTTP 404"):
        fetcher(tmp_path, Script([http_error(404)])).get("https://example.test/a", profile="p")
    with pytest.raises(FetchError, match="https"):
        fetcher(tmp_path, Script([])).get("http://example.test/a", profile="p")


def test_fetch_spaces_requests_by_the_rate_limit(tmp_path: Path) -> None:
    """Two requests at 2 per second are 0.5 s apart (J7)."""
    script = Script([b"a", b"b"])
    get = fetcher(tmp_path, script, rate_limits={"p": Decimal(2)}).get
    get("https://example.test/a", profile="p")
    get("https://example.test/b", profile="p")
    assert script.sleeps == [0.5]


def test_redirects_never_leave_https() -> None:
    """A redirect to plain HTTP is refused; to HTTPS it is followed (section 7)."""
    handler = HttpsOnlyRedirects()
    request = urllib.request.Request("https://example.test/a")
    headers = http.client.HTTPMessage()
    with pytest.raises(FetchError, match="non-https"):
        handler.redirect_request(request, io.BytesIO(), 302, "Found", headers, "http://x/b")
    followed = handler.redirect_request(request, io.BytesIO(), 302, "Found", headers, "https://x/b")
    assert followed is not None
    assert followed.full_url == "https://x/b"


def test_retry_after_forms() -> None:
    """Delay-seconds and HTTP-date forms; anything else ignored (RFC 9110 section 10.2.3)."""
    assert retry_after_s("7", 0.0) == 7.0  # noqa: PLR2004
    assert retry_after_s("Thu, 01 Jan 1970 00:00:10 GMT", 4.0) == 6.0  # noqa: PLR2004
    assert retry_after_s("soon", 0.0) is None
    assert retry_after_s(None, 0.0) is None


# --- J8 permitted use ----------------------------------------------------------------------------


def test_permitted_use_profiles() -> None:
    """Binance: no paper/live; Coinbase: no ML; outputs carry the strictest sharing (J8)."""
    permit(BINANCE_VISION_KLINES, Purpose.BACKTEST)
    for purpose in (Purpose.PAPER, Purpose.LIVE):
        with pytest.raises(PermitError, match="not permitted"):
            permit(BINANCE_VISION_KLINES, purpose)
    permit(COINBASE_CANDLES, Purpose.RESEARCH)
    with pytest.raises(PermitError, match="machine-learning"):
        permit(COINBASE_CANDLES, Purpose.RESEARCH, ml=True)
    permit(FRB_H10, Purpose.LIVE, ml=True)
    assert strictest_sharing([FRB_H10, BINANCE_VISION_KLINES]) is Sharing.SHARE_ALIKE
    assert strictest_sharing([BINANCE_VISION_KLINES, COINBASE_CANDLES]) is Sharing.PROHIBITED
    assert strictest_sharing([]) is Sharing.ALLOWED
    assert attributions([FRB_H10, BINANCE_VISION_KLINES, FRB_H10]) == [
        "Board of Governors of the Federal Reserve System",
        "Binance Vision",
    ]


def test_runner_refuses_before_writing(tmp_path: Path) -> None:
    """Not enabled, outdated terms, or a purpose outside the profile: nothing written (J8)."""
    data = treasury(["10/02/2026,4.10,5.00"])
    cases: list[
        tuple[
            IngestionConfig, Environment, type[Exception], object, ParserSpec, bytes, ParseContext
        ]
    ] = [
        (
            IngestionConfig(),
            ENV,
            TermsNotAcknowledgedError,
            USTREASURY_PAR,
            ustreasury_par.PARSER,
            data,
            UST_CONTEXT,
        ),
        (
            IngestionConfig(
                sources={
                    USTREASURY_PAR.name: SourceAcknowledgement(
                        use_class=UseClass.UNRESTRICTED, acknowledged_terms="old"
                    )
                }
            ),
            ENV,
            TermsNotAcknowledgedError,
            USTREASURY_PAR,
            ustreasury_par.PARSER,
            data,
            UST_CONTEXT,
        ),
        (
            US_CONFIG,
            Environment.PAPER,
            PermitError,
            BINANCE_CLEARED,
            binance.PARSER,
            klines([0]),
            ParseContext(
                source="ingest/binance/vision",
                ingest_ns=0,
                instrument=BTC,
                interval_ns=MINUTE,
                time_unit=TimeUnit.MILLISECONDS,
            ),
        ),
    ]
    for index, (config, environment, error, profile, parser, payload, context) in enumerate(cases):
        rig = Rig(tmp_path / str(index), SteppingClock(T0), config, environment)
        try:
            with pytest.raises(error):
                rig.ingest(profile, parser, payload, context)
            assert rig.events() == []
        finally:
            rig.close()


def test_eligibility_by_jurisdiction(tmp_path: Path) -> None:
    """No jurisdiction, one not assessed, or one not cleared: refused before writing (ADR 0029)."""
    data = treasury(["10/02/2026,4.10,5.00"])
    context = ParseContext(
        source="ingest/binance/vision",
        ingest_ns=0,
        instrument=BTC,
        interval_ns=MINUTE,
        time_unit=TimeUnit.MILLISECONDS,
    )
    cases: list[tuple[str | None, object, ParserSpec, bytes, ParseContext, str]] = [
        (None, USTREASURY_PAR, ustreasury_par.PARSER, data, UST_CONTEXT, "set ingestion"),
        ("DE", USTREASURY_PAR, ustreasury_par.PARSER, data, UST_CONTEXT, "not been assessed"),
        ("US", BINANCE_VISION_KLINES, binance.PARSER, klines([0]), context, "not cleared"),
    ]
    for index, (jurisdiction, profile, parser, payload, ctx, match) in enumerate(cases):
        config = IngestionConfig(jurisdiction=jurisdiction, sources=ALL_SOURCES)
        rig = Rig(tmp_path / str(index), SteppingClock(T0), config)
        try:
            with pytest.raises(NotEligibleError, match=match):
                rig.ingest(profile, parser, payload, ctx)
            assert rig.events() == []
        finally:
            rig.close()
    assert COINBASE_CANDLES.clearance("US") is not None
    assert all(p.clearance("US") is not None for p in (FRB_H10, USTREASURY_PAR))


def test_run_record_names_the_jurisdiction(rig: Rig) -> None:
    """The lineage record carries the jurisdiction the source was cleared for (ADR 0029)."""
    run = rig.ingest(
        USTREASURY_PAR, ustreasury_par.PARSER, treasury(["10/02/2026,4.1,5"]), UST_CONTEXT
    )
    assert run.jurisdiction == "US"


# --- J9 availability -----------------------------------------------------------------------------


def test_federal_holidays_follow_the_statute() -> None:
    """5 U.S.C. 6103 with Saturday/Sunday observance (J9)."""
    assert availability.federal_holidays(2026) == {
        datetime.date(2026, 1, 1),
        datetime.date(2026, 1, 19),
        datetime.date(2026, 2, 16),
        datetime.date(2026, 5, 25),
        datetime.date(2026, 6, 19),
        datetime.date(2026, 7, 3),  # 4 July is a Saturday
        datetime.date(2026, 9, 7),
        datetime.date(2026, 10, 12),
        datetime.date(2026, 11, 11),
        datetime.date(2026, 11, 26),
        datetime.date(2026, 12, 25),
    }
    assert datetime.date(2021, 12, 31) in availability.federal_holidays(2021)  # 2022-01-01 Sat
    assert datetime.date(2020, 6, 19) not in availability.federal_holidays(2020)
    assert datetime.date(1985, 1, 21) not in availability.federal_holidays(1985)
    assert datetime.date(1975, 10, 27) in availability.federal_holidays(1975)
    with pytest.raises(ValueError, match="1971"):
        availability.federal_holidays(1970)


def test_new_york_offsets_follow_us_dst_rules() -> None:
    """The time-zone database gives the statutory offsets (R6 measurement)."""
    noon = datetime.time(12, 0)
    cases = {
        datetime.date(1990, 1, 2): 17,  # EST
        datetime.date(2006, 4, 3): 16,  # EDT from the first Sunday of April (2006-04-02)
        datetime.date(2007, 3, 12): 16,  # EDT from the second Sunday of March (2007-03-11)
        datetime.date(2026, 11, 2): 17,  # EST again after 2026-11-01
    }
    for day, utc_hour in cases.items():
        ns = availability.new_york_ns(day, noon)
        moment = datetime.datetime.fromtimestamp(ns // SECOND, tz=datetime.UTC)
        assert (moment.date(), moment.hour) == (day, utc_hour)


def test_h10_release_rule_and_gap() -> None:
    """Following Monday 16:15 New York; Tuesday after a holiday Monday; 2006-2009 unknown (J9)."""
    assert availability.h10_release_date(datetime.date(2026, 10, 7)) == datetime.date(2026, 10, 13)
    assert availability.h10_release_date(datetime.date(2026, 9, 30)) == datetime.date(2026, 10, 5)
    ns = availability.h10_available_ns(datetime.date(2026, 10, 7))
    assert ns == availability.new_york_ns(datetime.date(2026, 10, 13), datetime.time(16, 15))
    assert availability.h10_release_date(datetime.date(2007, 6, 6)) is None
    assert availability.h10_release_date(datetime.date(1969, 6, 6)) is None


def test_treasury_declared_rule() -> None:
    """00:00 New York of the N-th business day after the quote date (J9)."""
    thursday = datetime.date(2026, 7, 2)  # Friday 3 July is the observed holiday
    assert availability.treasury_available_ns(thursday, 1) == availability.new_york_ns(
        datetime.date(2026, 7, 6), datetime.time(0, 0)
    )
    assert availability.treasury_available_ns(thursday, 2) == availability.new_york_ns(
        datetime.date(2026, 7, 7), datetime.time(0, 0)
    )


def test_market_view_releases_records_only_when_available(rig: Rig) -> None:
    """No record before its availability; bars at their end, rates at publication (J9)."""
    rig.ingest(FRB_H10, frb_h10.PARSER, h10(H10_WEEK), H10_CONTEXT)
    rates = MarketView.from_reader(rig.reader).rates(
        SeriesId("FRB", "H10/RXI_N.B.JA"), known_ns=rig.clock.now
    )
    release = availability.h10_available_ns(datetime.date(2026, 10, 9))
    assert release is not None
    assert all(available_ns(r) == release for r in rates)
    assert list(released(rates, release - 1)) == []
    assert len(list(released(rates, release))) == 5  # noqa: PLR2004
    bar = coinbase.parse(candles([T0 // SECOND]), coinbase_context((T0, T0 + MINUTE))).events[0]
    assert isinstance(bar, Bar)
    assert available_ns(bar) == T0 + MINUTE


# --- J10 parsers ---------------------------------------------------------------------------------


def test_h10_parses_exactly_with_quote_conventions() -> None:
    """Published digits kept; USD per EUR, JPY per USD; noon New York observation (J10)."""
    result = frb_h10.parse(h10(H10_WEEK[:1]), H10_CONTEXT)
    euro, yen = result.events
    assert isinstance(euro, ReferenceRate)
    assert isinstance(yen, ReferenceRate)
    assert (euro.value, euro.unit) == (Decimal("1.1211"), "USD per EUR")
    assert (str(yen.value), yen.unit) == ("157.9700", "JPY per USD")
    assert euro.ts_event_ns == availability.new_york_ns(
        datetime.date(2026, 10, 5), datetime.time(12)
    )
    assert result.availability is AvailabilityBasis.PUBLISHED_SCHEDULE


@pytest.mark.parametrize(
    ("data", "match"),
    [
        (H10_HEADER.replace('"Unit:"', '"Units"').encode(), "header row"),
        (
            H10_HEADER.replace('"Multiplier:","1","1"', '"Multiplier:","1000","1"').encode(),
            "multiplier",
        ),
        (h10(["2026-10-06,1.1,1.0", "2026-10-05,1.1,1.0"]), "not after"),
        (h10(["2026-10-05,1.1"]), "cells"),
        (h10(["2026-10-05,abc,1.0"]), "RXI"),
        (h10(["05/10/2026,1.1,1.0"]), "date"),
    ],
)
def test_h10_layout_violations_refused(data: bytes, match: str) -> None:
    """Header, multiplier, order, width, value and date violations refused (J10)."""
    with pytest.raises(ParseError, match=match):
        frb_h10.parse(data, H10_CONTEXT)


def test_h10_unknown_release_time_is_counted_not_stored() -> None:
    """Rows inside the 2006-2009 release gap are unavailable (J9, section 4.6)."""
    result = frb_h10.parse(h10(["2007-06-06,1.30,120.0"]), H10_CONTEXT)
    assert (result.events, result.unavailable) == ([], 2)


def test_treasury_parses_tenors_and_refuses_violations() -> None:
    """Tenor series, percent, 15:30 New York; unknown tenor, dates repeated or bad (J10)."""
    result = ustreasury_par.parse(
        treasury(["10/02/2026,4.10,", "10/01/2026,4.11,5.01"]), UST_CONTEXT
    )
    keys = [(str(e.series_id), str(e.value)) for e in result.events if isinstance(e, ReferenceRate)]
    assert keys == [("UST:PAR/10Y", "5.01"), ("UST:PAR/1M", "4.11"), ("UST:PAR/1M", "4.10")]
    first = result.events[0]
    assert isinstance(first, ReferenceRate)
    assert first.ts_event_ns == availability.new_york_ns(
        datetime.date(2026, 10, 1), datetime.time(15, 30)
    )
    assert result.availability is AvailabilityBasis.DECLARED
    for data, match in (
        (treasury(["10/02/2026,4.1,5"], header="1 Mo,11 Yr"), "tenor"),
        (treasury(["10/02/2026,4.1,5", "10/02/2026,4.1,5"]), "repeated"),
        (treasury(["2026-10-02,4.1,5"]), "date"),
        (b"Day,1 Mo\n", "Date"),
    ):
        with pytest.raises(ParseError, match=match):
            ustreasury_par.parse(data, UST_CONTEXT)


@pytest.mark.parametrize(
    ("data", "match"),
    [
        (candles([T0 // SECOND], price="65000.105"), "multiple"),
        (b"[[1, 2, 3]]", "6 fields"),
        (candles([T0 // SECOND + 30]), "grid"),
        (b"[[1788220800, 1, 1, 1, 1, 1], [1788220800, 1, 1, 1, 1, 1]]", "repeated"),
        (b"{}", "not a list"),
        (b"[", "JSON"),
    ],
)
def test_coinbase_violations_refused(data: bytes, match: str) -> None:
    """Off-grid values and times, wrong shape, repeats and bad JSON refused (J10)."""
    with pytest.raises(ParseError, match=match):
        coinbase.parse(data, coinbase_context((T0, T0 + 10 * MINUTE)))


def test_coinbase_window_url_and_outside_buckets() -> None:
    """URL within 299 buckets; buckets outside the window counted, not stored (J10)."""
    url = coinbase.candles_url("BTC-USD", 60, T0 // SECOND, T0 // SECOND + 299 * 60)
    assert url.startswith(
        "https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=60"
    )
    with pytest.raises(ValueError, match="1 to 299"):
        coinbase.candles_url("BTC-USD", 60, T0 // SECOND, T0 // SECOND + 300 * 60)
    with pytest.raises(ValueError, match="granularity"):
        coinbase.candles_url("BTC-USD", 61, 0, 61)
    result = coinbase.parse(
        candles([T0 // SECOND, T0 // SECOND + 60]), coinbase_context((T0, T0 + MINUTE))
    )
    assert len(result.events) == 1
    assert result.notes == ["1 bucket(s) outside the window not used"]


def test_coinbase_product_defines_the_instrument() -> None:
    """Increments from the product; the same specification gives the same id (J10)."""
    context = ParseContext(source="ingest/coinbase/exchange", ingest_ns=T0)
    event = coinbase.parse_product(product(), context).events[0]
    assert isinstance(event, InstrumentDefined)
    assert event.instrument.price_increment == Decimal("0.01")
    assert event.instrument.quantity_increment == Decimal("0.00000001")
    later = coinbase.parse_product(product(), ParseContext(source=context.source, ingest_ns=T0 + 1))
    assert later.events[0].id == event.id
    with pytest.raises(ParseError, match="status"):
        coinbase.parse_product(product().replace(b'"status": "online"', b'"x": 1'), context)


def test_kline_rows_out_of_order_refused() -> None:
    """Repeated or decreasing open times are refused (J10)."""
    context = ParseContext(
        source="ingest/binance/vision",
        ingest_ns=0,
        instrument=BTC,
        interval_ns=MINUTE,
        time_unit=TimeUnit.MILLISECONDS,
    )
    with pytest.raises(binance.KlineFormatError, match="not after"):
        binance.parse(klines([60_000, 0]), context)
    result = binance.parse(klines([0, 120_000]), context)
    assert [(g.start_ns, g.end_ns) for g in result.gaps] == [(MINUTE, 2 * MINUTE)]


# --- J11 crash between batches -------------------------------------------------------------------


class _CrashingLog:
    """An event log that fails on its second append (a crash between batches)."""

    def __init__(self, log: EventLog) -> None:
        """Wrap ``log``."""
        self._log = log
        self.appends = 0

    def append(self, events: list[Event]) -> object:
        """Append the first batch; fail afterwards."""
        self.appends += 1
        if self.appends > 1:
            raise OSError("simulated crash")
        return self._log.append(events)


def test_crash_between_batches_then_rerun_completes(tmp_path: Path) -> None:
    """Batches already stored are duplicates on the re-run, which completes the run (J11)."""
    config = IngestionConfig(jurisdiction="US", sources=ALL_SOURCES, batch_events=2)
    rig = Rig(tmp_path, SteppingClock(T0 + 30 * 24 * HOUR), config)
    data = h10(H10_WEEK[:3])
    try:
        crashing = IngestRunner(
            _CrashingLog(rig.log),  # type: ignore[arg-type]
            rig.reader,
            config,
            ENV,
            rig.clock,
        )
        raw = rig.store.put([data], max_bytes=1 << 20)
        with pytest.raises(OSError, match="simulated"):
            crashing.run(
                FRB_H10,
                frb_h10.PARSER,
                RawInput(raw, data, "test", ChecksumResult.NOT_PUBLISHED),
                lambda ns: ParseContext(source=H10_CONTEXT.source, ingest_ns=ns),
            )
        assert len(rig.events()) == 2  # noqa: PLR2004 - the first batch
        run = rig.ingest(FRB_H10, frb_h10.PARSER, data, H10_CONTEXT)
        assert (run.outcome, run.appended, run.duplicates) == (RunOutcome.COMPLETED, 4, 2)
    finally:
        rig.close()


# --- J12 configuration, datasets, command line ---------------------------------------------------


CONFIG = """\
schema_version = {version}
environment = "research"

[paths]
data_root = "{data}"
output_root = "{output}"
{extra}"""


def _config_root(tmp_path: Path, version: int, extra: str = "") -> Path:
    root = tmp_path / "config"
    (root / "research").mkdir(parents=True, exist_ok=True)
    (root / "research" / "config.toml").write_text(
        CONFIG.format(
            version=version, data=tmp_path / "data", output=tmp_path / "out", extra=extra
        ),
        encoding="utf-8",
    )
    return root


def test_config_version_2_migrates_to_ingestion_defaults(tmp_path: Path) -> None:
    """Version 2 has no ``ingestion`` section; it migrates with defaults (J12, ADR 0029)."""
    config = load_root(ENV, _config_root(tmp_path, 2), environ={})
    assert config.schema_version == CONFIG_MIGRATIONS.current
    assert config.ingestion == IngestionConfig()
    assert config.ingestion.jurisdiction is None
    assert config.ingestion.rate_limits == {"coinbase/exchange/candles": Decimal(3)}
    extra = (
        '\n[ingestion.sources."frb/h10/daily"]\n'
        'use_class = "unrestricted"\nacknowledged_terms = "undated, read 2026-10-21"\n'
    )
    config = load_root(ENV, _config_root(tmp_path, 3, extra), environ={})
    assert config.ingestion.sources["frb/h10/daily"].use_class is UseClass.UNRESTRICTED
    config = load_root(ENV, _config_root(tmp_path, 4, JURISDICTION + extra), environ={})
    assert config.ingestion.jurisdiction == "US"
    with pytest.raises(ConfigValidationError, match="jurisdiction"):
        load_root(
            ENV, _config_root(tmp_path, 4, '\n[ingestion]\njurisdiction = "usa"\n'), environ={}
        )


def test_reference_rates_export_to_a_series_partitioned_dataset(rig: Rig, tmp_path: Path) -> None:
    """``series_id`` partitions like ``instrument_id``: venue = publisher, symbol = code (J12)."""
    rig.ingest(FRB_H10, frb_h10.PARSER, h10(H10_WEEK[:2]), H10_CONTEXT)
    assert "series_id" not in dataset_schema(ReferenceRate).names
    results = export(rig.reader, tmp_path / "datasets", [ReferenceRate], StorageConfig())
    assert results[ReferenceRate].rows == 4  # noqa: PLR2004
    files = sorted((tmp_path / "datasets" / "market.reference_rate.v1").rglob("*.parquet"))
    assert any("venue=FRB/symbol=H10%2FRXI%24US_N.B.EU/year=2026" in str(f) for f in files)
    assert sum(pq.read_metadata(f).num_rows for f in files) == 4  # noqa: PLR2004


def test_command_line_runs_and_refusals(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Local files ingest; a source not enabled is refused; ``runs`` verifies the log (J12)."""
    csv_path = tmp_path / "h10.csv"
    csv_path.write_bytes(h10(H10_WEEK[:2]))
    base = ["--config-root", str(_config_root(tmp_path, 4)), "--environment", "research"]
    assert main([*base, "h10", str(csv_path)]) == 1
    assert "is not enabled" in capsys.readouterr().err
    extra = (
        '\n[ingestion.sources."frb/h10/daily"]\n'
        'use_class = "unrestricted"\nacknowledged_terms = "undated, read 2026-10-21"\n'
    )
    base = ["--config-root", str(_config_root(tmp_path, 4, extra)), "--environment", "research"]
    assert main([*base, "h10", str(csv_path)]) == 1
    assert "set ingestion.jurisdiction" in capsys.readouterr().err
    config_root = _config_root(tmp_path, 4, JURISDICTION + extra)
    base = ["--config-root", str(config_root), "--environment", "research"]
    assert main([*base, "h10", str(csv_path)]) == 0
    assert main([*base, "h10", str(csv_path)]) == 0
    assert main([*base, "runs"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("completed")
    assert "appended=4" in out[0]
    assert out[1].startswith("already_ingested")
    assert out[-1].startswith("log verified: next_sequence=")
    assert main(["--config-root", str(tmp_path / "none"), "--environment", "research", "runs"]) == 2  # noqa: PLR2004
