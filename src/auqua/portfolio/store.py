# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""The portfolio projection in the transactional store (BP6 section 4.10; amendment 8; T19).

The projection is derived state: every piece of the fold's state
(:class:`~auqua.portfolio.account.PortfolioState`) in typed STRICT tables,
stamped with the log position it reflects (``next_sequence`` and the chain
hash of the last record) and with the SHA-256 of its own contents. Decimals are
stored as their exact text, ``str(value)`` — the event codec's encoding
(ADR 0014) — never as scaled integers: the fold's values have no fixed scale
(amendment 8, F-S; ADR 0034 records the exception to ADR 0023). Postings are
stored with the balances; the balances must equal their sums (F-U).

At start the projection is valid only if (BP6 C8, D5): its schema, the
configuration identity (ADR 0020 ``config_id``) and the code identifier it was
folded with are the current ones — the code identifier is passed in by the
caller (BP5 ``code_id``; the portfolio does not import the analytics, contract
8), because the table layout's version does not cover the fold's semantics
(amendment 9); its ``next_sequence``
is at most the log's head; its hash is the log's hash at that position; its
contents hash to the stored SHA-256; every decimal is canonical text; the
balances equal the sums of the postings and the positions those of the lots;
and the state restores. Otherwise it is discarded and the account is rebuilt
from sequence 0. The content hash detects corruption only; tamper evidence is
the log's hash chain.

