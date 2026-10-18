# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Clock protocol (ADR 0009, ADR 0015)."""

from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Source of the current time in nanoseconds since the Unix epoch, UTC."""

    def now_ns(self) -> int:
        """Current time."""
        ...
