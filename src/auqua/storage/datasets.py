# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Analytical Parquet datasets derived from the event log (ADR 0022).

One dataset per event type and version, under ``<root>/<tag>/``, Hive
partitions ``venue=…/symbol=…/year=…`` (UTC year of ``ts_event_ns``;
partition values percent-encoded, which DuckDB decodes). Column types are
fixed per dataset: every decimal is ``decimal128(38, 18)``, because DuckDB
silently truncates when files of one dataset differ in decimal scale.

The manifest is the dataset's state: it lists exactly the published files and
the log position they cover, and every change publishes a new manifest with an
atomic ``os.replace`` (the Apache Iceberg pattern). Readers use only the files
listed in the manifest they loaded. Rows pass through a bounded buffer: when it
is full, it is flushed before more rows are accepted, so nothing is dropped.
"""

import datetime
import hashlib
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, unquote

import msgspec
import pyarrow as pa
import pyarrow.parquet as pq

from auqua.config.storage import StorageConfig
from auqua.contracts.events.base import Event, LogRecord
from auqua.contracts.identity import InstrumentId
from auqua.storage.chain import GENESIS
from auqua.storage.errors import (
    EncodingError,
    SchemaMismatchError,
    StorageError,
    UnsupportedStoreVersionError,
)
from auqua.storage.replay import EventLogReader

DECIMAL_TYPE = pa.decimal128(38, 18)
MANIFEST = "manifest.json"
MANIFEST_FORMAT = 1
PARTITION_KEYS = ("venue", "symbol", "year")
_INSTRUMENT_FIELD = "instrument_id"
_NS_PER_SECOND = 1_000_000_000


class FileEntry(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """One published Parquet file (path relative to the dataset directory)."""

    path: str
    rows: int
    first_sequence: int
    last_sequence: int
    sha256: str


class Manifest(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """State of one dataset: its files and the log position they cover.

    Attributes:
        format: Manifest format version.
        tag: Event tag of the dataset.
        schema_fingerprint: SHA-256 of the dataset's Arrow schema.
        version: Incremented by every published change.
        next_sequence: First log sequence not yet exported.
        head_hash: Hex log hash of record ``next_sequence - 1`` (genesis when 0).
        files: Files of this snapshot.
        previous_files: Files of the previous snapshot, retained for readers.
    """

    format: int
    tag: str
    schema_fingerprint: str
    version: int
    next_sequence: int
    head_hash: str
    files: tuple[FileEntry, ...]
    previous_files: tuple[str, ...] = ()


def _arrow_type(field: msgspec.inspect.Field) -> tuple[Any, bool]:
    """Arrow type and nullability of one event field (BP3 section 4.3)."""
    kind = field.type
    nullable = False
    if isinstance(kind, msgspec.inspect.UnionType):
        members = [t for t in kind.types if not isinstance(t, msgspec.inspect.NoneType)]
        if len(members) != 1 or len(members) == len(kind.types):
            raise SchemaMismatchError(f"{field.name}: unsupported union")
        kind, nullable = members[0], True
    match kind:
        case msgspec.inspect.DecimalType():
            return DECIMAL_TYPE, nullable
        case msgspec.inspect.IntType():
            return pa.int64(), nullable
        case msgspec.inspect.UUIDType():
            return pa.binary(16), nullable
        case msgspec.inspect.StrType() | msgspec.inspect.EnumType():
            return pa.string(), nullable
        case msgspec.inspect.BoolType():
            return pa.bool_(), nullable
        case _:
            raise SchemaMismatchError(f"{field.name}: no column rule for {type(kind).__name__}")


def _fields(event_type: type[Event]) -> tuple[msgspec.inspect.Field, ...]:
    info = msgspec.inspect.type_info(event_type)
    if not isinstance(info, msgspec.inspect.StructType):  # pragma: no cover - events are structs
        raise SchemaMismatchError(f"{event_type.__name__} is not a struct")
    fields = info.fields
    instrument = [f for f in fields if f.name == _INSTRUMENT_FIELD]
    if not instrument or not (
        isinstance(instrument[0].type, msgspec.inspect.StructType)
        and instrument[0].type.cls is InstrumentId
    ):
        raise SchemaMismatchError(f"{event_type.__name__} has no instrument_id to partition by")
    return tuple(f for f in fields if f.name != _INSTRUMENT_FIELD)


def dataset_schema(event_type: type[Event]) -> pa.Schema:
    """Arrow schema of the files of ``event_type``'s dataset (partition keys excluded).

    Raises:
        SchemaMismatchError: a field has no column rule, or the type has no
            ``instrument_id``.
    """
    columns = [pa.field("sequence", pa.int64(), nullable=False)]
    for field in _fields(event_type):
        arrow_type, nullable = _arrow_type(field)
        columns.append(pa.field(field.name, arrow_type, nullable=nullable))
    return pa.schema(columns)


def schema_fingerprint(schema: pa.Schema) -> str:
    """SHA-256 of the schema's text form."""
    text = schema.to_string(show_field_metadata=False, show_schema_metadata=False)
    return hashlib.sha256(text.encode()).hexdigest()