On resume, the instrument index is rebuilt from the log's ``InstrumentDefined``
records before ``next_sequence`` only (amendment 8, F-T): a version appended
later, even one "known" earlier, cannot change state already folded.
"""

import datetime
import hashlib
import sqlite3
import uuid
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from typing import Any

import msgspec

from auqua.config.identity import config_id
from auqua.config.portfolio import PortfolioConfig
from auqua.contracts.codec import CANONICAL, encode
from auqua.contracts.errors import EventDecodeError
from auqua.contracts.events.account import ReconciliationResult
from auqua.contracts.events.base import Event
from auqua.contracts.events.lifecycle import CorporateActionV2
from auqua.contracts.events.reference import InstrumentDefined
from auqua.contracts.events.trading import OrderSubmitted, Side
from auqua.contracts.identity import InstrumentId
from auqua.contracts.numeric import exact
from auqua.contracts.portfolio import Balance, LogPosition, Posting, PostingKind
from auqua.portfolio.account import (
    FutureRecord,
    LotsRecord,
    Portfolio,
    PortfolioState,
    Scheduled,
)
from auqua.portfolio.effects import PendingFx
from auqua.portfolio.errors import PortfolioError
from auqua.portfolio.ledger import Ledger
from auqua.portfolio.lots import Lot
from auqua.portfolio.reconcile import Reconciler
from auqua.storage.chain import GENESIS
from auqua.storage.errors import StorageError
from auqua.storage.marketview import MarketView
from auqua.storage.reference import ReferenceIndex
from auqua.storage.replay import EventLogReader
from auqua.storage.store import StoreMigrations

#: Version of the projection's tables and their meaning; another value → rebuild.
PROJECTION_SCHEMA = "1"

_TABLES: tuple[tuple[str, str, str], ...] = (
    # (name, columns, order of the canonical content hash)
    ("portfolio_meta", "key TEXT PRIMARY KEY, value TEXT NOT NULL", "key"),
    (
        "portfolio_postings",
        "position INTEGER PRIMARY KEY, account TEXT NOT NULL, currency TEXT NOT NULL, "
        "amount TEXT NOT NULL, kind TEXT NOT NULL, cause_id TEXT NOT NULL",
        "position",
    ),
    (
        "portfolio_balances",
        "currency TEXT PRIMARY KEY, cash TEXT NOT NULL, receivable TEXT NOT NULL",
        "currency",
    ),
    (
        "portfolio_lot_books",
        "instrument TEXT PRIMARY KEY, increment TEXT NOT NULL, direction INTEGER NOT NULL",
        "instrument",
    ),
    (
        "portfolio_lots",
        "instrument TEXT NOT NULL, position INTEGER NOT NULL, quantity TEXT NOT NULL, "
        "total_cost TEXT NOT NULL, unit_cost TEXT, PRIMARY KEY (instrument, position)",
        "instrument, position",
    ),
    ("portfolio_positions", "instrument TEXT PRIMARY KEY, quantity TEXT NOT NULL", "instrument"),
    (
        "portfolio_futures",
        "instrument TEXT PRIMARY KEY, settled_quantity TEXT NOT NULL, last_date TEXT, "
        "last_price TEXT, last_available_ns INTEGER",
        "instrument",
    ),
    (
        "portfolio_future_trades",
        "instrument TEXT NOT NULL, position INTEGER NOT NULL, ts_event_ns INTEGER NOT NULL, "
        "quantity TEXT NOT NULL, price TEXT NOT NULL, PRIMARY KEY (instrument, position)",
        "instrument, position",
    ),
    ("portfolio_submitted", "position INTEGER PRIMARY KEY, event BLOB NOT NULL", "position"),
    (
        "portfolio_working",
        "position INTEGER PRIMARY KEY, order_id TEXT NOT NULL, instrument TEXT NOT NULL, "
        "quantity TEXT NOT NULL",
        "position",
    ),
    (
        "portfolio_pending_fx",
        "position INTEGER PRIMARY KEY, fill_id TEXT NOT NULL, instrument TEXT NOT NULL, "
        "side TEXT NOT NULL, base_amount TEXT NOT NULL, quote_amount TEXT NOT NULL, "
        "ts_event_ns INTEGER NOT NULL",
        "position",
    ),
    (
        "portfolio_scheduled",
        "position INTEGER PRIMARY KEY, at_ns INTEGER NOT NULL, ordinal INTEGER NOT NULL, "
        "stage TEXT NOT NULL, entitled TEXT NOT NULL, action BLOB NOT NULL",
        "position",
    ),
    ("portfolio_delisted", "instrument TEXT PRIMARY KEY", "instrument"),
    ("portfolio_results", "position INTEGER PRIMARY KEY, result BLOB NOT NULL", "position"),
    ("portfolio_open_breaks", "result_id TEXT PRIMARY KEY", "result_id"),
    ("portfolio_derived", "position INTEGER PRIMARY KEY, event BLOB NOT NULL", "position"),
)


def _create_tables(connection: sqlite3.Connection) -> None:
    for name, columns, _ in _TABLES:
        connection.execute(f"CREATE TABLE {name} ({columns}) STRICT")


#: The transactional-store migrations of the portfolio projection (BP3 ADR 0023).
MIGRATIONS = StoreMigrations(current=1)
MIGRATIONS.register(0, _create_tables)


class ProjectionInvalidError(PortfolioError):
    """The stored projection cannot be used; the account is rebuilt from sequence 0."""


# --- encoding -----------------------------------------------------------------------------------
def _text(value: Decimal) -> str:
    return str(value)


def _decimal(text: str) -> Decimal:
    """Parse exact text; refuse anything that is not its own canonical form (F-S)."""
    try:
        value = Decimal(text)
    except InvalidOperation as error:
        raise ProjectionInvalidError(f"not a decimal: {text!r}") from error
    if not value.is_finite() or str(value) != text:
        raise ProjectionInvalidError(f"not canonical decimal text: {text!r}")
    return value


def _event[E: Event](data: bytes, kind: type[E]) -> E:
    try:
        event = CANONICAL.decode(data)
    except (EventDecodeError, msgspec.ValidationError) as error:
        raise ProjectionInvalidError("a stored event does not decode") from error
    if not isinstance(event, kind) or encode(event) != data:
        raise ProjectionInvalidError(f"a stored {kind.__name__} is not canonical")
    return event


def _meta_rows(
    state: PortfolioState, config: PortfolioConfig, code_id: str
) -> list[tuple[str, str]]:
    """The stamp: schema, configuration identity, code identifier, log position and clock."""
    return [
        ("projection_schema", PROJECTION_SCHEMA),
        ("config_id", config_id(config)),
        ("code_id", code_id),
        ("next_sequence", str(state.log_position.next_sequence)),
        ("hash", state.log_position.hash),
        ("clock_ns", str(state.clock_ns)),
        ("order", str(state.order)),
    ]


def _rows(state: PortfolioState, config: PortfolioConfig) -> dict[str, list[tuple[Any, ...]]]:
    """The state tables (everything but the meta stamp)."""
    ledger = Ledger.from_postings(config.account_id, state.postings)
    rows: dict[str, list[tuple[Any, ...]]] = {
        "portfolio_postings": [
            (n, p.account, p.currency, _text(p.amount), p.kind.value, str(p.cause_id))
            for n, p in enumerate(state.postings)
        ],
        "portfolio_balances": [
            (b.currency, _text(b.cash), _text(b.receivable)) for b in ledger.balances()
        ],
        "portfolio_lot_books": [
            (str(r.instrument_id), _text(r.increment), r.direction) for r in state.lots
        ],
        "portfolio_lots": [
            (
                str(r.instrument_id),
                n,
                _text(lot.quantity),
                _text(lot.total_cost),
                None if lot.unit_cost is None else _text(lot.unit_cost),
            )
            for r in state.lots
            for n, lot in enumerate(r.lots)
        ],
        "portfolio_positions": [(str(r.instrument_id), _text(_position(r))) for r in state.lots],
        "portfolio_futures": [
            (
                str(r.instrument_id),
                _text(r.settled_quantity),
                None if r.last_date is None else r.last_date.isoformat(),
                None if r.last_price is None else _text(r.last_price),
                r.last_available_ns,
            )
            for r in state.futures
        ],
        "portfolio_future_trades": [
            (str(r.instrument_id), n, ts, _text(q), _text(p))
            for r in state.futures
            for n, (ts, q, p) in enumerate(r.trades)
        ],
        "portfolio_submitted": [(n, encode(o)) for n, o in enumerate(state.submitted)],
        "portfolio_working": [
            (n, str(o), str(i), _text(q)) for n, (o, i, q) in enumerate(state.working)
        ],
        "portfolio_pending_fx": [
            (
                n,
                str(p.fill_id),
                str(p.instrument_id),
                p.side.value,
                _text(p.base_amount),
                _text(p.quote_amount),
                p.ts_event_ns,
            )
            for n, p in enumerate(state.pending_fx)
        ],
        "portfolio_scheduled": [
            (n, s.at_ns, s.order, s.stage, _text(s.entitled), encode(s.action))
            for n, s in enumerate(state.scheduled)
        ],
        "portfolio_delisted": [(str(i),) for i in state.delisted],
        "portfolio_results": [(n, encode(r)) for n, r in enumerate(state.results)],
        "portfolio_open_breaks": [(str(r.id),) for r in _open_breaks(state.results)],
        "portfolio_derived": [(n, encode(e)) for n, e in enumerate(state.derived)],
    }
    return rows


def _position(record: LotsRecord) -> Decimal:
    with exact():
        return sum((lot.quantity for lot in record.lots), Decimal(0)) * record.direction


def _open_breaks(results: Iterable[ReconciliationResult]) -> list[ReconciliationResult]:
    return sorted(Reconciler(results).open_breaks, key=lambda r: str(r.id))


def content_sha256(connection: sqlite3.Connection) -> str:
    """SHA-256 over every projection table except the hash itself, in a canonical order."""
    digest = hashlib.sha256()
    for name, _, order in _TABLES:
        where = " WHERE key != 'content_sha256'" if name == "portfolio_meta" else ""
        rows = connection.execute(f"SELECT * FROM {name}{where} ORDER BY {order}").fetchall()  # noqa: S608 - fixed table names
        encoded = [[v.hex() if isinstance(v, bytes) else v for v in row] for row in rows]
        digest.update(msgspec.json.encode([name, encoded]))
    return digest.hexdigest()


# --- save ---------------------------------------------------------------------------------------
def save(
    connection: sqlite3.Connection,
    portfolio: Portfolio,
    config: PortfolioConfig,
    *,
    code_id: str,
) -> None:
    """Replace the projection with ``portfolio``'s state, in one transaction.

    ``code_id`` identifies the code that folded it (BP5 ``analytics.lineage.code_id``).
    """
    state = portfolio.state()
    rows: dict[str, list[tuple[Any, ...]]] = {
        "portfolio_meta": list(_meta_rows(state, config, code_id)),
        **_rows(state, config),
    }
    connection.execute("BEGIN IMMEDIATE")
    try:
        for name, _, _ in _TABLES:
            connection.execute(f"DELETE FROM {name}")  # noqa: S608 - fixed table names
            table = rows[name]
            if table:
                marks = ", ".join("?" * len(table[0]))
                connection.executemany(f"INSERT INTO {name} VALUES ({marks})", table)  # noqa: S608
        connection.execute(
            "INSERT INTO portfolio_meta VALUES ('content_sha256', ?)", (content_sha256(connection),)
        )
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


# --- load ---------------------------------------------------------------------------------------
def _meta(connection: sqlite3.Connection) -> dict[str, str]:
    return dict(connection.execute("SELECT key, value FROM portfolio_meta").fetchall())


def _state(connection: sqlite3.Connection, meta: dict[str, str]) -> PortfolioState:
    def table(name: str, order: str) -> list[tuple[Any, ...]]:
        return connection.execute(f"SELECT * FROM {name} ORDER BY {order}").fetchall()  # noqa: S608

    postings = tuple(
        Posting(account, currency, _decimal(amount), PostingKind(kind), uuid.UUID(cause))
        for _, account, currency, amount, kind, cause in table("portfolio_postings", "position")
    )
    lots: dict[str, list[Lot]] = {}
    for instrument, _, quantity, total, unit in table("portfolio_lots", "instrument, position"):
        lots.setdefault(instrument, []).append(
            Lot(_decimal(quantity), _decimal(total), None if unit is None else _decimal(unit))
        )
    books = tuple(
        LotsRecord(InstrumentId.parse(i), _decimal(increment), direction, tuple(lots.pop(i, [])))
        for i, increment, direction in table("portfolio_lot_books", "instrument")
    )
    if lots:
        raise ProjectionInvalidError("lots without their book")
    trades: dict[str, list[tuple[int, Decimal, Decimal]]] = {}
    for i, _, ts, q, p in table("portfolio_future_trades", "instrument, position"):
        trades.setdefault(i, []).append((ts, _decimal(q), _decimal(p)))
    futures = tuple(
        FutureRecord(
            InstrumentId.parse(i),
            _decimal(settled),
            None if day is None else datetime.date.fromisoformat(day),
            None if price is None else _decimal(price),
            available,
            tuple(trades.pop(i, [])),
        )
        for i, settled, day, price, available in table("portfolio_futures", "instrument")
    )
    if trades:
        raise ProjectionInvalidError("future trades without their future")
    return PortfolioState(
        clock_ns=int(meta["clock_ns"]),
        log_position=LogPosition(int(meta["next_sequence"]), meta["hash"]),
        order=int(meta["order"]),
        postings=postings,
        lots=books,
        futures=futures,
        submitted=tuple(
            _event(e, OrderSubmitted) for _, e in table("portfolio_submitted", "position")
        ),
        working=tuple(
            (uuid.UUID(o), InstrumentId.parse(i), _decimal(q))
            for _, o, i, q in table("portfolio_working", "position")
        ),
        pending_fx=tuple(
            PendingFx(
                uuid.UUID(fill),
                InstrumentId.parse(i),
                Side(side),
                _decimal(base),
                _decimal(quote),
                ts,
            )
            for _, fill, i, side, base, quote, ts in table("portfolio_pending_fx", "position")
        ),
        scheduled=tuple(
            Scheduled(at, ordinal, _event(action, CorporateActionV2), stage, _decimal(entitled))
            for _, at, ordinal, stage, entitled, action in table("portfolio_scheduled", "position")
        ),
        delisted=tuple(InstrumentId.parse(i) for (i,) in table("portfolio_delisted", "instrument")),
        results=tuple(
            _event(r, ReconciliationResult) for _, r in table("portfolio_results", "position")
        ),
        derived=tuple(_event(e, Event) for _, e in table("portfolio_derived", "position")),
    )


def _check_derived_tables(
    connection: sqlite3.Connection, state: PortfolioState, config: PortfolioConfig
) -> None:
    """Stored balances, positions and open breaks must equal what the state implies (F-U)."""
    expected = _rows(state, config)
    for name in ("portfolio_balances", "portfolio_positions", "portfolio_open_breaks"):
        stored = connection.execute(f"SELECT * FROM {name} ORDER BY 1").fetchall()  # noqa: S608
        if stored != sorted(expected[name]):
            raise ProjectionInvalidError(f"{name} disagrees with the state it summarizes")
    sums = {
        b.currency: b for b in Ledger.from_postings(config.account_id, state.postings).balances()
    }
    for currency, cash, receivable in connection.execute("SELECT * FROM portfolio_balances"):
        if sums.get(currency) != Balance(currency, _decimal(cash), _decimal(receivable)):
            raise ProjectionInvalidError(f"balance of {currency} is not the sum of its postings")


def _instruments_before(reader: EventLogReader, next_sequence: int) -> ReferenceIndex:
    """The instrument index from the log's records before ``next_sequence`` only (F-T)."""
    index = ReferenceIndex()
    if next_sequence == 0:
        return index
    for record in reader.records():
        if record.sequence >= next_sequence:
            break
        if isinstance(record.event, InstrumentDefined):
            index.apply(record)
    return index


