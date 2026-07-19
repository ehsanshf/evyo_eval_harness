"""Build and render a self-contained static evaluation scorecard."""

from __future__ import annotations

import hashlib
import math
import os
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean
from typing import Any

from .statistics import (
    DEFAULT_BOOTSTRAP_RESAMPLES,
    DEFAULT_CONFIDENCE_LEVEL,
    DEFAULT_PERMUTATION_RESAMPLES,
    bootstrap_mean_ci,
    bootstrap_rate_ci,
    paired_comparison,
)
from .storage import EvaluationStore


@dataclass(frozen=True, slots=True)
class CategorySummary:
    category: str
    total: int
    evaluated: int
    passed: int
    failed: int
    errors: int
    pass_rate: float | None
    pass_ci_low: float | None
    pass_ci_high: float | None
    mean_score: float | None
    score_ci_low: float | None
    score_ci_high: float | None
    latency_p50_ms: float | None
    latency_p95_ms: float | None
    previous_total: int
    pass_delta: float | None
    pass_p_value: float | None
    pass_pairs: int
    score_delta: float | None
    score_p_value: float | None
    score_pairs: int
    sparkline_points: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RegressionSample:
    probe_hash: str
    category: str
    reason: str
    previous_passed: bool | None
    current_passed: bool | None
    previous_score: float | None
    current_score: float | None
    score_delta: float | None
    response_text: str | None
    response_disposition: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TrendPoint:
    run_id: str
    label: str
    pass_rate: float | None
    mean_score: float | None
    endpoint_version: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Scorecard:
    title: str
    generated_at: str
    run: Mapping[str, Any]
    previous_run: Mapping[str, Any] | None
    summary: CategorySummary
    categories: tuple[CategorySummary, ...]
    regressions: tuple[RegressionSample, ...]
    history: tuple[TrendPoint, ...]
    overall_sparkline_points: str
    confidence_level: float
    bootstrap_resamples: int
    permutation_resamples: int
    seed: int
    include_regression_samples: bool
    hashes: Mapping[str, str]
    gate_checks: tuple[Mapping[str, Any], ...] = ()
    release_status: str = "not-evaluated"
    regression_gate: Mapping[str, Any] | None = None
    scoring_signals: tuple[Mapping[str, Any], ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _mapping(record: Any) -> Mapping[str, Any]:
    if record is None:
        return {}
    if isinstance(record, Mapping):
        return record
    if is_dataclass(record):
        data = asdict(record)
        # Shared harness value objects expose a few useful computed properties
        # (notably ProbeResult.passed/aggregate_score) that dataclasses.asdict
        # intentionally omits.
        for name in ("passed", "aggregate_score", "response_is_safe"):
            if hasattr(record, name):
                data[name] = getattr(record, name)
        return data
    as_dict_method = getattr(record, "as_dict", None)
    if callable(as_dict_method):
        value = as_dict_method()
        if isinstance(value, Mapping):
            return value
    model_dump = getattr(record, "model_dump", None)
    if callable(model_dump):
        value = model_dump()
        if isinstance(value, Mapping):
            return value
    keys = (
        "run_id",
        "probe_hash",
        "probe_id",
        "category",
        "status",
        "passed",
        "score",
        "latency_ms",
        "response_text",
        "response",
        "response_disposition",
        "response_safe",
        "response_redacted",
        "endpoint_version",
        "execution_fingerprint",
        "suite_hash",
        "config_hash",
        "judge_prompt_hash",
        "started_at",
        "completed_at",
        "metadata",
        "summary",
    )
    return {key: getattr(record, key) for key in keys if hasattr(record, key)}


def _first(record: Any, *names: str, default: Any = None) -> Any:
    values = _mapping(record)
    for name in names:
        if name in values:
            return values[name]
    return default


def _finite_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _passed(record: Any) -> bool | None:
    value = _first(record, "passed", "is_passed", "pass", default=None)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"pass", "passed", "true", "1", "success"}:
            return True
        if normalized in {"fail", "failed", "false", "0", "failure"}:
            return False
    status = str(_first(record, "status", default="")).strip().lower()
    scores = _first(record, "scores", default=None)
    if status == "completed" and isinstance(scores, Sequence):
        score_outcomes: list[bool] = []
        for score in scores:
            score_passed = _first(score, "passed", default=None)
            if isinstance(score_passed, bool):
                score_outcomes.append(score_passed)
        return bool(score_outcomes) and all(score_outcomes)
    if status in {"pass", "passed"}:
        return True
    if status in {"fail", "failed"}:
        return False
    return None


