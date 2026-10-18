# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Wall clock for the ingestion boundary only (ADR 0009).

Ruff rule TID251 bans wall-clock calls everywhere except this module and
``auqua.ingestion`` (``pyproject.toml``); decision, order, risk and
accounting code uses :class:`auqua.clock.log.LogClock`.
"""

import time


class WallClock:
    """System time in nanoseconds since the Unix epoch, UTC."""

    def now_ns(self) -> int:
        """Current system time."""
        return time.time_ns()
