# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Public baseline signal — walking skeleton (thin); owned and extended by BP9.

Moving-average crossover on bar closes: direction +1 while the fast simple
moving average is above the slow one, otherwise 0 (long or flat). The
comparison is exact: ``sum_fast * slow > sum_slow * fast`` needs no division.
The provider is deterministic and keeps only the last ``slow`` closes.
"""

from collections import deque
from decimal import Decimal

from auqua.contracts.events.market import Bar

PROVIDER = "baseline/ma-crossover"
PROVIDER_VERSION = "1"


class MovingAverageCrossover:
    """Long/flat crossover of two simple moving averages."""

    def __init__(self, fast: int, slow: int) -> None:
        """Use windows of ``fast`` and ``slow`` bars (``0 < fast < slow``)."""
        if not 0 < fast < slow:
            raise ValueError(f"windows must satisfy 0 < fast < slow, got {fast}, {slow}")
        self._fast, self._slow = fast, slow
        self._closes: deque[Decimal] = deque(maxlen=slow)

    def on_bar(self, bar: Bar) -> int | None:
        """Direction after ``bar`` (0 or 1), or ``None`` until ``slow`` bars were seen."""
        self._closes.append(bar.close)
        if len(self._closes) < self._slow:
            return None
        closes = list(self._closes)
        fast_sum = sum(closes[-self._fast :], Decimal(0))
        slow_sum = sum(closes, Decimal(0))
        return 1 if fast_sum * self._slow > slow_sum * self._fast else 0
