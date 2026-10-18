# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Clock driven by the sequenced event log (ADR 0009).

The same clock serves live operation and replay: it moves only when a log
record is applied, so replaying the same records yields the same times.
"""


class LogClock:
    """Time of the last applied log record; never moves backwards."""

    def __init__(self, start_ns: int = 0) -> None:
        """Start at ``start_ns`` (nanoseconds since the Unix epoch).

        Raises:
            ValueError: ``start_ns`` is negative.
        """
        if start_ns < 0:
            raise ValueError(f"start_ns must be >= 0, got {start_ns}")
        self._now_ns = start_ns
        self._regressions = 0

    def now_ns(self) -> int:
        """Time of the last applied record (or the start time)."""
        return self._now_ns

    @property
    def regressions(self) -> int:
        """Number of applied record times earlier than the clock (left unapplied)."""
        return self._regressions

    def advance(self, record_ns: int) -> bool:
        """Apply the time of the next log record.

        Returns:
            ``True`` if the clock moved forward or stayed equal; ``False`` if
            ``record_ns`` was earlier than the clock, which is counted and ignored.
        """
        if record_ns < self._now_ns:
            self._regressions += 1
            return False
        self._now_ns = record_ns
        return True
