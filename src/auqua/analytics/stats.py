# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Research statistics (BP5 sections 4 and 4.3; ADR 0031).

Accounting is exact; statistics are estimates. :func:`returns` is the single
conversion boundary: per-period returns are formed in ``Decimal`` from exact
equity and converted to ``float`` once. Nothing computed here is written to
trading state. Standard library only (decision log a36).

- Sharpe ratio per period (not annualised) is what PSR, DSR and PBO use.
- :func:`annualised_sharpe` (reporting only): Lo (2002), "The Statistics of
  Sharpe Ratios", ``SR_q = eta(q) * SR``, ``eta(q) = q / sqrt(q + 2 *
  sum_{k=1..m} (q - k) * rho_k)``, with autocorrelations beyond the Newey-West
  (1994) bandwidth ``m = floor(4 * (T / 100) ** (2 / 9))`` taken as zero,
  because the full sum over ``q - 1`` lags is not estimable from short samples.
- :func:`psr`: probabilistic Sharpe ratio, ``Phi((SR - SR*) * sqrt(T - 1) /
  sqrt(1 - skew * SR + (kurt - 1) / 4 * SR^2))`` with raw (non-excess)
  kurtosis (Bailey & Lopez de Prado).
- :func:`expected_max_sharpe` and :func:`dsr`: the Deflated Sharpe Ratio is the
  PSR against the expected maximum of N trial Sharpe ratios (Bailey & Lopez de
  Prado 2014).
- :func:`pbo_cscv`: probability of backtest overfitting by combinatorially
  symmetric cross-validation (Bailey, Borwein, Lopez de Prado & Zhu).
"""

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from statistics import NormalDist

from auqua.contracts.numeric import arithmetic
from auqua.portfolio.series import SeriesError, simple_returns

EULER_MASCHERONI = 0.5772156649015329
_NORMAL = NormalDist()


class StatisticsError(ValueError):
    """Too few observations or degenerate input for a statistic."""


def returns(equity: Sequence[Decimal]) -> list[float]:
    """Per-period simple returns of an equity series: the Decimal-to-float boundary.

    Delegates to :func:`auqua.portfolio.series.simple_returns` (BP6 F-A: one
    implementation, shared with allocation).

    Raises:
        StatisticsError: fewer than two values or a non-positive equity.
    """
    try:
        return simple_returns(equity)
    except SeriesError as error:
        raise StatisticsError(str(error)) from error


@dataclass(frozen=True)
class Moments:
    """Sample size, mean, standard deviation, skewness and raw kurtosis."""

    n: int
    mean: float
    sd: float
    skew: float
    kurt: float


def moments(values: Sequence[float]) -> Moments:
    """Moments with population-normalised third and fourth moments (as in PSR).

    Raises:
        StatisticsError: fewer than two values or zero variance.
    """
    n = len(values)
    if n < 2:  # noqa: PLR2004
        raise StatisticsError("moments need at least two values")
    mean = math.fsum(values) / n
    m2 = math.fsum((v - mean) ** 2 for v in values) / n
    if m2 == 0:
        raise StatisticsError("zero variance")
    m3 = math.fsum((v - mean) ** 3 for v in values) / n
    m4 = math.fsum((v - mean) ** 4 for v in values) / n
    sd = math.sqrt(m2 * n / (n - 1))
    return Moments(n, mean, sd, m3 / m2**1.5, m4 / m2**2)


def sharpe(values: Sequence[float]) -> float:
    """Sharpe ratio per period (zero risk-free rate; not annualised)."""
    m = moments(values)
    return m.mean / m.sd


def autocorrelation(values: Sequence[float], lag: int) -> float:
    """Sample autocorrelation at ``lag`` (biased estimator, denominator n)."""
    n = len(values)
    if not 0 < lag < n:
        raise StatisticsError(f"lag {lag} outside 1..{n - 1}")
    mean = math.fsum(values) / n
    denominator = math.fsum((v - mean) ** 2 for v in values)
    if denominator == 0:
        raise StatisticsError("zero variance")
    numerator = math.fsum((values[i] - mean) * (values[i + lag] - mean) for i in range(n - lag))
    return numerator / denominator


def newey_west_lags(n: int) -> int:
    """Newey-West (1994) bandwidth ``floor(4 * (n / 100) ** (2 / 9))``."""
    if n <= 0:
        raise StatisticsError("n must be positive")
    return math.floor(4 * math.pow(n / 100, 2 / 9))


def lo_eta(q: int, rhos: Sequence[float]) -> float:
    """Lo's scale factor ``q / sqrt(q + 2 * sum_k (q - k) * rho_k)`` for ``k = 1..len(rhos)``."""
    if q <= 0:
        raise StatisticsError("q must be positive")
    inner = q + 2 * math.fsum((q - k) * rho for k, rho in enumerate(rhos[: q - 1], start=1))
    if inner <= 0:
        raise StatisticsError("non-positive variance ratio")
    return q / math.sqrt(inner)


def annualised_sharpe(daily: Sequence[float], periods_per_year: int) -> float:
    """Annualised Sharpe ratio of daily returns with Lo's serial-correlation adjustment."""
    lags = min(newey_west_lags(len(daily)), len(daily) - 1, periods_per_year - 1)
    rhos = [autocorrelation(daily, k) for k in range(1, lags + 1)]
    return lo_eta(periods_per_year, rhos) * sharpe(daily)