def event_tag(event_type: type[Event]) -> str:
    """The versioned tag of ``event_type``."""
    tag = event_type.__struct_config__.tag
    if not isinstance(tag, str):  # pragma: no cover - every event type has a string tag
        raise SchemaMismatchError(f"{event_type.__name__} has no string tag")
    return tag


def _column_value(value: object) -> object:
    if isinstance(value, uuid.UUID):
        return value.bytes
    if isinstance(value, Enum):
        return value.value
    return value


def _partition(event: Event) -> tuple[str, str, int]:
    instrument = cast("InstrumentId", getattr(event, _INSTRUMENT_FIELD))
    seconds = event.ts_event_ns // _NS_PER_SECOND
    year = datetime.datetime.fromtimestamp(seconds, tz=datetime.UTC).year
    return instrument.venue, instrument.symbol, year


def partition_path(venue: str, symbol: str, year: int) -> str:
    """Relative Hive directory of one partition (values percent-encoded)."""
    return f"venue={quote(venue, safe='')}/symbol={quote(symbol, safe='')}/year={year:04d}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def load_manifest(directory: Path, event_type: type[Event]) -> Manifest | None:
    """Load the dataset manifest, or ``None`` when the dataset does not exist yet.

    Raises:
        UnsupportedStoreVersionError: unknown manifest format.
        SchemaMismatchError: the manifest belongs to another tag or schema.
    """
    path = directory / MANIFEST
    if not path.exists():
        return None
    manifest = msgspec.json.decode(path.read_bytes(), type=Manifest)
    if manifest.format != MANIFEST_FORMAT:
        raise UnsupportedStoreVersionError(f"{path}: manifest format {manifest.format}")
    expected = schema_fingerprint(dataset_schema(event_type))
    if manifest.tag != event_tag(event_type) or manifest.schema_fingerprint != expected:
        raise SchemaMismatchError(f"{path}: manifest is for another tag or schema")
    return manifest


