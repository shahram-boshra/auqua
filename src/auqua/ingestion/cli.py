# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Command line of the ingestion layer: ``python -m auqua.ingestion`` (BP4 section 6).

Runs on the operator's workstation against the environment's configuration
(``<config root>/<environment>/config.toml``) and writes only under its
``paths.data_root``: raw objects in ``raw/``, events in ``event_log.sqlite3``.
Nothing runs for a source that the configuration does not enable with the
current terms (BP4 section 4.2). ``runs`` prints the lineage records, which
are the V1 evidence recorded in the decision log (counts and hashes only).
"""

import argparse
import datetime
import os
import sys
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

from auqua.clock.wall import WallClock
from auqua.config.base import Environment
from auqua.config.errors import ConfigError
from auqua.config.loader import load_root
from auqua.config.root import AuquaConfig
from auqua.contracts.events.ingest import ChecksumResult, IngestRun
from auqua.contracts.events.reference import InstrumentDefined
from auqua.contracts.identity import InstrumentId
from auqua.contracts.instruments import CryptoSpot, Identifiers
from auqua.ingestion.errors import IngestionError, ParseError
from auqua.ingestion.fetch import Fetcher
from auqua.ingestion.parsers import ParseContext, ParserSpec, TimeUnit, frb_h10, ustreasury_par
from auqua.ingestion.parsers import binance_vision_klines as binance
from auqua.ingestion.parsers import coinbase_candles as coinbase
from auqua.ingestion.raw import RawFile, RawStore, read_single_member, verify_checksum_file
from auqua.ingestion.runner import IngestRunner, RawInput
from auqua.ingestion.sources import (
    BINANCE_VISION_KLINES,
    COINBASE_CANDLES,
    ENVIRONMENT_PURPOSE,
    FRB_H10,
    USTREASURY_PAR,
    SourceProfile,
    acknowledged,
    permit,
)
from auqua.storage.errors import StorageError
from auqua.storage.eventlog import EventLog
from auqua.storage.replay import EventLogReader

LOG_FILE = "event_log.sqlite3"
BINANCE_ARCHIVE = "https://data.binance.vision/data/spot/monthly/klines"
#: Binance interval names and their length in nanoseconds.
BINANCE_INTERVALS = {
    name: seconds * int(TimeUnit.SECONDS)
    for name, seconds in {
        "1m": 60,
        "3m": 180,
        "5m": 300,
        "15m": 900,
        "30m": 1800,
        "1h": 3600,
        "2h": 7200,
        "4h": 14400,
        "6h": 21600,
        "8h": 28800,
        "12h": 43200,
        "1d": 86400,
    }.items()
}
#: From this month Binance spot timestamps are microseconds (binance-public-data README).
BINANCE_MICROSECONDS_FROM = datetime.date(2025, 1, 1)


def _utc_s(text: str) -> int:
    """Seconds since the epoch of an ISO 8601 date (midnight UTC) or UTC date-time."""
    try:
        moment = datetime.datetime.fromisoformat(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"not ISO 8601: {text!r}") from error
    if moment.tzinfo is None:
        if len(text) != len("YYYY-MM-DD"):
            raise argparse.ArgumentTypeError(f"give a UTC offset, e.g. {text}Z")
        moment = moment.replace(tzinfo=datetime.UTC)
    return int(moment.timestamp())


class _Session:
    """The open log, reader, raw store, fetcher and runner of one invocation."""

    def __init__(self, config: AuquaConfig, environment: Environment) -> None:
        root = config.paths.data_root
        timeout = config.storage.busy_timeout_ms
        self.config = config
        self.environment = environment
        self.store = RawStore(root / "raw")
        self.fetcher = Fetcher(config.ingestion, self.store)
        self.log = EventLog(root / LOG_FILE, environment, busy_timeout_ms=timeout)
        self.reader = EventLogReader(root / LOG_FILE, environment, busy_timeout_ms=timeout)
        self.runner = IngestRunner(
            self.log, self.reader, config.ingestion, environment, WallClock()
        )

    def close(self) -> None:
        self.reader.close()
        self.log.close()

    def obtain(self, location: str, profile: SourceProfile) -> tuple[RawFile, str]:
        """Store ``location`` (https URL or local file) and return it with its retrieval text.

        The source must be enabled and permitted before anything is fetched.
        """
        acknowledged(profile, self.config.ingestion)
        permit(profile, ENVIRONMENT_PURPOSE[self.environment])
        if location.startswith("https://"):
            return self.fetcher.get(location, profile=profile.name), location
        path = Path(location).resolve()
        return self.store.put_file(path, max_bytes=self.config.ingestion.max_raw_bytes), str(path)

    def ingest(  # noqa: PLR0913 - one run's inputs
        self,
        profile: SourceProfile,
        parser: ParserSpec,
        raw: RawFile,
        retrieval: str,
        context: ParseContext,
        *,
        data: bytes | None = None,
        checksum: ChecksumResult = ChecksumResult.NOT_PUBLISHED,
    ) -> IngestRun:
        content = self.store.read(raw.sha256) if data is None else data
        raw_input = RawInput(raw, content, retrieval, checksum)
        run = self.runner.run(
            profile,
            parser,
            raw_input,
            lambda ingest_ns: _with_time(context, ingest_ns),
        )
        _print_run(run)
        return run


def _with_time(context: ParseContext, ingest_ns: int) -> ParseContext:
    return ParseContext(
        source=context.source,
        ingest_ns=ingest_ns,
        instrument=context.instrument,
        interval_ns=context.interval_ns,
        window=context.window,
        time_unit=context.time_unit,
        business_days=context.business_days,
    )


def _print_run(run: IngestRun) -> None:
    sys.stdout.write(
        f"{run.outcome.value:16} {run.profile:26} sha256={run.raw_sha256} "
        f"appended={run.appended} duplicates={run.duplicates} revisions={run.revisions} "
        f"gaps={run.gaps} unavailable={run.unavailable} checksum={run.checksum.value}"
        f"{' note=' + run.message if run.message else ''}\n"
    )


def _coinbase(session: _Session, args: argparse.Namespace) -> None:
    source = COINBASE_CANDLES.event_source
    raw, retrieval = session.obtain(coinbase.product_url(args.product), COINBASE_CANDLES)
    product_bytes = session.store.read(raw.sha256)
    defined = coinbase.parse_product(product_bytes, ParseContext(source=source, ingest_ns=0))
    event = defined.events[0]
    if not isinstance(event, InstrumentDefined):  # pragma: no cover - parse_product contract
        raise ParseError("product parser returned no instrument")
    instrument = event.instrument
    session.ingest(
        COINBASE_CANDLES,
        coinbase.PRODUCT_PARSER,
        raw,
        retrieval,
        ParseContext(source=source, ingest_ns=0),
    )
    step = args.granularity
    window = coinbase.MAX_WINDOW_BUCKETS * step
    start, end = args.start, args.end
    if (end - start) % step or end <= start:
        raise ParseError("--start/--end must span whole buckets of --granularity")
    while start < end:
        stop = min(start + window, end)
        raw, retrieval = session.obtain(
            coinbase.candles_url(args.product, step, start, stop), COINBASE_CANDLES
        )
        session.ingest(
            COINBASE_CANDLES,
            coinbase.PARSER,
            raw,
            retrieval,
            ParseContext(
                source=source,
                ingest_ns=0,
                instrument=instrument,
                interval_ns=step * int(TimeUnit.SECONDS),
                window=(start * int(TimeUnit.SECONDS), stop * int(TimeUnit.SECONDS)),
            ),
        )
        start = stop


def _binance(session: _Session, args: argparse.Namespace) -> None:
    month = datetime.date.fromisoformat(f"{args.month}-01")
    following = datetime.date(month.year + month.month // 12, month.month % 12 + 1, 1)
    name = f"{args.symbol}-{args.interval}-{args.month}"
    base = f"{BINANCE_ARCHIVE}/{args.symbol}/{args.interval}/{name}.zip"
    raw, retrieval = session.obtain(base, BINANCE_VISION_KLINES)
    checksum_raw, _ = session.obtain(base + ".CHECKSUM", BINANCE_VISION_KLINES)
    archive = session.store.read(raw.sha256)
    verify_checksum_file(
        archive, session.store.read(checksum_raw.sha256).decode("ascii"), f"{name}.zip"
    )
    data = read_single_member(
        archive, f"{name}.csv", max_bytes=session.config.ingestion.max_raw_bytes
    )
    instrument = CryptoSpot(
        id=InstrumentId("BINANCE", args.symbol),
        quote_currency=args.quote,
        price_increment=Decimal(args.price_increment),
        quantity_increment=Decimal(args.quantity_increment),
        multiplier=Decimal(1),
        valid_from_ns=0,
        knowledge_ns=0,
        identifiers=Identifiers(),
        base_currency=args.base,
    )
    second = int(TimeUnit.SECONDS)
    epoch = datetime.date(1970, 1, 1)
    session.ingest(
        BINANCE_VISION_KLINES,
        binance.PARSER,
        raw,
        retrieval,
        ParseContext(
            source=BINANCE_VISION_KLINES.event_source,
            ingest_ns=0,
            instrument=instrument,
            interval_ns=BINANCE_INTERVALS[args.interval],
            window=(
                (month - epoch).days * 86_400 * second,
                (following - epoch).days * 86_400 * second,
            ),
            time_unit=(
                TimeUnit.MICROSECONDS
                if month >= BINANCE_MICROSECONDS_FROM
                else TimeUnit.MILLISECONDS
            ),
        ),
        data=data,
        checksum=ChecksumResult.VERIFIED,
    )


def _reference(session: _Session, args: argparse.Namespace) -> None:
    profile, parser = (
        (FRB_H10, frb_h10.PARSER)
        if args.command == "h10"
        else (USTREASURY_PAR, ustreasury_par.PARSER)
    )
    raw, retrieval = session.obtain(args.location, profile)
    session.ingest(
        profile,
        parser,
        raw,
        retrieval,
        ParseContext(
            source=profile.event_source,
            ingest_ns=0,
            business_days=session.config.ingestion.treasury_available_after_business_days,
        ),
    )


def _runs(session: _Session, _: argparse.Namespace) -> None:
    for record in session.reader.records():
        if isinstance(record.event, IngestRun):
            _print_run(record.event)
    head = session.reader.verify()
    sys.stdout.write(f"log verified: next_sequence={head.next_sequence} head={head.hash.hex()}\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m auqua.ingestion", description=__doc__)
    parser.add_argument("--config-root", type=Path, required=True)
    parser.add_argument("--environment", type=Environment, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    cb = commands.add_parser("coinbase", help="Coinbase Exchange product and candles")
    cb.add_argument("product", help="product id, e.g. BTC-USD")
    cb.add_argument("--granularity", type=int, default=60, choices=sorted(coinbase.GRANULARITIES_S))
    cb.add_argument("--start", type=_utc_s, required=True, help="UTC, e.g. 2026-09-01")
    cb.add_argument("--end", type=_utc_s, required=True, help="UTC, exclusive")
    bn = commands.add_parser("binance", help="Binance Vision monthly spot klines (backtest only)")
    bn.add_argument("symbol", help="e.g. BTCUSDT")
    bn.add_argument("interval", choices=sorted(BINANCE_INTERVALS))
    bn.add_argument("month", help="YYYY-MM")
    bn.add_argument("--base", required=True)
    bn.add_argument("--quote", required=True)
    bn.add_argument("--price-increment", required=True)
    bn.add_argument("--quantity-increment", required=True)
    for name, text in (("h10", "FRB H.10 DDP CSV"), ("treasury", "Treasury par-yield CSV")):
        reference = commands.add_parser(name, help=text)
        reference.add_argument("location", help="https URL or local file")
    commands.add_parser("runs", help="print ingestion runs and verify the log")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command; return the process exit status."""
    args = _parser().parse_args(argv)
    try:
        config = load_root(args.environment, args.config_root, environ=os.environ)
        session = _Session(config, args.environment)
    except (ConfigError, StorageError, OSError) as error:
        sys.stderr.write(f"error: {type(error).__name__}: {error}\n")
        return 2
    handlers = {
        "coinbase": _coinbase,
        "binance": _binance,
        "h10": _reference,
        "treasury": _reference,
        "runs": _runs,
    }
    try:
        handlers[args.command](session, args)
    except IngestionError as error:
        sys.stderr.write(f"error: {type(error).__name__}: {error}\n")
        return 1
    finally:
        session.close()
    return 0