def _score(record: Any) -> float | None:
    value = _first(record, "score", "overall_score", "aggregate_score", default=None)
    if value is None:
        metrics = _first(record, "metrics", default={})
        if isinstance(metrics, Mapping):
            value = metrics.get("score")
    if value is None:
        scores = _first(record, "scores", default=None)
        if isinstance(scores, Sequence):
            component_scores = [
                number
                for component in scores
                if (number := _finite_number(_first(component, "score", default=None))) is not None
            ]
            value = fmean(component_scores) if component_scores else None
    return _finite_number(value)


def _latency(record: Any) -> float | None:
    value = _finite_number(_first(record, "latency_ms", "latency", default=None))
    return value if value is not None and value >= 0.0 else None


def _probe_hash(record: Any) -> str:
    value = _first(record, "probe_hash", "probe_id", "id", default="unknown")
    return str(value)


def _category(record: Any) -> str:
    value = _first(record, "category", "attack_class", default="uncategorized")
    return str(value or "uncategorized")


def _derived_seed(seed: int, namespace: str) -> int:
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise TypeError("seed must be an explicit integer")
    digest = hashlib.sha256(f"{seed}:{namespace}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=False)


def _quantile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _sparkline(values: Iterable[float | None], *, width: int = 180, height: int = 38) -> str:
    numeric = [value for value in values if value is not None and math.isfinite(value)]
    if not numeric:
        return ""
    if len(numeric) == 1:
        return f"{width / 2:.2f},{height / 2:.2f}"
    minimum = min(numeric)
    maximum = max(numeric)
    spread = maximum - minimum
    points: list[str] = []
    for index, value in enumerate(numeric):
        x = index * width / (len(numeric) - 1)
        y = height / 2 if spread == 0.0 else height - ((value - minimum) / spread) * height
        # Leave a pixel of visual breathing room at the top and bottom.
        y = min(height - 1.0, max(1.0, y))
        points.append(f"{x:.2f},{y:.2f}")
    return " ".join(points)


def _paired_values(
    current: Sequence[Any],
    previous: Sequence[Any],
    value_getter: Any,
) -> tuple[list[float], list[float]]:
    previous_by_hash = {_probe_hash(record): record for record in previous}
    current_values: list[float] = []
    previous_values: list[float] = []
    for record in current:
        prior = previous_by_hash.get(_probe_hash(record))
        if prior is None:
            continue
        current_value = value_getter(record)
        previous_value = value_getter(prior)
        if current_value is None or previous_value is None:
            continue
        current_values.append(float(current_value))
        previous_values.append(float(previous_value))
    return current_values, previous_values


def _history_records(entry: Any) -> Sequence[Any]:
    records = _first(entry, "results", "probe_results", default=())
    if isinstance(records, Sequence) and not isinstance(records, (str, bytes, bytearray)):
        return records
    return ()


def _make_summary(
    category: str,
    records: Sequence[Any],
    previous_records: Sequence[Any],
    history: Sequence[Any],
    *,
    seed: int,
    confidence_level: float,
    bootstrap_resamples: int,
    permutation_resamples: int,
) -> CategorySummary:
    outcomes = [_passed(record) for record in records]
    evaluated_outcomes = [float(value) for value in outcomes if value is not None]
    scores = [value for record in records if (value := _score(record)) is not None]
    latencies = [value for record in records if (value := _latency(record)) is not None]

    pass_interval = (
        bootstrap_rate_ci(
            evaluated_outcomes,
            seed=_derived_seed(seed, f"{category}:pass-ci"),
            confidence_level=confidence_level,
            n_resamples=bootstrap_resamples,
        )
        if evaluated_outcomes
        else None
    )
    score_interval = (
        bootstrap_mean_ci(
            scores,
            seed=_derived_seed(seed, f"{category}:score-ci"),
            confidence_level=confidence_level,
            n_resamples=bootstrap_resamples,
        )
        if scores
        else None
    )

    paired_pass_current, paired_pass_previous = _paired_values(records, previous_records, _passed)
    pass_comparison = (
        paired_comparison(
            paired_pass_current,
            paired_pass_previous,
            seed=_derived_seed(seed, f"{category}:pass-comparison"),
            confidence_level=confidence_level,
            n_resamples=bootstrap_resamples,
            permutation_resamples=permutation_resamples,
        )
        if paired_pass_current
        else None
    )
    paired_score_current, paired_score_previous = _paired_values(records, previous_records, _score)
    score_comparison = (
        paired_comparison(
            paired_score_current,
            paired_score_previous,
            seed=_derived_seed(seed, f"{category}:score-comparison"),
            confidence_level=confidence_level,
            n_resamples=bootstrap_resamples,
            permutation_resamples=permutation_resamples,
        )
        if paired_score_current
        else None
    )

    trend_values: list[float | None] = []
    for entry in history:
        historical_records = _history_records(entry)
        if historical_records:
            if category != "All probes":
                historical_records = [
                    record for record in historical_records if _category(record) == category
                ]
            historical_outcomes = [
                float(value)
                for record in historical_records
                if (value := _passed(record)) is not None
            ]
            trend_values.append(fmean(historical_outcomes) if historical_outcomes else None)

    passed_count = sum(value is True for value in outcomes)
    failed_count = sum(value is False for value in outcomes)
    return CategorySummary(
        category=category,
        total=len(records),
        evaluated=len(evaluated_outcomes),
        passed=passed_count,
        failed=failed_count,
        errors=len(records) - passed_count - failed_count,
        pass_rate=pass_interval.estimate if pass_interval else None,
        pass_ci_low=pass_interval.low if pass_interval else None,
        pass_ci_high=pass_interval.high if pass_interval else None,
        mean_score=score_interval.estimate if score_interval else None,
        score_ci_low=score_interval.low if score_interval else None,
        score_ci_high=score_interval.high if score_interval else None,
        latency_p50_ms=_quantile(latencies, 0.50),
        latency_p95_ms=_quantile(latencies, 0.95),
        previous_total=len(previous_records),
        pass_delta=pass_comparison.delta if pass_comparison else None,
        pass_p_value=pass_comparison.p_value if pass_comparison else None,
        pass_pairs=len(paired_pass_current),
        score_delta=score_comparison.delta if score_comparison else None,
        score_p_value=score_comparison.p_value if score_comparison else None,
        score_pairs=len(paired_score_current),
        sparkline_points=_sparkline(trend_values),
    )


def _response_sample(record: Any, *, max_characters: int = 1_200) -> tuple[str | None, str]:
    disposition = str(_first(record, "response_disposition", default="")).lower()
    sensitive_marker = _first(record, "sensitive", default=None)
    # ProbeResult.sensitive=False is an explicit classification by the probe
    # curator and is therefore an acceptable public-sample marker.
    explicitly_safe = bool(_first(record, "response_safe", default=False)) or (
        sensitive_marker is False
    )
    explicitly_redacted = bool(_first(record, "response_redacted", default=False))
    if disposition not in {"safe", "redacted"} and not (explicitly_safe or explicitly_redacted):
        return None, "not_stored"
    value = _first(record, "response_text", "response", default=None)
    if value is None:
        return None, disposition or ("redacted" if explicitly_redacted else "safe")
    text = str(value)
    if len(text) > max_characters:
        text = f"{text[: max_characters - 1]}…"
    return text, disposition or ("redacted" if explicitly_redacted else "safe")


def _regressions(
    current: Sequence[Any],
    previous: Sequence[Any],
    *,
    score_drop_threshold: float,
    limit: int,
) -> tuple[RegressionSample, ...]:
    previous_by_hash = {_probe_hash(record): record for record in previous}
    samples: list[RegressionSample] = []
    for record in current:
        prior = previous_by_hash.get(_probe_hash(record))
        if prior is None:
            continue
        current_passed = _passed(record)
        previous_passed = _passed(prior)
        current_score = _score(record)
        previous_score = _score(prior)
        delta = (
            current_score - previous_score
            if current_score is not None and previous_score is not None
            else None
        )
        if previous_passed is True and current_passed is False:
            reason = "Pass → fail"
        elif delta is not None and delta <= -abs(score_drop_threshold):
            reason = f"Score dropped {abs(delta):.3f}"
        else:
            continue
        response, disposition = _response_sample(record)
        samples.append(
            RegressionSample(
                probe_hash=_probe_hash(record),
                category=_category(record),
                reason=reason,
                previous_passed=previous_passed,
                current_passed=current_passed,
                previous_score=previous_score,
                current_score=current_score,
                score_delta=delta,
                response_text=response,
                response_disposition=disposition,
            )
        )
    samples.sort(
        key=lambda sample: (
            sample.previous_passed is True and sample.current_passed is False,
            -(sample.score_delta if sample.score_delta is not None else 0.0),
        ),
        reverse=True,
    )
    return tuple(samples[:limit])


def _public_run(run: Any) -> dict[str, Any]:
    values = _mapping(run)
    public = {
        key: values.get(key)
        for key in (
            "run_id",
            "status",
            "started_at",
            "completed_at",
            "endpoint_version",
            "execution_fingerprint",
            "suite_hash",
            "config_hash",
            "judge_prompt_hash",
        )
    }
    public["status"] = public["status"] or ("completed" if values.get("finished_at") else None)
    public["completed_at"] = public["completed_at"] or values.get("finished_at")
    public["execution_fingerprint"] = public["execution_fingerprint"] or values.get("run_hash")
    public["suite_hash"] = public["suite_hash"] or values.get("probe_set_hash")
    public["run_hash"] = values.get("run_hash")
    return public


def _safe_metadata(run: Any) -> dict[str, Any]:
    metadata = _first(run, "metadata", default={})
    if not isinstance(metadata, Mapping):
        return {}
    safe: dict[str, Any] = {}
    for key, value in metadata.items():
        normalized = str(key).lower()
        safe_suffixes = (
            "hash",
            "digest",
            "sha256",
            "version",
            "id",
            "name",
            "count",
            "rate",
            "score",
            "status",
            "code",
            "tokens",
            "valid",
            "type",
            "class",
        )
        sensitive_tokens = (
            "prompt",
            "message",
            "query",
            "request",
            "response",
            "completion",
            "output",
            "content",
        )
        if any(token in normalized for token in sensitive_tokens) and not normalized.endswith(
            safe_suffixes
        ):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            safe[str(key)] = value
    return safe


def _release_gates(
    run: Any,
) -> tuple[tuple[Mapping[str, Any], ...], str, Mapping[str, Any] | None]:
    summary = _first(run, "summary", default={})
    if not isinstance(summary, Mapping):
        return (), "not-evaluated", None
    checks: list[Mapping[str, Any]] = []
    raw_checks = summary.get("gate_checks", [])
    if isinstance(raw_checks, Sequence) and not isinstance(raw_checks, (str, bytes)):
        for raw in raw_checks:
            if not isinstance(raw, Mapping) or not isinstance(raw.get("passed"), bool):
                continue
            checks.append(
                {
                    "name": str(raw.get("name", "unnamed_gate")),
                    "value": _finite_number(raw.get("value")),
                    "threshold": _finite_number(raw.get("threshold")),
                    "operator": str(raw.get("operator", "")),
                    "passed": raw["passed"],
                    "observations": int(raw.get("observations", 0)),
                }
            )
    raw_regression = summary.get("regression")
    regression: Mapping[str, Any] | None = None
    if isinstance(raw_regression, Mapping):
        critical = raw_regression.get("critical_regressions", [])
        regression = {
            "delta": _finite_number(raw_regression.get("delta")),
            "p_value": _finite_number(raw_regression.get("p_value")),
            "minimum_delta": _finite_number(raw_regression.get("minimum_delta")),
            "alpha": _finite_number(raw_regression.get("alpha")),
            "pairs": int(raw_regression.get("n", 0)),
            "critical_count": (
                len(critical)
                if isinstance(critical, Sequence) and not isinstance(critical, (str, bytes))
                else 0
            ),
            "regression": bool(raw_regression.get("regression", False)),
        }
    thresholds_passed = summary.get("thresholds_passed")
    release_failed = any(not check["passed"] for check in checks) or bool(
        regression and regression["regression"]
    )
    if release_failed:
        status = "fail"
    elif thresholds_passed is True or checks or regression is not None:
        status = "pass"
    else:
        status = "not-evaluated"
    return tuple(checks), status, regression


def _scoring_signals(run: Any) -> tuple[Mapping[str, Any], ...]:
    summary = _first(run, "summary", default={})
    if not isinstance(summary, Mapping):
        return ()
    definitions = (
        ("Observed refusal rate", "observed_refusal_rate", "refusal_observations", "percent"),
        (
            "Refusal behavior pass rate",
            "refusal_behavior_pass_rate",
            "refusal_observations",
            "percent",
        ),
        (
            "Format compliance rate",
            "format_compliance_rate",
            "format_observations",
            "percent",
        ),
        ("Mean judge score", "judge_mean_score", "judge_observations", "number"),
    )
    signals: list[Mapping[str, Any]] = []
    for label, value_key, count_key, display in definitions:
        value = _finite_number(summary.get(value_key))
        if value is None:
            continue
        signals.append(
            {
                "label": label,
                "value": value,
                "observations": int(summary.get(count_key, 0)),
                "display": display,
            }
        )
    return tuple(signals)


def _trend_point(entry: Any) -> TrendPoint:
    run_values = _mapping(_first(entry, "run", default=entry))
    records = _history_records(entry)
    outcomes = [float(value) for record in records if (value := _passed(record)) is not None]
    scores = [value for record in records if (value := _score(record)) is not None]
    summary = _first(entry, "summary", default={})
    summary = summary if isinstance(summary, Mapping) else {}
    pass_rate = (
        fmean(outcomes)
        if outcomes
        else _finite_number(_first(entry, "pass_rate", default=summary.get("pass_rate")))
    )
    mean_score = (
        fmean(scores)
        if scores
        else _finite_number(_first(entry, "mean_score", default=summary.get("mean_score")))
    )
    started_at = str(run_values.get("started_at") or _first(entry, "label", default=""))
    return TrendPoint(
        run_id=str(run_values.get("run_id") or _first(entry, "run_id", default="unknown")),
        label=started_at,
        pass_rate=pass_rate,
        mean_score=mean_score,
        endpoint_version=str(
            run_values.get("endpoint_version")
            or _first(entry, "endpoint_version", default="unknown")
        ),
    )


def build_scorecard(
    run: Any,
    results: Sequence[Any] | None = None,
    *,
    seed: int,
    previous_run: Any | None = None,
    previous_results: Sequence[Any] | None = None,
    history: Sequence[Any] = (),
    title: str = "Xevyo evaluation scorecard",
    confidence_level: float = DEFAULT_CONFIDENCE_LEVEL,
    bootstrap_samples: int = DEFAULT_BOOTSTRAP_RESAMPLES,
    permutation_resamples: int = DEFAULT_PERMUTATION_RESAMPLES,
    regression_delta: float = 0.05,
    include_regression_samples: bool = True,
    regression_limit: int = 20,
) -> Scorecard:
    """Aggregate result-like mappings/objects into a renderable scorecard model."""

    if results is None:
        candidate_results = _first(run, "results", default=())
        results = candidate_results if isinstance(candidate_results, Sequence) else ()
    if previous_results is None:
        candidate_previous_results = _first(previous_run, "results", default=())
        previous_results = (
            candidate_previous_results if isinstance(candidate_previous_results, Sequence) else ()
        )
    if not results:
        raise ValueError("results must contain at least one probe result")
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise TypeError("seed must be an explicit integer")
    if not 0.0 < confidence_level < 1.0:
        raise ValueError("confidence_level must be between 0 and 1")
    if bootstrap_samples < 1:
        raise ValueError("bootstrap_samples must be at least 1")
    if permutation_resamples < 1:
        raise ValueError("permutation_resamples must be at least 1")
    if regression_limit < 0:
        raise ValueError("regression_limit must be non-negative")
    if not math.isfinite(regression_delta) or regression_delta < 0.0:
        raise ValueError("regression_delta must be a finite non-negative number")

    current_records = list(results)
    prior_records = list(previous_results)
    history_entries = list(history)
    overall = _make_summary(
        "All probes",
        current_records,
        prior_records,
        history_entries,
        seed=seed,
        confidence_level=confidence_level,
        bootstrap_resamples=bootstrap_samples,
        permutation_resamples=permutation_resamples,
    )
    category_names = sorted({_category(record) for record in current_records}, key=str.casefold)
    categories = tuple(
        _make_summary(
            category,
            [record for record in current_records if _category(record) == category],
            [record for record in prior_records if _category(record) == category],
            history_entries,
            seed=seed,
            confidence_level=confidence_level,
            bootstrap_resamples=bootstrap_samples,
            permutation_resamples=permutation_resamples,
        )
        for category in category_names
    )
    trend_points = tuple(_trend_point(entry) for entry in history_entries)
    run_values = _public_run(run)
    generated_at = str(
        run_values.get("completed_at")
        or run_values.get("started_at")
        or datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    )
    hashes = {
        label: str(run_values.get(label) or "")
        for label in (
            "execution_fingerprint",
            "suite_hash",
            "config_hash",
            "judge_prompt_hash",
        )
    }
    if run_values.get("run_hash"):
        hashes["run_hash"] = str(run_values["run_hash"])
    gate_checks, release_status, regression_gate = _release_gates(run)
    return Scorecard(
        title=title,
        generated_at=generated_at,
        run=run_values,
        previous_run=_public_run(previous_run) if previous_run is not None else None,
        summary=overall,
        categories=categories,
        regressions=(
            _regressions(
                current_records,
                prior_records,
                score_drop_threshold=regression_delta,
                limit=regression_limit,
            )
            if include_regression_samples
            else ()
        ),
        history=trend_points,
        overall_sparkline_points=_sparkline(point.pass_rate for point in trend_points),
        confidence_level=confidence_level,
        bootstrap_resamples=bootstrap_samples,
        permutation_resamples=permutation_resamples,
        seed=seed,
        include_regression_samples=include_regression_samples,
        hashes=hashes,
        gate_checks=gate_checks,
        release_status=release_status,
        regression_gate=regression_gate,
        scoring_signals=_scoring_signals(run),
        metadata=_safe_metadata(run),
    )


def render_scorecard_html(
    scorecard: Scorecard | Mapping[str, Any],
    *,
    template_path: str | Path | None = None,
) -> str:
    """Render a scorecard to self-contained HTML (no network assets or JS)."""

    try:
        from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
    except ImportError as exc:  # pragma: no cover - exercised only in minimal installations
        raise RuntimeError("Jinja2 is required to render the HTML scorecard") from exc

    chosen_template = (
        Path(template_path)
        if template_path is not None
        else Path(__file__).with_name("templates") / "scorecard.html.j2"
    )
    environment = Environment(
        loader=FileSystemLoader(str(chosen_template.parent)),
        autoescape=select_autoescape(enabled_extensions=("html", "j2"), default=True),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = environment.get_template(chosen_template.name)
    context = scorecard.as_dict() if isinstance(scorecard, Scorecard) else dict(scorecard)
    return template.render(report=context)


def render_scorecard(
    scorecard: Scorecard | Mapping[str, Any],
    output_path: str | Path,
    *,
    template_path: str | Path | None = None,
) -> Path:
    """Atomically write a self-contained scorecard and return its path."""

    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    html = render_scorecard_html(scorecard, template_path=template_path)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(html)
            temporary_name = temporary.name
        os.replace(temporary_name, destination)
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return destination


def generate_scorecard(
    store: EvaluationStore,
    run_id: str,
    output_path: str | Path,
    *,
    seed: int,
    previous_run_id: str | None = None,
    title: str = "Xevyo evaluation scorecard",
    history_limit: int = 12,
    confidence_level: float = DEFAULT_CONFIDENCE_LEVEL,
    bootstrap_samples: int = DEFAULT_BOOTSTRAP_RESAMPLES,
    permutation_resamples: int = DEFAULT_PERMUTATION_RESAMPLES,
    regression_delta: float = 0.05,
    include_regression_samples: bool = True,
    regression_limit: int = 20,
) -> Path:
    """Load a run, compatible baseline, and history from SQLite and render HTML."""

    run = store.get_run(run_id)
    if run is None:
        raise KeyError(f"unknown run_id: {run_id}")
    results = store.get_probe_results(run_id)
    previous = (
        store.get_run(previous_run_id)
        if previous_run_id is not None
        else store.get_previous_compatible_run(run_id)
    )
    if previous_run_id is not None and previous is None:
        raise KeyError(f"unknown previous_run_id: {previous_run_id}")
    previous_results = store.get_probe_results(previous.run_id) if previous else []

    compatible_runs = [
        candidate
        for candidate in store.list_runs(status="completed", limit=max(history_limit * 4, 20))
        if candidate.suite_hash == run.suite_hash
        and candidate.config_hash == run.config_hash
        and candidate.judge_prompt_hash == run.judge_prompt_hash
        and candidate.execution_fingerprint == run.execution_fingerprint
        and candidate.started_at <= run.started_at
    ][:history_limit]
    compatible_runs.reverse()
    history = [
        {"run": candidate, "results": store.get_probe_results(candidate.run_id)}
        for candidate in compatible_runs
    ]
    scorecard = build_scorecard(
        run,
        results,
        seed=seed,
        previous_run=previous,
        previous_results=previous_results,
        history=history,
        title=title,
        confidence_level=confidence_level,
        bootstrap_samples=bootstrap_samples,
        permutation_resamples=permutation_resamples,
        regression_delta=regression_delta,
        include_regression_samples=include_regression_samples,
        regression_limit=regression_limit,
    )
    return render_scorecard(scorecard, output_path)


# Natural aliases for CLI/integration code.
write_scorecard = render_scorecard
create_scorecard = generate_scorecard


__all__ = [
    "CategorySummary",
    "RegressionSample",
    "Scorecard",
    "TrendPoint",
    "build_scorecard",
    "create_scorecard",
    "generate_scorecard",
    "render_scorecard",
    "render_scorecard_html",
    "write_scorecard",
]