class DatasetWriter:
    """The single writer of one dataset (application-owned writer boundary)."""

    def __init__(self, root: Path, event_type: type[Event], config: StorageConfig) -> None:
        """Open the dataset of ``event_type`` under ``root``."""
        self._event_type = event_type
        self._schema = dataset_schema(event_type)
        self._config = config
        self.directory = root / event_tag(event_type)
        self._manifest = load_manifest(self.directory, event_type) or Manifest(
            format=MANIFEST_FORMAT,
            tag=event_tag(event_type),
            schema_fingerprint=schema_fingerprint(self._schema),
            version=0,
            next_sequence=0,
            head_hash=GENESIS.hex(),
            files=(),
        )
        self._pending: dict[tuple[str, str, int], list[dict[str, object]]] = {}
        self._buffered = 0

    @property
    def manifest(self) -> Manifest:
        """The last published manifest (version 0: nothing published yet)."""
        return self._manifest

    @property
    def buffered_rows(self) -> int:
        """Rows waiting in the buffer (never more than ``buffer_rows``)."""
        return self._buffered

    def add(self, record: LogRecord[Event]) -> bool:
        """Buffer ``record`` if it is of this dataset's type; return whether it was taken.

        The caller flushes with :meth:`flush` when :attr:`full` (``export`` does),
        so the buffer never holds more than ``buffer_rows`` rows.

        Raises:
            StorageError: the buffer is full.
        """
        event = record.event
        if type(event) is not self._event_type:
            return False
        if self.full:
            raise StorageError(f"{self.directory}: buffer full, flush before adding")
        row: dict[str, object] = {"sequence": record.sequence}
        for name in self._schema.names[1:]:
            row[name] = _column_value(getattr(event, name))
        self._pending.setdefault(_partition(event), []).append(row)
        self._buffered += 1
        return True

    @property
    def full(self) -> bool:
        """Whether the buffer reached its capacity."""
        return self._buffered >= self._config.buffer_rows

    def _write_file(self, key: tuple[str, str, int], table: pa.Table) -> FileEntry:
        """Write one new file; its name carries the manifest version that publishes it.

        A published file is never overwritten: names differ between versions
        even when a compaction produces the same sequence range again.
        """
        sequences = table.column("sequence")
        first, last = int(sequences[0].as_py()), int(sequences[-1].as_py())
        version = self._manifest.version + 1
        relative = f"{partition_path(*key)}/part-{first:020d}-{last:020d}-v{version}.parquet"
        target = self.directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.tmp")
        pq.write_table(table, temporary, row_group_size=self._config.rows_per_group)
        temporary.replace(target)
        return FileEntry(relative, table.num_rows, first, last, _sha256_file(target))

    def _table(self, rows: list[dict[str, object]]) -> pa.Table:
        try:
            return pa.Table.from_pylist(rows, schema=self._schema)
        except (pa.ArrowInvalid, pa.ArrowTypeError, OverflowError) as error:
            first = rows[0]["sequence"]
            raise EncodingError(
                f"{event_tag(self._event_type)} rows from sequence {first}: "
                f"value not representable exactly ({error})"
            ) from error

    def flush(self, next_sequence: int, head_hash: bytes) -> Manifest:
        """Write the buffered rows and publish a manifest covering the log up to ``next_sequence``.

        Args:
            next_sequence: First log sequence not covered after this flush.
            head_hash: Log hash of record ``next_sequence - 1``.

        Raises:
            EncodingError: a value is not representable exactly; nothing is published.
            ValueError: ``next_sequence`` moves backwards.
        """
        if next_sequence < self._manifest.next_sequence:
            raise ValueError("next_sequence must not move backwards")
        tables = {key: self._table(rows) for key, rows in sorted(self._pending.items())}
        added: list[FileEntry] = []
        for key, table in tables.items():
            for offset in range(0, table.num_rows, self._config.rows_per_file):
                chunk = table.slice(offset, self._config.rows_per_file)
                added.append(self._write_file(key, chunk))
        self._pending.clear()
        self._buffered = 0
        files = self._manifest.files + tuple(added)
        self._publish(files, next_sequence, head_hash)
        self._compact()
        return self._manifest

    def _partition_of(self, entry: FileEntry) -> str:
        return entry.path.rsplit("/", 1)[0]

    def _compact(self) -> None:
        groups: dict[str, list[FileEntry]] = {}
        for entry in self._manifest.files:
            groups.setdefault(self._partition_of(entry), []).append(entry)
        large = {k: v for k, v in groups.items() if len(v) > self._config.max_files_per_partition}
        if not large:
            return
        keep = [e for e in self._manifest.files if self._partition_of(e) not in large]
        merged: list[FileEntry] = []
        for directory, entries in sorted(large.items()):
            ordered = sorted(entries, key=lambda e: e.first_sequence)
            table = pa.concat_tables([pq.read_table(self.directory / e.path) for e in ordered])
            key = self._key_of(directory)
            for offset in range(0, table.num_rows, self._config.rows_per_file):
                merged.append(
                    self._write_file(key, table.slice(offset, self._config.rows_per_file))
                )
        files = tuple(
            sorted(keep + merged, key=lambda e: (self._partition_of(e), e.first_sequence))
        )
        self._publish(files, self._manifest.next_sequence, bytes.fromhex(self._manifest.head_hash))

    def _key_of(self, directory: str) -> tuple[str, str, int]:
        venue, symbol, year = (part.split("=", 1)[1] for part in directory.split("/"))
        return unquote(venue), unquote(symbol), int(year)

    def _publish(self, files: tuple[FileEntry, ...], next_sequence: int, head_hash: bytes) -> None:
        previous = tuple(e.path for e in self._manifest.files)
        manifest = msgspec.structs.replace(
            self._manifest,
            version=self._manifest.version + 1,
            next_sequence=next_sequence,
            head_hash=head_hash.hex(),
            files=files,
            previous_files=previous,
        )
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / MANIFEST
        temporary = path.with_name(f".{MANIFEST}.tmp")
        temporary.write_bytes(msgspec.json.format(msgspec.json.encode(manifest)))
        temporary.replace(path)
        self._manifest = manifest
        self._remove_unreferenced()

    def _remove_unreferenced(self) -> None:
        """Delete files listed in neither the current nor the previous snapshot."""
        referenced = {e.path for e in self._manifest.files} | set(self._manifest.previous_files)
        for path in self.directory.rglob("*.parquet"):
            if path.relative_to(self.directory).as_posix() not in referenced:
                path.unlink()
        for path in self.directory.rglob(".*.tmp"):
            path.unlink()


@dataclass(frozen=True)
class ExportResult:
    """Outcome of one export: rows written and the manifest published."""

    rows: int
    manifest: Manifest


def export(
    reader: EventLogReader,
    root: Path,
    event_types: Iterable[type[Event]],
    config: StorageConfig,
) -> dict[type[Event], ExportResult]:
    """Export new log records of each event type into its dataset under ``root``.

    Each dataset resumes from its manifest's ``next_sequence``. The buffer of a
    dataset is flushed whenever it is full and once at the end; every flush
    publishes a manifest, so a crash loses at most the unflushed rows, which
    the next export writes again.
    """
    results: dict[type[Event], ExportResult] = {}
    for event_type in event_types:
        writer = DatasetWriter(root, event_type, config)
        start = writer.manifest.next_sequence
        scanned = start
        rows = 0
        for record in reader.records(start):
            if writer.add(record):
                rows += 1
            scanned = record.sequence + 1
            if writer.full:
                writer.flush(scanned, reader.hash_at(scanned - 1))
        # Publish only a real change: a redundant manifest would shorten the
        # retention of the previous snapshot's files.
        if scanned > writer.manifest.next_sequence or writer.buffered_rows:
            writer.flush(scanned, reader.hash_at(scanned - 1))
        results[event_type] = ExportResult(rows, writer.manifest)
    return results
