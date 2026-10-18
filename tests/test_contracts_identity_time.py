# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Tests for identifiers, time fields and the clocks (BP1 X6, X7; ADR 0015)."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from auqua.clock.base import Clock
from auqua.clock.log import LogClock
from auqua.clock.wall import WallClock
from auqua.contracts.errors import InvalidFieldError
from auqua.contracts.identity import InstrumentId, new_id, require_source
from auqua.contracts.temporal import from_rfc3339, to_rfc3339

UUID_V7 = 7
BURST = 1000


def test_new_id_is_uuid7_and_ordered_within_a_burst() -> None:
    """UUIDv7 values generated in a burst are version 7 and strictly increasing."""
    ids = [new_id() for _ in range(BURST)]
    assert all(i.version == UUID_V7 for i in ids)
    assert ids == sorted(ids)
    assert len(set(ids)) == BURST


@pytest.mark.parametrize("source", ["ingest/binance/spot", "execution/sim", "a.b-c_d/e1"])
def test_valid_sources(source: str) -> None:
    """Lower-case path segments are accepted."""
    require_source(source)


@pytest.mark.parametrize("source", ["", "Ingest/x", "a//b", "/a", "a/", "a b"])
def test_invalid_sources(source: str) -> None:
    """Upper case, empty segments and whitespace are refused."""
    with pytest.raises(InvalidFieldError):
        require_source(source)


def test_instrument_id_text_form() -> None:
    """``VENUE:SYMBOL`` round-trips; malformed parts are refused."""
    iid = InstrumentId("BINANCE", "BTCUSDT")
    assert str(iid) == "BINANCE:BTCUSDT"
    assert InstrumentId.parse("BINANCE:BTCUSDT") == iid
    for bad in ("BINANCEBTCUSDT", "binance:BTCUSDT", "BINANCE:", "BINANCE:BTC USDT"):
        with pytest.raises(InvalidFieldError):
            InstrumentId.parse(bad)


@given(st.integers(min_value=0, max_value=2**63 - 1))
def test_rfc3339_round_trip(ns: int) -> None:
    """Formatting then parsing returns the same nanoseconds."""
    assert from_rfc3339(to_rfc3339(ns)) == ns


def test_rfc3339_parsing() -> None:
    """Offsets are applied; missing offsets and pre-epoch times are refused."""
    assert to_rfc3339(0) == "1970-01-01T00:00:00.000000000Z"
    half_second_ns = 500_000_000
    assert from_rfc3339("1970-01-01T01:00:00.5+01:00") == half_second_ns
    for bad in ("1970-01-01T00:00:00", "1969-12-31T23:59:59Z", "1970-13-01T00:00:00Z"):
        with pytest.raises(InvalidFieldError):
            from_rfc3339(bad)


def test_log_clock_moves_only_forward_from_records() -> None:
    """The log clock follows record times and ignores earlier ones (counted)."""
    clock = LogClock()
    assert isinstance(clock, Clock)
    assert [clock.advance(t) for t in (10, 20, 15, 20, 30)] == [True, True, False, True, True]
    assert clock.now_ns() == 30  # noqa: PLR2004
    assert clock.regressions == 1


def test_log_clock_replay_is_identical() -> None:
    """Applying the same record times twice yields the same sequence of now_ns()."""
    times = [5, 7, 7, 3, 11]

    def run() -> list[int]:
        clock = LogClock()
        seen = []
        for t in times:
            clock.advance(t)
            seen.append(clock.now_ns())
        return seen

    assert run() == run() == [5, 7, 7, 7, 11]


def test_log_clock_refuses_negative_start() -> None:
    """Times before the epoch are refused."""
    with pytest.raises(ValueError, match="start_ns"):
        LogClock(-1)


def test_wall_clock_satisfies_the_protocol() -> None:
    """The wall clock returns positive nanoseconds."""
    clock = WallClock()
    assert isinstance(clock, Clock)
    assert clock.now_ns() > 0
