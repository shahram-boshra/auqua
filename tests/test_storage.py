# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Tests for the storage layer (BP3 Z1-Z13; ADRs 0021-0023).

Every database and dataset is created in a temporary directory.
"""

import hashlib
import sqlite3
import stat
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import duckdb
import msgspec
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from auqua.clock.log import LogClock
from auqua.config.base import Environment
from auqua.config.storage import StorageConfig
from auqua.contracts.codec import encode
from auqua.contracts.events import Bar, CorporateAction, InstrumentDefined, Quote, Trade
from auqua.contracts.events.base import Event, LogRecord
from auqua.contracts.identity import InstrumentId, new_id
from auqua.contracts.instruments import CryptoSpot, Identifiers
from auqua.storage.chain import GENESIS, record_hash
from auqua.storage.datasets import (
    DECIMAL_TYPE,
    MANIFEST,
    DatasetWriter,
    dataset_schema,
    event_tag,
    export,
    load_manifest,
    partition_path,
)
from auqua.storage.errors import (
    DuplicateConflictError,
    EncodingError,
    LogCorruptionError,
    SchemaMismatchError,
    StorageError,
    UnsupportedStoreVersionError,
    WriterLockedError,
)
from auqua.storage.eventlog import EventLog
from auqua.storage.files import PRIVATE_DIRECTORY, PRIVATE_FILE, write_private
from auqua.storage.query import QuerySession
from auqua.storage.reference import ReferenceIndex
from auqua.storage.replay import EventLogReader, replay
from auqua.storage.scaled import INT64_MAX, ScaledDecimal
from auqua.storage.sqlite import connect_reader
from auqua.storage.store import StoreMigrations, StoreStep, open_store

ENV = Environment.TEST
BTC = InstrumentId("BINANCE", "BTCUSDT")
SHIB = InstrumentId("BINANCE", "SHIB/USDT")
T0 = 1_735_689_600_000_000_000  # 2025-01-01T00:00:00Z
MINUTE_NS = 60_000_000_000
TIMEOUT_MS = 5_000


def trade(index: int, instrument: InstrumentId = BTC, price: str = "93800.10") -> Trade:
    """A trade one minute after the previous index."""
    ts = T0 + index * MINUTE_NS
    return Trade(
        id=new_id(),
        source="ingest/test",
        ts_event_ns=ts,
        ts_ingest_ns=ts + 1,
        instrument_id=instrument,
        price=Decimal(price),
        quantity=Decimal("0.001"),
    )


@pytest.fixture
def log_path(tmp_path: Path) -> Path:
    """Path of a not yet created event log."""
    return tmp_path / "data" / "event_log.sqlite3"


@pytest.fixture
def log(log_path: Path) -> Iterator[EventLog]:
    """An open, empty event log."""
    with EventLog(log_path, ENV, busy_timeout_ms=TIMEOUT_MS) as opened:
        yield opened


def _reader(path: Path) -> EventLogReader:
    return EventLogReader(path, ENV, busy_timeout_ms=TIMEOUT_MS)


def _records(path: Path) -> list[LogRecord[Event]]:
    with _reader(path) as reader:
        return list(reader.records())


# Z1, Z2 -------------------------------------------------------------------------------


def test_sequences_contiguous_across_batches_and_reopen(log_path: Path) -> None:
    """Sequences start at 0, stay contiguous across batches and reopen (Z1)."""
    events = [trade(i) for i in range(5)]
    with EventLog(log_path, ENV, busy_timeout_ms=TIMEOUT_MS) as log:
        first = log.append(events[:3])
    with EventLog(log_path, ENV, busy_timeout_ms=TIMEOUT_MS) as log:
        assert log.head == first.head
        second = log.append(events[3:])
    assert (first.appended, second.appended, second.head.next_sequence) == (3, 2, 5)
    records = _records(log_path)
    assert [r.sequence for r in records] == list(range(5))
    assert [r.event for r in records] == events


def test_duplicates_counted_and_conflicts_roll_back(log: EventLog, log_path: Path) -> None:
    """Identical copies are counted; a conflicting copy rolls the batch back (Z2)."""
    original = trade(0)
    result = log.append([original, original])
    assert (result.appended, result.duplicates) == (1, 1)
    assert log.append([original]).duplicates == 1
    conflict = msgspec.structs.replace(original, price=Decimal("1.00"))
    with pytest.raises(DuplicateConflictError, match="conflicts with sequence 0"):
        log.append([trade(1), conflict])
    assert log.head.next_sequence == 1
    assert [r.event for r in _records(log_path)] == [original]


def test_non_canonical_event_type_refused(log: EventLog) -> None:
    """The log stores only types its reader can decode."""

    class Foreign(Event, frozen=True, tag="test.foreign.v1"):
        """Not part of the canonical union."""

    foreign = Foreign(id=new_id(), source="test/x", ts_event_ns=T0, ts_ingest_ns=T0)
    with pytest.raises(EncodingError, match="not a canonical"):
        log.append([foreign])


# Z3 -----------------------------------------------------------------------------------


def _independent_head(events: list[Trade]) -> bytes:
    previous = bytes(32)
    for sequence, event in enumerate(events):
        digest = hashlib.sha256(previous + sequence.to_bytes(8, "big") + encode(event))
        previous = digest.digest()
    return previous


def _tamper(path: Path, statement: str) -> None:
    connection = sqlite3.connect(path, autocommit=True)
    connection.executescript(statement)
    connection.close()


def test_head_hash_is_an_independent_recomputation(log: EventLog, log_path: Path) -> None:
    """The head hash equals SHA-256 chaining computed without the storage code (Z3)."""
    events = [trade(i) for i in range(4)]
    head = log.append(events).head
    assert head.hash == _independent_head(events)
    with _reader(log_path) as reader:
        assert reader.verify() == head
        assert reader.head() == head
    assert record_hash(GENESIS, 0, encode(events[0])) == _independent_head(events[:1])


@pytest.mark.parametrize(
    ("statement", "match"),
    [
        (
            "UPDATE log SET payload = CAST(replace(CAST(payload AS TEXT), '93800.10', "
            "'93800.11') AS BLOB) WHERE sequence = 1",
            "hash chain broken",
        ),
        ("DELETE FROM log WHERE sequence = 1", "expected sequence 1"),
        ("UPDATE log SET ts_event_ns = ts_event_ns + 1 WHERE sequence = 2", "columns disagree"),
        (
            "UPDATE log SET sequence = -1 WHERE sequence = 1; "
            "UPDATE log SET sequence = 1 WHERE sequence = 2; "
            "UPDATE log SET sequence = 2 WHERE sequence = -1;",
            "hash chain broken",
        ),
    ],
)
def test_tampering_detected(log_path: Path, statement: str, match: str) -> None:
    """A changed byte, a removed record, an edited column or a swap stops replay (Z3)."""
    with EventLog(log_path, ENV, busy_timeout_ms=TIMEOUT_MS) as log:
        log.append([trade(i) for i in range(4)])
    _tamper(log_path, statement)
    with _reader(log_path) as reader, pytest.raises(LogCorruptionError, match=match):
        reader.verify()


# Z4 -----------------------------------------------------------------------------------


def test_single_writer_read_only_reader_and_settings(log: EventLog, log_path: Path) -> None:
    """A second writer is refused; readers cannot write; WAL and FULL are set (Z4)."""
    with pytest.raises(WriterLockedError):
        EventLog(log_path, ENV, busy_timeout_ms=TIMEOUT_MS)
    log.append([trade(0)])
    reader = connect_reader(log_path, TIMEOUT_MS)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        reader.execute("DELETE FROM log")
    reader.close()
    connection = log._connection  # the writer's own connection carries its settings
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2  # noqa: PLR2004 - FULL
    for path in (log_path, log_path.with_name(log_path.name + "-wal")):
        assert stat.S_IMODE(path.stat().st_mode) == PRIVATE_FILE


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_private_directories_and_files(tmp_path: Path) -> None:
    """Every missing parent is 0700 and files are 0600, also over a wider existing file."""
    nested = tmp_path / "a" / "b" / "event_log.sqlite3"
    with EventLog(nested, ENV, busy_timeout_ms=TIMEOUT_MS) as opened:
        opened.append([trade(0)])
    assert {_mode(tmp_path / "a"), _mode(tmp_path / "a" / "b")} == {PRIVATE_DIRECTORY}
    root = tmp_path / "x" / "datasets"
    _export(nested, root, _config())
    assert {_mode(p) for p in [tmp_path / "x", *root.rglob("*")] if p.is_dir()} == {
        PRIVATE_DIRECTORY
    }
    assert {_mode(p) for p in root.rglob("*") if p.is_file()} == {PRIVATE_FILE}
    wide = tmp_path / "out" / "report.html"
    wide.parent.mkdir()
    wide.write_text("old", encoding="utf-8")
    wide.chmod(0o644)
    write_private(wide, b"new")
    assert (_mode(wide), wide.read_bytes()) == (PRIVATE_FILE, b"new")
    assert not list(wide.parent.glob(".*.tmp"))


def test_other_environment_refused(log_path: Path) -> None:
    """A log is bound to the environment that created it."""
    EventLog(log_path, ENV, busy_timeout_ms=TIMEOUT_MS).close()
    with pytest.raises(StorageError, match="environment"):
        EventLog(log_path, Environment.LIVE, busy_timeout_ms=TIMEOUT_MS)
    with pytest.raises(StorageError, match="environment"):
        EventLogReader(log_path, Environment.LIVE, busy_timeout_ms=TIMEOUT_MS)
    EventLog(log_path, ENV, busy_timeout_ms=TIMEOUT_MS).close()  # lock released on refusal


# Z5 -----------------------------------------------------------------------------------


def test_replay_reproduces_live_inputs_and_clock(log: EventLog, log_path: Path) -> None:
    """Live and replay see identical records and clock readings; bytes re-encode (Z5)."""
    events = [trade(i) for i in range(6)]
    live_clock, live_seen = LogClock(), []
    for sequence, event in enumerate(events):
        live_clock.advance(event.ts_ingest_ns)
        live_seen.append((LogRecord(sequence, event), live_clock.now_ns()))
    log.append(events)
    replay_clock, replay_seen = LogClock(), []
    with _reader(log_path) as reader:
        count = replay(
            reader, replay_clock, lambda r: replay_seen.append((r, replay_clock.now_ns()))
        )
        stored = {r.sequence: encode(r.event) for r in reader.records()}
    assert count == len(events)
    assert replay_seen == live_seen
    assert [stored[i] for i in range(6)] == [encode(e) for e in events]
    with _reader(log_path) as reader:
        tail: list[LogRecord[Event]] = []
        replay(reader, LogClock(), tail.append, from_sequence=4)
    assert [r.sequence for r in tail] == [4, 5]


# Z6 -----------------------------------------------------------------------------------

_CRASH = textwrap.dedent(
    """
    import os, sys
    from pathlib import Path
    from auqua.config.base import Environment
    from auqua.storage.eventlog import EventLog
    from tests.test_storage import trade

    def batch():
        for i in range(3, 6):
            yield trade(i)
        os._exit(17)  # killed inside the open transaction

    log = EventLog(Path(sys.argv[1]), Environment.TEST, busy_timeout_ms=5000)
    log.append([trade(i) for i in range(3)])
    log.append(batch())
    """
)


def test_crash_inside_append_keeps_last_commit(log_path: Path) -> None:
    """A process killed inside an append leaves the log at its last commit (Z6)."""
    completed = subprocess.run(  # noqa: S603 - fixed interpreter and script
        [sys.executable, "-c", _CRASH, str(log_path)],
        check=False,
        cwd=Path(__file__).parent.parent,
    )
    assert completed.returncode == 17  # noqa: PLR2004 - the script's exit code
    with _reader(log_path) as reader:
        assert reader.verify().next_sequence == 3  # noqa: PLR2004 - first batch only
    with EventLog(log_path, ENV, busy_timeout_ms=TIMEOUT_MS) as log:  # lock was released
        assert log.head.next_sequence == 3  # noqa: PLR2004


# Z7 -----------------------------------------------------------------------------------


@given(
    scale=st.integers(min_value=0, max_value=18),
    data=st.data(),
)
def test_scaled_decimal_round_trip(scale: int, data: st.DataObject) -> None:
    """Values on the scale and inside 64 bits round-trip exactly (Z7)."""
    stored = data.draw(st.integers(min_value=-(2**63), max_value=INT64_MAX))
    codec = ScaledDecimal(scale)
    value = codec.decode(stored)
    assert codec.encode(value) == stored
    assert value.as_tuple().exponent == -scale
    assert codec.encode(value.normalize()) == stored


@pytest.mark.parametrize(
    ("scale", "value", "match"),
    [
        (2, "1.005", "decimal places"),
        (0, "0.5", "decimal places"),
        (8, "92233720368.54775808", "outside 64 bits"),
        (2, "NaN", "not finite"),
        (2, "Infinity", "not finite"),
    ],
)
def test_scaled_decimal_refuses_inexact(scale: int, value: str, match: str) -> None:
    """Rounding, overflow and non-finite values are refused, never stored (Z7)."""
    with pytest.raises(EncodingError, match=match):
        ScaledDecimal(scale).encode(Decimal(value))


def test_scaled_decimal_bounds() -> None:
    """Scale 0..18 only; decode refuses values outside 64 bits."""
    with pytest.raises(ValueError, match="scale"):
        ScaledDecimal(19)
    with pytest.raises(EncodingError):
        ScaledDecimal(2).decode(2**63)
    assert ScaledDecimal(2).encode(Decimal("-1.50")) == -150  # noqa: PLR2004


# Z8 -----------------------------------------------------------------------------------


def _step(statement: str) -> StoreStep:
    def run(connection: sqlite3.Connection) -> None:
        connection.execute(statement)

    return run


def _store_migrations() -> StoreMigrations:
    migrations = StoreMigrations(current=2)
    migrations.register(
        0, _step("CREATE TABLE cash (currency TEXT PRIMARY KEY, amount INTEGER NOT NULL) STRICT")
    )
    migrations.register(
        1, _step("ALTER TABLE cash ADD COLUMN updated_ns INTEGER NOT NULL DEFAULT 0")
    )
    return migrations


def test_store_migrations_and_strict_tables(tmp_path: Path) -> None:
    """Stores migrate step by step; newer versions and loose tables are refused (Z8)."""
    path = tmp_path / "state.sqlite3"
    connection = open_store(path, _store_migrations(), busy_timeout_ms=TIMEOUT_MS)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 2  # noqa: PLR2004
    usd = ScaledDecimal(2)
    connection.execute("INSERT INTO cash VALUES ('USD', ?, 0)", (usd.encode(Decimal("10.25")),))
    assert usd.decode(connection.execute("SELECT amount FROM cash").fetchone()[0]) == Decimal(
        "10.25"
    )
    with pytest.raises(sqlite3.Error, match="REAL"):
        connection.execute("INSERT INTO cash VALUES ('EUR', 1.5, 0)")
    connection.execute("PRAGMA user_version = 3")
    connection.close()
    with pytest.raises(UnsupportedStoreVersionError):
        open_store(path, _store_migrations(), busy_timeout_ms=TIMEOUT_MS)
    loose = StoreMigrations(current=1)
    loose.register(0, _step("CREATE TABLE notes (text)"))
    with pytest.raises(SchemaMismatchError, match="STRICT"):
        open_store(tmp_path / "loose.sqlite3", loose, busy_timeout_ms=TIMEOUT_MS)
    gap = StoreMigrations(current=2)
    gap.register(0, _step("SELECT 1"))
    with pytest.raises(UnsupportedStoreVersionError, match="no step"):
        open_store(tmp_path / "gap.sqlite3", gap, busy_timeout_ms=TIMEOUT_MS)


# Z9 -----------------------------------------------------------------------------------


def test_dataset_schemas_of_market_events() -> None:
    """Market events map to fixed column types; other shapes are refused (Z9)."""
    trade_schema = dataset_schema(Trade)
    assert trade_schema.names == [
        "sequence",
        "id",
        "source",
        "ts_event_ns",
        "ts_ingest_ns",
        "price",
        "quantity",
        "aggressor",
        "venue_trade_id",
    ]
    assert trade_schema.field("price").type == DECIMAL_TYPE
    assert trade_schema.field("id").type == pa.binary(16)
    assert trade_schema.field("aggressor").nullable
    assert not trade_schema.field("price").nullable
    for event_type in (Quote, Bar):
        types = {f.type for f in dataset_schema(event_type) if f.name not in {"id", "source"}}
        assert types <= {pa.int64(), DECIMAL_TYPE}
    for refused in (InstrumentDefined, CorporateAction):
        with pytest.raises(SchemaMismatchError):
            dataset_schema(refused)


# Z10-Z12 ------------------------------------------------------------------------------


def _config(**changes: int) -> StorageConfig:
    return StorageConfig(**changes)


def _export(log_path: Path, root: Path, config: StorageConfig) -> int:
    with _reader(log_path) as reader:
        return export(reader, root, [Trade], config)[Trade].rows


def test_export_and_query_keep_decimals_exact(
    log: EventLog, log_path: Path, tmp_path: Path
) -> None:
    """Instruments of different increments query back exactly (Z10)."""
    log.append([trade(0), trade(1, SHIB, "0.00001234"), trade(2, SHIB, "0.00001235")])
    root = tmp_path / "datasets"
    assert _export(log_path, root, _config()) == 3  # noqa: PLR2004
    with QuerySession(root, _config()) as session:
        assert session.register(Trade, "trades") == 3  # noqa: PLR2004
        table = session.sql(
            "SELECT symbol, price FROM trades WHERE venue = ? ORDER BY sequence", ["BINANCE"]
        )
    assert table.column("symbol").to_pylist() == ["BTCUSDT", "SHIB/USDT", "SHIB/USDT"]
    assert table.column("price").to_pylist() == [
        Decimal("93800.10"),
        Decimal("0.00001234"),
        Decimal("0.00001235"),
    ]
    assert (root / "market.trade.v1" / partition_path("BINANCE", "SHIB/USDT", 2025)).is_dir()


def test_query_refuses_changed_or_mis_scaled_files(
    log: EventLog, log_path: Path, tmp_path: Path
) -> None:
    """A file with another decimal scale or changed bytes fails before DuckDB reads (Z10)."""
    log.append([trade(0)])
    root = tmp_path / "datasets"
    _export(log_path, root, _config())
    directory = root / event_tag(Trade)
    manifest = load_manifest(directory, Trade)
    assert manifest is not None
    entry = manifest.files[0]
    table = pq.read_table(directory / entry.path)
    narrowed = table.set_column(
        table.schema.get_field_index("price"),
        "price",
        table.column("price").cast(pa.decimal128(38, 8)),
    )
    pq.write_table(narrowed, directory / entry.path)
    digest = hashlib.sha256((directory / entry.path).read_bytes()).hexdigest()
    with QuerySession(root, _config()) as session:
        with pytest.raises(SchemaMismatchError, match="changed"):
            session.register(Trade, "trades")
        fixed = msgspec.structs.replace(
            manifest, files=(msgspec.structs.replace(entry, sha256=digest),)
        )
        (directory / MANIFEST).write_bytes(msgspec.json.encode(fixed))
        with pytest.raises(SchemaMismatchError, match="schema differs"):
            session.register(Trade, "trades")


def test_buffer_bounded_resume_and_compaction(
    log: EventLog, log_path: Path, tmp_path: Path
) -> None:
    """Bounded buffer, resumable export, immutable files, compaction (Z11)."""
    config = _config(buffer_rows=3, rows_per_group=2, rows_per_file=4, max_files_per_partition=2)
    root = tmp_path / "datasets"
    log.append([trade(i) for i in range(7)])
    log.append([Quote(
        id=new_id(), source="ingest/test", ts_event_ns=T0, ts_ingest_ns=T0, instrument_id=BTC,
        bid_price=Decimal(1), bid_size=Decimal(1), ask_price=Decimal(2), ask_size=Decimal(1),
    )])  # fmt: skip
    assert _export(log_path, root, config) == 7  # noqa: PLR2004
    directory = root / event_tag(Trade)
    first = load_manifest(directory, Trade)
    assert first is not None
    assert first.next_sequence == 8  # noqa: PLR2004 - includes the quote it skipped
    assert len(first.files) <= config.max_files_per_partition
    assert sum(e.rows for e in first.files) == 7  # noqa: PLR2004
    for entry in first.files:
        metadata = pq.ParquetFile(directory / entry.path).metadata
        assert all(
            metadata.row_group(i).num_rows <= config.rows_per_group
            for i in range(metadata.num_row_groups)
        )
    with _reader(log_path) as reader:
        assert first.head_hash == reader.hash_at(7).hex()
    before = {e.path: e.sha256 for e in first.files}
    (directory / "orphan.parquet").write_bytes(b"left by a crash")
    log.append([trade(i) for i in range(7, 10)])
    assert _export(log_path, root, config) == 3  # noqa: PLR2004
    second = load_manifest(directory, Trade)
    assert second is not None
    for entry in second.files:
        if entry.path in before:
            assert before[entry.path] == entry.sha256
    assert not (directory / "orphan.parquet").exists()
    assert set(second.previous_files) <= {
        p.relative_to(directory).as_posix() for p in directory.rglob("*.parquet")
    }
    assert _export(log_path, root, config) == 0  # nothing new: no manifest is published
    assert load_manifest(directory, Trade) == second
    with QuerySession(root, config) as session:
        session.register(Trade, "trades")
        sequences = session.sql("SELECT sequence FROM trades ORDER BY sequence")
    assert sequences.column("sequence").to_pylist() == [*range(7), *range(8, 11)]
    writer = DatasetWriter(root, Trade, config)
    with _reader(log_path) as reader:
        records = [r for r in reader.records() if isinstance(r.event, Trade)]
    for record in records[:3]:
        writer.add(record)
    assert writer.full
    with pytest.raises(StorageError, match="buffer full"):
        writer.add(records[3])


def test_value_beyond_decimal_column_refused(log: EventLog, log_path: Path, tmp_path: Path) -> None:
    """A price with more than 18 decimals is refused, never rounded (Z10)."""
    log.append([trade(0, price="0.0000000000000000001")])
    with pytest.raises(EncodingError, match="not representable"):
        _export(log_path, tmp_path / "datasets", _config())


def test_query_session_is_sandboxed(log: EventLog, log_path: Path, tmp_path: Path) -> None:
    """Reads outside the datasets root and setting changes are refused (Z12)."""
    log.append([trade(0)])
    root = tmp_path / "datasets"
    _export(log_path, root, _config())
    outside = tmp_path / "outside.parquet"
    pq.write_table(pa.table({"a": [1]}), outside)
    with QuerySession(root, _config()) as session:
        with pytest.raises(duckdb.Error):
            session.sql(f"SELECT * FROM read_parquet('{outside.as_posix()}')")  # noqa: S608 - test path
        for statement in ("SET enable_external_access = true", "SET memory_limit = '8GB'"):
            with pytest.raises(duckdb.Error):
                session.sql(statement)
        with pytest.raises(ValueError, match="view name"):
            session.register(Trade, "trades; DROP")


# Z13 ----------------------------------------------------------------------------------


def _instrument(valid_from: int, knowledge: int, increment: str) -> InstrumentDefined:
    return InstrumentDefined(
        id=new_id(),
        source="ingest/reference",
        ts_event_ns=knowledge,
        ts_ingest_ns=knowledge,
        instrument=CryptoSpot(
            id=BTC,
            quote_currency="USDT",
            price_increment=Decimal(increment),
            quantity_increment=Decimal("0.00001"),
            multiplier=Decimal(1),
            valid_from_ns=valid_from,
            knowledge_ns=knowledge,
            identifiers=Identifiers(),
            base_currency="BTC",
        ),
    )


VERSIONS = st.lists(
    st.tuples(st.integers(0, 20), st.integers(0, 20), st.sampled_from(["0.01", "0.1", "1"])),
    min_size=1,
    max_size=8,
)


@settings(max_examples=150)
@given(versions=VERSIONS, valid=st.integers(0, 22), known=st.integers(0, 22))
def test_as_of_matches_brute_force(
    versions: list[tuple[int, int, str]], valid: int, known: int
) -> None:
    """``as_of`` equals a brute-force search over all versions (Z13)."""
    records = [
        LogRecord(sequence, _instrument(*version)) for sequence, version in enumerate(versions)
    ]
    index = ReferenceIndex.from_records(records)
    candidates = [
        (v[0], v[1], s) for s, v in enumerate(versions) if v[0] <= valid and v[1] <= known
    ]
    expected = None
    if candidates:
        expected = records[max(candidates)[2]].event
        assert isinstance(expected, InstrumentDefined)
    found = index.as_of(BTC, valid, known)
    assert found == (None if expected is None else expected.instrument)
    assert index.as_of(SHIB, valid, known) is None
