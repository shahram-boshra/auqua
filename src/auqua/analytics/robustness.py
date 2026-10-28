# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Robustness sweeps and robust ranking (BP5 section 4, R-6; ADR 0032).

Each configuration is re-simulated under perturbed assumptions and evaluated
on the validation period: costs x0.5 and x2, one more bar of latency, and a
seeded data perturbation (a share of bars dropped); the validation period is
also scored by halves (sub-periods), and the neighbouring configurations of
the grid are compared. A configuration is **robust** when every sweep keeps
the sign of its validation Sharpe ratio and its Deflated Sharpe Ratio meets
the threshold; ranking uses the worst Sharpe ratio across sweeps, so a robust
configuration with a lower peak ranks above a fragile one (Strategy Module 3:
"rank robust solutions rather than simply the highest historical objective").
"""

import random
from collections.abc import Sequence
from dataclasses import dataclass

from auqua.contracts.events.market import Bar

COST_LOW = "costs_x0.5"
COST_HIGH = "costs_x2"
LATENCY = "latency_plus_1_bar"
PERTURBED = "data_drop"
FIRST_HALF = "validation_first_half"
SECOND_HALF = "validation_second_half"
NEIGHBOURS = "grid_neighbours"


def drop_bars(bars: Sequence[Bar], share: float, seed: int) -> list[Bar]:
    """``bars`` without a seeded random ``share`` of them (the first bar is kept)."""
    rng = random.Random(seed)  # noqa: S311 - a reproducible perturbation, not security
    return [bar for index, bar in enumerate(bars) if index == 0 or rng.random() >= share]


def neighbours(
    configuration: tuple[int, int], grid: Sequence[tuple[int, int]]
) -> list[tuple[int, int]]:
    """Grid configurations one step away in ``fast`` or ``slow`` (same other parameter)."""
    fasts = sorted({f for f, _ in grid})
    slows = sorted({s for _, s in grid})
    fast, slow = configuration
    out = []
    for values, index, make in (
        (fasts, fasts.index(fast), lambda v: (v, slow)),
        (slows, slows.index(slow), lambda v: (fast, v)),
    ):
        for step in (-1, 1):
            position = index + step
            if 0 <= position < len(values):
                candidate = make(values[position])
                if candidate in grid:
                    out.append(candidate)
    return sorted(set(out))


@dataclass(frozen=True)
class Robustness:
    """Sweep results of one configuration and the verdict."""

    base: float
    sweeps: dict[str, float]
    dsr_ok: bool

    @property
    def robust(self) -> bool:
        """Positive base, every sweep positive too, and the DSR meets the threshold."""
        return self.base > 0 and all(value > 0 for value in self.sweeps.values()) and self.dsr_ok

    @property
    def score(self) -> float:
        """The worst Sharpe ratio across the base and the sweeps."""
        return min([self.base, *self.sweeps.values()])
