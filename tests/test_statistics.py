from __future__ import annotations

import pytest

from xeval.statistics import bootstrap_mean_ci, bootstrap_rate_ci, paired_comparison


def test_bootstrap_is_deterministic_for_an_explicit_seed() -> None:
    values = [0.1, 0.2, 0.5, 0.9, 1.0]
    first = bootstrap_mean_ci(values, seed=1234, n_resamples=500)
    second = bootstrap_mean_ci(values, seed=1234, n_resamples=500)

    assert first == second
    assert first.low <= first.estimate <= first.high
    assert first.seed == 1234


def test_seed_is_required_and_rates_are_binary() -> None:
    with pytest.raises(TypeError):
        bootstrap_mean_ci([1, 2, 3])  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="0/1"):
        bootstrap_rate_ci([0, 0.5, 1], seed=1)


def test_constant_and_tiny_paired_samples_are_well_defined() -> None:
    unchanged = paired_comparison([1, 1], [1, 1], seed=7, n_resamples=100)
    tiny = paired_comparison([1], [0], seed=7, n_resamples=100)

    assert unchanged.delta == 0
    assert unchanged.confidence_interval == (0, 0)
    assert unchanged.p_value == 1
    assert unchanged.method == "constant-differences"
    assert tiny.p_value == 1


def test_small_paired_test_uses_exact_two_sided_sign_flips() -> None:
    comparison = paired_comparison(
        [1, 1, 1, 1],
        [0, 0, 0, 0],
        seed=9,
        n_resamples=100,
    )

    assert comparison.method == "exact-paired-sign-flip"
    assert comparison.permutation_resamples == 16
    assert comparison.p_value == pytest.approx(0.125)
    assert comparison.wins == 4


def test_large_paired_test_uses_seeded_monte_carlo() -> None:
    current = [float(index % 3) for index in range(20)]
    previous = [0.0] * 20
    first = paired_comparison(
        current,
        previous,
        seed=2026,
        n_resamples=100,
        exact_max_pairs=4,
        permutation_resamples=250,
    )
    second = paired_comparison(
        current,
        previous,
        seed=2026,
        n_resamples=100,
        exact_max_pairs=4,
        permutation_resamples=250,
    )

    assert first == second
    assert first.method == "monte-carlo-paired-sign-flip"
    assert 0 < first.p_value <= 1