def load(
    connection: sqlite3.Connection,
    reader: EventLogReader,
    config: PortfolioConfig,
    market: MarketView,
    *,
    code_id: str,
) -> Portfolio:
    """The account from a valid projection, or ``ProjectionInvalidError`` (rebuild from 0).

    Raises:
        ProjectionInvalidError: the projection is missing, of another schema,
            configuration or code, ahead of the log, not on its chain, corrupted,
            or inconsistent.
    """
    meta = _meta(connection)
    if not meta:
        raise ProjectionInvalidError("no projection")
    if meta.get("projection_schema") != PROJECTION_SCHEMA:
        raise ProjectionInvalidError(f"projection schema {meta.get('projection_schema')}")
    if meta.get("config_id") != config_id(config):
        raise ProjectionInvalidError("folded with another configuration")
    if meta.get("code_id") != code_id:
        raise ProjectionInvalidError("folded by other code")
    if meta.get("content_sha256") != content_sha256(connection):
        raise ProjectionInvalidError("contents do not match their SHA-256")
    try:
        state = _state(connection, meta)
        _check_derived_tables(connection, state, config)
    except ProjectionInvalidError:
        raise
    except (KeyError, ValueError, PortfolioError) as error:
        raise ProjectionInvalidError(f"unreadable projection: {error}") from error
    position = state.log_position
    head = reader.head()
    if position.next_sequence > head.next_sequence:
        raise ProjectionInvalidError("projection is ahead of the log")
    try:
        expected = (
            GENESIS if position.next_sequence == 0 else reader.hash_at(position.next_sequence - 1)
        )
    except StorageError as error:
        raise ProjectionInvalidError(str(error)) from error
    if expected.hex() != position.hash:
        raise ProjectionInvalidError("projection is not on the log's chain")
    reference = _instruments_before(reader, position.next_sequence)
    try:
        return Portfolio.restore(config, market, reference, state)
    except PortfolioError as error:
        raise ProjectionInvalidError(f"state does not restore: {error}") from error


def catch_up(
    connection: sqlite3.Connection,
    reader: EventLogReader,
    config: PortfolioConfig,
    market: MarketView,
    *,
    code_id: str,
) -> tuple[Portfolio, tuple[Event, ...], bool]:
    """Fold the log into the projection: from the stored position, or from 0 when invalid.

    ``code_id`` identifies the running code (the caller computes it with
    ``analytics.lineage.code_id``). Returns the account, the events derived
    while folding (for the caller to append to the log) and whether the
    projection was rebuilt from 0.
    """
    try:
        portfolio = load(connection, reader, config, market, code_id=code_id)
        rebuilt = False
    except ProjectionInvalidError:
        portfolio, rebuilt = Portfolio(config, market), True
    derived: list[Event] = []
    for record, digest in reader.records_with_hashes(portfolio.log_position.next_sequence):
        derived += portfolio.apply(record, digest)
    save(connection, portfolio, config, code_id=code_id)
    return portfolio, tuple(derived), rebuilt
