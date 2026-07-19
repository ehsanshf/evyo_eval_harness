"""Deterministic threshold and prior-run regression decisions."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from statistics import fmean
from typing import Any

from .models import ProbeResult
from .statistics import paired_comparison
from .storage import ProbeResultRecord


@dataclass(frozen=True, slots=True)
class GateCheck:
    name: str
    value: float | None
    threshold: float
    operator: str
    passed: bool
    observations: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _percentile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def evaluate_thresholds(
    results: Sequence[ProbeResult], thresholds: Mapping[str, Any]
) -> tuple[GateCheck, ...]:
    checks: list[GateCheck] = []
    completed = [result for result in results if result.status == "completed"]
    for name, raw_threshold in thresholds.items():
        if name == "regression_alpha":
            continue
        try:
            threshold = float(raw_threshold)
        except (TypeError, ValueError):
            checks.append(GateCheck(name, None, 0.0, "invalid", False, 0))
            continue
        if name == "max_p95_latency_ms":
            values = [result.latency_ms for result in completed]
            value = _percentile(values, 0.95) if values else None
            checks.append(
                GateCheck(
                    name,
                    value,
                    threshold,
                    "<=",
                    value is not None and value <= threshold,
                    len(values),
                )
            )
            continue
        if not name.endswith("_pass_rate"):
            continue
        slice_name = name[: -len("_pass_rate")]
        selected = _select_slice(completed, slice_name)
        value = fmean(float(result.passed) for result in selected) if selected else None
        checks.append(
            GateCheck(
                name,
                value,
                threshold,
                ">=",
                value is not None and value >= threshold,
                len(selected),
            )
        )
    return tuple(checks)


def _select_slice(results: Iterable[ProbeResult], name: str) -> list[ProbeResult]:
    if name == "overall":
        return list(results)
    if name == "goldset":
        return [
            result for result in results if result.suite == "goldset" or "goldset" in result.tags
        ]
    if name == "redteam":
        return [
            result
            for result in results
            if result.suite.startswith("redteam") or "red_team" in result.tags
        ]
    return [result for result in results if result.category == name]


def evaluate_regression(
    current: Sequence[ProbeResultRecord],
    previous: Sequence[ProbeResultRecord],
    *,
    seed: int,
    confidence_level: float,
    n_resamples: int,
    alpha: float,
    minimum_delta: float,
) -> dict[str, Any] | None:
    old = {result.probe_hash: result for result in previous if result.score is not None}
    pairs = [
        (float(result.score), float(old[result.probe_hash].score))
        for result in current
        if result.score is not None and result.probe_hash in old
    ]
    if not pairs:
        return None
    comparison = paired_comparison(
        [pair[0] for pair in pairs],
        [pair[1] for pair in pairs],
        seed=seed,
        confidence_level=confidence_level,
        n_resamples=n_resamples,
    )
    result = comparison.as_dict()
    result["alpha"] = alpha
    result["minimum_delta"] = abs(minimum_delta)
    material_statistical_regression = (
        comparison.delta <= -abs(minimum_delta) and comparison.p_value < alpha
    )
    critical_regressions = sorted(
        current_result.probe_hash
        for current_result in current
        if current_result.probe_hash in old
        and old[current_result.probe_hash].passed is True
        and current_result.passed is False
        and _is_critical(current_result)
    )
    result["material_statistical_regression"] = material_statistical_regression
    result["critical_regressions"] = critical_regressions
    result["regression"] = material_statistical_regression or bool(critical_regressions)
    return result


def _is_critical(result: ProbeResultRecord) -> bool:
    tags = {str(tag) for tag in result.metadata.get("tags", [])}
    critical_tags = {
        "jailbreak",
        "prompt_injection",
        "system_prompt_extraction",
        "privacy",
        "cross_tenant",
        "credentials",
        "medical",
        "crisis",
    }
    return bool(tags & critical_tags)
