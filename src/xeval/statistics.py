"""Small, deterministic statistical helpers used by the scorecard.

The harness deliberately keeps this module dependency-free.  The probe suites are
small, so a transparent percentile bootstrap and a paired randomisation test are
preferable to pulling a large numerical stack into the runner.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import asdict, dataclass
from statistics import fmean

DEFAULT_CONFIDENCE_LEVEL = 0.95
DEFAULT_BOOTSTRAP_RESAMPLES = 10_000
DEFAULT_PERMUTATION_RESAMPLES = 20_000


@dataclass(frozen=True, slots=True)
class ConfidenceInterval:
    """A point estimate and its deterministic percentile-bootstrap interval."""

    estimate: float
    low: float
    high: float
    confidence_level: float
    n: int
    resamples: int
    seed: int
    method: str = "percentile-bootstrap"

    @property
    def lower(self) -> float:
        """Alias that is convenient for callers using scipy-like terminology."""

        return self.low

    @property
    def upper(self) -> float:
        """Alias that is convenient for callers using scipy-like terminology."""

        return self.high

    def as_dict(self) -> dict[str, float | int | str]:
        return asdict(self)

    def __iter__(self) -> Iterator[float]:
        """Allow ``low, high = interval`` without losing the point estimate."""

        yield self.low
        yield self.high


@dataclass(frozen=True, slots=True)
class PairedComparison:
    """Current-minus-previous result for observations matched by probe."""

    n: int
    current_mean: float
    previous_mean: float
    delta: float
    ci_low: float
    ci_high: float
    confidence_level: float
    p_value: float
    method: str
    seed: int
    bootstrap_resamples: int
    permutation_resamples: int
    wins: int
    losses: int
    ties: int

    @property
    def significant(self) -> bool:
        return self.p_value < round(1.0 - self.confidence_level, 12)

    @property
    def confidence_interval(self) -> tuple[float, float]:
        return (self.ci_low, self.ci_high)

    def as_dict(self) -> dict[str, float | int | str | bool]:
        result = asdict(self)
        result["significant"] = self.significant
        return result


def _finite_values(values: Iterable[float | int | bool], *, name: str) -> tuple[float, ...]:
    result = tuple(float(value) for value in values)
    if not result:
        raise ValueError(f"{name} must contain at least one observation")
    if not all(math.isfinite(value) for value in result):
        raise ValueError(f"{name} must contain only finite values")
    return result


def _validate_options(confidence_level: float, n_resamples: int, seed: int) -> None:
    if not 0.0 < confidence_level < 1.0:
        raise ValueError("confidence_level must be between 0 and 1")
    if n_resamples < 1:
        raise ValueError("n_resamples must be at least 1")
    # bool is an int in Python, but accepting True as a seed is almost certainly
    # an accidental configuration error.
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise TypeError("seed must be an explicit integer")


def _quantile(sorted_values: Sequence[float], probability: float) -> float:
    """Linearly interpolated quantile (the common R-7/numpy default rule)."""

    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * probability
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    if lower_index == upper_index:
        return sorted_values[lower_index]
    fraction = position - lower_index
    return sorted_values[lower_index] * (1.0 - fraction) + sorted_values[upper_index] * fraction


def bootstrap_mean_ci(
    values: Iterable[float | int | bool],
    *,
    seed: int,
    confidence_level: float = DEFAULT_CONFIDENCE_LEVEL,
    n_resamples: int = DEFAULT_BOOTSTRAP_RESAMPLES,
) -> ConfidenceInterval:
    """Return a deterministic percentile-bootstrap CI for an arithmetic mean.

    ``seed`` is keyword-only and has no default on purpose: reproducibility is a
    property of the run configuration, not process-global random state.
    """

    data = _finite_values(values, name="values")
    _validate_options(confidence_level, n_resamples, seed)
    estimate = fmean(data)
    if len(data) == 1 or min(data) == max(data):
        return ConfidenceInterval(
            estimate=estimate,
            low=estimate,
            high=estimate,
            confidence_level=confidence_level,
            n=len(data),
            resamples=n_resamples,
            seed=seed,
        )

    random_source = random.Random(seed)
    sample_size = len(data)
    estimates = [
        math.fsum(data[random_source.randrange(sample_size)] for _ in range(sample_size))
        / sample_size
        for _ in range(n_resamples)
    ]
    estimates.sort()
    alpha = 1.0 - confidence_level
    return ConfidenceInterval(
        estimate=estimate,
        low=_quantile(estimates, alpha / 2.0),
        high=_quantile(estimates, 1.0 - alpha / 2.0),
        confidence_level=confidence_level,
        n=sample_size,
        resamples=n_resamples,
        seed=seed,
    )


def bootstrap_rate_ci(
    outcomes: Iterable[bool | int | float],
    *,
    seed: int,
    confidence_level: float = DEFAULT_CONFIDENCE_LEVEL,
    n_resamples: int = DEFAULT_BOOTSTRAP_RESAMPLES,
) -> ConfidenceInterval:
    """Return a deterministic bootstrap CI for a binary success rate."""

    data = _finite_values(outcomes, name="outcomes")
    if any(value not in (0.0, 1.0) for value in data):
        raise ValueError("outcomes must contain only booleans or numeric 0/1 values")
    return bootstrap_mean_ci(
        data,
        seed=seed,
        confidence_level=confidence_level,
        n_resamples=n_resamples,
    )


def paired_comparison(
    current: Iterable[float | int | bool],
    previous: Iterable[float | int | bool],
    *,
    seed: int,
    confidence_level: float = DEFAULT_CONFIDENCE_LEVEL,
    n_resamples: int = DEFAULT_BOOTSTRAP_RESAMPLES,
    exact_max_pairs: int = 16,
    permutation_resamples: int = DEFAULT_PERMUTATION_RESAMPLES,
) -> PairedComparison:
    """Compare paired probe outcomes using a two-sided sign-flip test.

    The confidence interval bootstraps the paired differences.  The p-value is
    exact when there are at most ``exact_max_pairs`` non-zero differences; larger
    samples use a deterministically seeded Monte Carlo sign-flip test with the
    standard plus-one correction.  All-zero and other constant samples are
    handled without warnings or NaNs.
    """

    current_values = _finite_values(current, name="current")
    previous_values = _finite_values(previous, name="previous")
    if len(current_values) != len(previous_values):
        raise ValueError("current and previous must have the same number of observations")
    _validate_options(confidence_level, n_resamples, seed)
    if exact_max_pairs < 0:
        raise ValueError("exact_max_pairs must be non-negative")
    if permutation_resamples < 1:
        raise ValueError("permutation_resamples must be at least 1")

    differences = tuple(
        current_value - previous_value
        for current_value, previous_value in zip(current_values, previous_values, strict=True)
    )
    delta_ci = bootstrap_mean_ci(
        differences,
        seed=seed,
        confidence_level=confidence_level,
        n_resamples=n_resamples,
    )
    non_zero = tuple(value for value in differences if value != 0.0)
    observed = abs(math.fsum(non_zero))
    tolerance = max(1e-15, observed * 1e-12)

    if not non_zero:
        p_value = 1.0
        method = "constant-differences"
        permutations_used = 1
    elif len(non_zero) <= exact_max_pairs:
        total = 1 << len(non_zero)
        extreme = 0
        for mask in range(total):
            permuted_sum = math.fsum(
                value if mask & (1 << index) else -value for index, value in enumerate(non_zero)
            )
            if abs(permuted_sum) + tolerance >= observed:
                extreme += 1
        p_value = extreme / total
        method = "exact-paired-sign-flip"
        permutations_used = total
    else:
        # A separate stream prevents changes in the number of bootstrap draws from
        # silently changing a run's p-value.
        random_source = random.Random(seed ^ 0x9E3779B97F4A7C15)
        extreme = 0
        for _ in range(permutation_resamples):
            permuted_sum = math.fsum(
                value if random_source.getrandbits(1) else -value for value in non_zero
            )
            if abs(permuted_sum) + tolerance >= observed:
                extreme += 1
        p_value = (extreme + 1) / (permutation_resamples + 1)
        method = "monte-carlo-paired-sign-flip"
        permutations_used = permutation_resamples

    return PairedComparison(
        n=len(differences),
        current_mean=fmean(current_values),
        previous_mean=fmean(previous_values),
        delta=delta_ci.estimate,
        ci_low=delta_ci.low,
        ci_high=delta_ci.high,
        confidence_level=confidence_level,
        p_value=min(1.0, max(0.0, p_value)),
        method=method,
        seed=seed,
        bootstrap_resamples=n_resamples,
        permutation_resamples=permutations_used,
        wins=sum(value > 0.0 for value in differences),
        losses=sum(value < 0.0 for value in differences),
        ties=sum(value == 0.0 for value in differences),
    )


# Friendly aliases for callers that use "proportion" or a longer CI name.
bootstrap_proportion_ci = bootstrap_rate_ci
bootstrap_confidence_interval = bootstrap_mean_ci


__all__ = [
    "ConfidenceInterval",
    "PairedComparison",
    "bootstrap_confidence_interval",
    "bootstrap_mean_ci",
    "bootstrap_proportion_ci",
    "bootstrap_rate_ci",
    "paired_comparison",
]
