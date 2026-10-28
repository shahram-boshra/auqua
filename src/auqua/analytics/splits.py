# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Validation splits (BP5 section 4.1; ADR 0031).

All splits work on observation indices ``0 .. n - 1`` in time order.

- :func:`walk_forward`: in-sample window followed by an out-of-sample step,
  rolling (fixed window) or anchored (expanding from the start).
- :func:`purged_kfold`: k contiguous test folds; training observations whose
  label interval ``[i, i + horizon]`` overlaps the test fold are purged, and
  ``embargo`` observations after each test fold are dropped from training
  (Lopez de Prado, *Advances in Financial Machine Learning*, 2018, ch. 7).
"""

from dataclasses import dataclass


class SplitError(ValueError):
    """Split parameters are inconsistent with the number of observations."""


@dataclass(frozen=True)
class Split:
    """Index ranges of one split: ``train`` and ``test`` (sorted, disjoint)."""

    train: tuple[int, ...]
    test: tuple[int, ...]


def walk_forward(n: int, window: int, step: int, *, anchored: bool = False) -> list[Split]:
    """Walk-forward splits over ``n`` observations.

    Raises:
        SplitError: non-positive sizes or no complete split.
    """
    if window <= 0 or step <= 0:
        raise SplitError("window and step must be positive")
    splits = []
    start = 0
    while start + window + step <= n:
        first = 0 if anchored else start
        train = tuple(range(first, start + window))
        test = tuple(range(start + window, start + window + step))
        splits.append(Split(train, test))
        start += step
    if not splits:
        raise SplitError(f"{n} observations hold no window of {window} plus a step of {step}")
    return splits


def purged_kfold(n: int, k: int, *, horizon: int = 0, embargo: int = 0) -> list[Split]:
    """K-fold splits with purging and embargo.

    Observation ``i`` has the label interval ``[i, i + horizon]``. A training
    observation is purged when that interval overlaps the test fold's interval
    ``[start, end + horizon]``; observations in ``(end, end + embargo]`` are
    embargoed.

    Raises:
        SplitError: ``k`` < 2, more folds than observations, or negative sizes.
    """
    if k < 2 or k > n:  # noqa: PLR2004 - two folds at least
        raise SplitError(f"k must be in 2..{n}")
    if horizon < 0 or embargo < 0:
        raise SplitError("horizon and embargo must be >= 0")
    bounds = [round(i * n / k) for i in range(k + 1)]
    splits = []
    for fold in range(k):
        start, end = bounds[fold], bounds[fold + 1] - 1
        test = tuple(range(start, end + 1))
        train = tuple(
            i
            for i in range(n)
            if (i + horizon < start or i > end + horizon) and not end < i <= end + embargo
        )
        splits.append(Split(train, test))
    return splits