def psr(sr: float, sr_star: float, m: Moments) -> float:
    """Probabilistic Sharpe ratio of a per-period ``sr`` against ``sr_star``.

    Raises:
        StatisticsError: fewer than two observations or a non-positive variance term.
    """
    if m.n < 2:  # noqa: PLR2004
        raise StatisticsError("PSR needs at least two observations")
    variance = 1 - m.skew * sr + (m.kurt - 1) / 4 * sr**2
    if variance <= 0:
        raise StatisticsError("non-positive PSR variance term")
    return _NORMAL.cdf((sr - sr_star) * math.sqrt(m.n - 1) / math.sqrt(variance))


def expected_max_sharpe(variance_of_trials: float, trials: int) -> float:
    """Expected maximum Sharpe ratio of ``trials`` independent trials under the null.

    ``sqrt(V) * ((1 - g) * Z^-1(1 - 1/N) + g * Z^-1(1 - 1/(N e)))``; 0 for one trial.
    """
    if trials < 1:
        raise StatisticsError("trials must be >= 1")
    if trials == 1 or variance_of_trials <= 0:
        return 0.0
    g = EULER_MASCHERONI
    return math.sqrt(variance_of_trials) * (
        (1 - g) * _NORMAL.inv_cdf(1 - 1 / trials) + g * _NORMAL.inv_cdf(1 - 1 / (trials * math.e))
    )


def dsr(sr: float, m: Moments, variance_of_trials: float, trials: int) -> float:
    """Deflated Sharpe ratio: PSR against the expected maximum Sharpe ratio of the trials."""
    return psr(sr, expected_max_sharpe(variance_of_trials, trials), m)


def max_drawdown(equity: Sequence[Decimal]) -> Decimal:
    """Largest peak-to-trough fall as a fraction of the peak (exact)."""
    peak = Decimal(0)
    worst = Decimal(0)
    with arithmetic():
        for value in equity:
            peak = max(peak, value)
            if peak > 0:
                worst = max(worst, (peak - value) / peak)
    return worst


@dataclass(frozen=True)
class Pbo:
    """PBO estimate: share of non-positive logits, the logits, and the partition count."""

    probability: float
    logits: tuple[float, ...]
    groups: int


def pbo_cscv(matrix: Sequence[Sequence[float]], groups: int = 16) -> Pbo:
    """Probability of backtest overfitting by CSCV.

    ``matrix[t][n]`` is the return of trial ``n`` in period ``t``. Rows are cut
    into ``groups`` equal, contiguous blocks (a remainder of rows at the end is
    dropped); for every choice of half the blocks as in-sample, the trial with
    the best in-sample Sharpe ratio is ranked out of sample; ``w`` is its
    relative rank in (0, 1) and the logit ``ln(w / (1 - w))``; PBO is the share
    of logits <= 0.

    Raises:
        StatisticsError: fewer than two trials, an odd or too large ``groups``.
    """
    if groups < 2 or groups % 2:  # noqa: PLR2004
        raise StatisticsError("groups must be an even number >= 2")
    rows = len(matrix)
    trials = len(matrix[0]) if rows else 0
    if trials < 2:  # noqa: PLR2004
        raise StatisticsError("PBO needs at least two trials")
    size = rows // groups
    if size < 2:  # noqa: PLR2004
        raise StatisticsError(f"{rows} rows are too few for {groups} groups")
    # Sufficient statistics per (block, trial): the Sharpe ratio of any union
    # of blocks follows from counts, sums and sums of squares, so each of the
    # C(groups, groups/2) combinations costs O(groups * trials), not O(rows * trials).
    sums = [[0.0] * trials for _ in range(groups)]
    squares = [[0.0] * trials for _ in range(groups)]
    for g in range(groups):
        for r in range(g * size, (g + 1) * size):
            row = matrix[r]
            for n in range(trials):
                sums[g][n] += row[n]
                squares[g][n] += row[n] * row[n]
    every = range(groups)
    logits: list[float] = []
    for chosen in itertools.combinations(every, groups // 2):
        inside = set(chosen)
        is_scores = [_block_sharpe(sums, squares, inside, n, size) for n in range(trials)]
        best = max(range(trials), key=lambda n: (is_scores[n], -n))
        outside = {g for g in every if g not in inside}
        oos = [_block_sharpe(sums, squares, outside, n, size) for n in range(trials)]
        below = sum(1 for n in range(trials) if oos[n] < oos[best])
        ties = sum(1 for n in range(trials) if n != best and oos[n] == oos[best])
        rank = 1 + below + ties / 2
        w = rank / (trials + 1)
        logits.append(math.log(w / (1 - w)))
    probability = sum(1 for value in logits if value <= 0) / len(logits)
    return Pbo(probability, tuple(logits), groups)


def _block_sharpe(
    sums: list[list[float]], squares: list[list[float]], blocks: set[int], trial: int, size: int
) -> float:
    count = size * len(blocks)
    total = math.fsum(sums[g][trial] for g in blocks)
    total_sq = math.fsum(squares[g][trial] for g in blocks)
    variance = (total_sq - total * total / count) / (count - 1)
    if variance <= 0:
        return 0.0
    return (total / count) / math.sqrt(variance)
