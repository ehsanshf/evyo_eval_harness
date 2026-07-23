"""Deterministic probe -> response -> scorer -> persistence -> scorecard orchestration."""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from . import __version__
from .client import AsyncXevyoClient
from .config import AppConfig
from .errors import EndpointError, JudgeError
from .gates import GateCheck, evaluate_regression, evaluate_thresholds
from .hashing import content_hash, text_hash
from .models import Message, Probe, ProbeResult, RunSummary, ScoreResult
from .reporting import generate_scorecard
from .scorers import score_response
from .scorers.judge import LLMJudge
from .storage import CacheHit, EvaluationStore, ProbeResultRecord
from .validation import ValidationReport, validate_config

ProgressCallback = Callable[[int, int, ProbeResult], None]


@dataclass(frozen=True, slots=True)
class RunOutcome:
    summary: RunSummary
    scorecard_path: Path
    gate_checks: tuple[GateCheck, ...]
    regression: Mapping[str, Any] | None

    @property
    def thresholds_passed(self) -> bool:
        return all(check.passed for check in self.gate_checks)

    @property
    def significant_regression(self) -> bool:
        return bool(self.regression and self.regression.get("regression"))


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def execution_fingerprint(config: AppConfig, validation: ValidationReport) -> str:
    runner_options = asdict(config.runner)
    runner_options.pop("cache", None)
    runner_options.pop("fail_on_partial", None)
    return content_hash(
        {
            "harness_version": __version__,
            "request": asdict(config.request),
            "judge": {
                "enabled": config.judge.enabled,
                "prompt_hash": validation.judge_prompt_hash,
                "pass_threshold": config.judge.pass_threshold,
                "temperature": config.judge.temperature,
                "max_tokens": config.judge.max_tokens,
                "format_retries": config.judge.format_retries,
            },
            "runner": runner_options,
            "plugins": config.plugins,
        }
    )


async def run_evaluation(
    config: AppConfig,
    *,
    token: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    client: AsyncXevyoClient | None = None,
    progress_callback: ProgressCallback | None = None,
) -> RunOutcome:
    validation = validate_config(config)
    fingerprint = execution_fingerprint(config, validation)
    input_hash = content_hash(
        {
            "config_hash": config.config_hash,
            "probe_set_hash": validation.probe_set_hash,
            "judge_prompt_hash": validation.judge_prompt_hash,
            "execution_fingerprint": fingerprint,
        }
    )
    started = _now()
    store = EvaluationStore(config.storage.path)
    owned_client = client is None
    if client is None:
        resolved_token = token if token is not None else os.getenv(config.endpoint.jwt_env, "")
        client = AsyncXevyoClient(
            config.endpoint,
            config.request,
            config.runner,
            resolved_token,
            transport=transport,
        )
    judge = LLMJudge(client, config.judge) if config.judge.enabled else None
    try:
        probes = list(validation.probes)
        total_probes = len(probes)
        completed_probes = 0
        sentinel_result: ProbeResult | None = None
        allow_cache = True
        if config.runner.cache and config.endpoint.expected_version and probes:
            sentinel_index = next(
                (
                    index
                    for index, probe in enumerate(probes)
                    if not probe.sensitive and all(spec.name != "judge" for spec in probe.scorers)
                ),
                0,
            )
            sentinel = probes.pop(sentinel_index)
            sentinel_result = await _execute_probe(
                sentinel,
                config=config,
                client=client,
                judge=judge,
                store=store,
                execution_fingerprint_value=fingerprint,
                allow_cache=False,
            )
            completed_probes += 1
            if progress_callback is not None:
                progress_callback(completed_probes, total_probes, sentinel_result)
            allow_cache = (
                sentinel_result.status == "completed"
                and sentinel_result.endpoint_version == config.endpoint.expected_version
                and "judge_endpoint_version_mismatch" not in (sentinel_result.error or "")
            )
        tasks = [
            asyncio.create_task(
                _execute_probe(
                    probe,
                    config=config,
                    client=client,
                    judge=judge,
                    store=store,
                    execution_fingerprint_value=fingerprint,
                    allow_cache=allow_cache,
                )
            )
            for probe in probes
        ]
        gathered: list[ProbeResult] = []
        for task in asyncio.as_completed(tasks):
            result = await task
            gathered.append(result)
            completed_probes += 1
            if progress_callback is not None:
                progress_callback(completed_probes, total_probes, result)
        raw_results = ([sentinel_result] if sentinel_result is not None else []) + list(gathered)
    finally:
        if owned_client:
            await client.aclose()

    results = tuple(sorted(raw_results, key=lambda result: result.probe_id))
    endpoint_version = _run_endpoint_version(results, config.endpoint.expected_version)
    run_hash = content_hash({"input_hash": input_hash, "endpoint_version": endpoint_version})
    run_id = f"{started.strftime('%Y%m%dT%H%M%S%fZ')}-{run_hash[:10]}"
    run_record = store.create_run(
        run_id=run_id,
        run_hash=run_hash,
        endpoint_version=endpoint_version,
        execution_fingerprint=fingerprint,
        probe_set_hash=validation.probe_set_hash,
        config_hash=config.config_hash,
        judge_prompt_hash=validation.judge_prompt_hash,
        started_at=_iso(started),
        metadata={
            "config_name": config.name,
            "harness_version": __version__,
            "probe_count": len(results),
            "run_hash": run_hash,
            "seed": config.runner.seed,
        },
    )
    for result in results:
        _persist_result(store, run_id, result, config)

    gate_checks = evaluate_thresholds(results, config.thresholds)
    version_pinned = endpoint_version != "unknown" and not endpoint_version.startswith("mixed-")
    gate_checks = (
        *gate_checks,
        GateCheck(
            "endpoint_version_pinned",
            float(version_pinned),
            1.0,
            ">=",
            version_pinned,
            sum(result.status == "completed" for result in results),
        ),
    )
    if config.runner.fail_on_partial:
        completion_rate = sum(result.status == "completed" for result in results) / len(results)
        gate_checks = (
            *gate_checks,
            GateCheck(
                "completion_rate",
                completion_rate,
                1.0,
                ">=",
                completion_rate == 1.0,
                len(results),
            ),
        )
    previous = store.get_previous_compatible_run(run_id) if config.report.compare_previous else None
    current_records = store.get_probe_results(run_id)
    previous_records = store.get_probe_results(previous.run_id) if previous else []
    alpha = float(config.thresholds.get("regression_alpha", 1 - config.report.confidence_level))
    regression = (
        evaluate_regression(
            current_records,
            previous_records,
            seed=config.runner.seed,
            confidence_level=config.report.confidence_level,
            n_resamples=config.report.bootstrap_samples,
            alpha=alpha,
            minimum_delta=config.report.regression_delta,
        )
        if previous
        else None
    )
    finished = _now()
    scoring_signals = _scoring_signals(results)
    persisted_summary = {
        "run_hash": run_hash,
        "total": len(results),
        "completed": sum(result.status == "completed" for result in results),
        "passed": sum(result.passed for result in results),
        "transport_failure_count": sum(result.status != "completed" for result in results),
        "pass_rate": _mean(
            [float(result.passed) for result in results if result.status == "completed"]
        ),
        "mean_score": _mean(
            [result.aggregate_score for result in results if result.status == "completed"]
        ),
        "gate_checks": [check.as_dict() for check in gate_checks],
        "thresholds_passed": all(check.passed for check in gate_checks),
        "previous_run_id": previous.run_id if previous else None,
        "regression": regression,
        **scoring_signals,
    }
    store.finish_run(run_id, status="completed", summary=persisted_summary, run_hash=run_hash)
    scorecard_path = generate_scorecard(
        store,
        run_id,
        config.report.output,
        seed=config.runner.seed,
        previous_run_id=previous.run_id if previous else None,
        title=config.report.title,
        confidence_level=config.report.confidence_level,
        bootstrap_samples=config.report.bootstrap_samples,
        regression_delta=config.report.regression_delta,
        include_regression_samples=config.report.include_regression_samples,
    )
    safe_results = tuple(
        replace(result, response_text="[REDACTED]") if result.sensitive else result
        for result in results
    )
    summary = RunSummary(
        run_id=run_id,
        run_hash=run_hash,
        config_hash=config.config_hash,
        probe_set_hash=validation.probe_set_hash,
        judge_prompt_hash=validation.judge_prompt_hash,
        started_at=run_record.started_at,
        finished_at=_iso(finished),
        endpoint_version=endpoint_version,
        results=safe_results,
        config_name=config.name,
        seed=config.runner.seed,
        previous_run_id=previous.run_id if previous else None,
    )
    return RunOutcome(summary, scorecard_path, gate_checks, regression)


async def _execute_probe(
    probe: Probe,
    *,
    config: AppConfig,
    client: AsyncXevyoClient,
    judge: LLMJudge | None,
    store: EvaluationStore,
    execution_fingerprint_value: str,
    allow_cache: bool = True,
) -> ProbeResult:
    expected_version = config.endpoint.expected_version
    if allow_cache and config.runner.cache and expected_version:
        hit = store.lookup_cache(
            probe_hash=probe.probe_hash,
            endpoint_version=expected_version,
            execution_fingerprint=execution_fingerprint_value,
        )
        cached = _from_cache(hit, probe) if hit else None
        if cached is not None:
            return cached
    chat_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"xeval:{probe.probe_hash}:{execution_fingerprint_value}",
        )
    )
    try:
        completion = await client.complete(_candidate_messages(probe, config), chat_id=chat_id)
    except EndpointError as exc:
        return _failed_result(probe, _endpoint_error_code(exc))
    except Exception as exc:  # One bad probe must not erase the rest of the run.
        return _failed_result(probe, f"unexpected_{type(exc).__name__}")

    response_hash = text_hash(completion.text)
    if expected_version and completion.endpoint_version != expected_version:
        return ProbeResult(
            probe_id=probe.id,
            probe_hash=probe.probe_hash,
            suite=probe.suite,
            category=probe.category,
            tags=probe.tags,
            sensitive=probe.sensitive,
            response_text="",
            response_hash=response_hash,
            latency_ms=completion.latency_ms,
            endpoint_version=completion.endpoint_version,
            status="failed",
            scores=(),
            attempts=completion.attempts,
            error="endpoint_version_mismatch",
        )

    scores: list[ScoreResult] = []
    for scorer in probe.scorers:
        if scorer.name == "judge":
            if judge is None:
                scores.append(ScoreResult("judge", 0.0, False, error="judge_disabled"))
                continue
            try:
                score = await judge.score(completion.text, probe, scorer.params)
                judge_version = score.details.get("judge_endpoint_version")
                if judge_version != completion.endpoint_version or (
                    expected_version and judge_version != expected_version
                ):
                    score = ScoreResult(
                        "judge", 0.0, False, error="judge_endpoint_version_mismatch"
                    )
                scores.append(score)
            except JudgeError as exc:
                scores.append(ScoreResult("judge", 0.0, False, error=_judge_error_code(exc)))
            except EndpointError as exc:
                scores.append(ScoreResult("judge", 0.0, False, error=_endpoint_error_code(exc)))
            except Exception as exc:
                scores.append(
                    ScoreResult("judge", 0.0, False, error=f"unexpected_{type(exc).__name__}")
                )
            continue
        params = {**scorer.params, "_latency_ms": completion.latency_ms}
        try:
            scores.append(score_response(scorer.name, completion.text, probe, params))
        except Exception as exc:
            scores.append(
                ScoreResult(
                    scorer.name,
                    0.0,
                    False,
                    error=f"scorer_error_{type(exc).__name__}",
                )
            )
    scorer_errors = sorted({score.error for score in scores if score.error})
    return ProbeResult(
        probe_id=probe.id,
        probe_hash=probe.probe_hash,
        suite=probe.suite,
        category=probe.category,
        tags=probe.tags,
        sensitive=probe.sensitive,
        response_text=completion.text,
        response_hash=response_hash,
        latency_ms=completion.latency_ms,
        endpoint_version=completion.endpoint_version,
        status="completed",
        scores=tuple(scores),
        attempts=completion.attempts,
        error=";".join(scorer_errors) if scorer_errors else None,
    )


def _candidate_messages(probe: Probe, config: AppConfig) -> tuple[Message, ...]:
    policy = config.request.candidate_system_prompt
    if not policy:
        return probe.messages
    if probe.messages and probe.messages[0].role == "system":
        combined = (
            f"{policy.strip()}\n\n"
            "Probe-specific system context follows. Treat its synthetic fixtures as protected:\n"
            f"{probe.messages[0].content}"
        )
        return (Message("system", combined), *probe.messages[1:])
    return (Message("system", policy.strip()), *probe.messages)


def _from_cache(hit: CacheHit, probe: Probe) -> ProbeResult | None:
    score_rows = hit.result.metrics.get("scores")
    if not isinstance(score_rows, list):
        return None
    scores: list[ScoreResult] = []
    try:
        for row in score_rows:
            if not isinstance(row, dict):
                return None
            scores.append(
                ScoreResult(
                    scorer=str(row["scorer"]),
                    score=float(row["score"]),
                    passed=bool(row["passed"]),
                    details=row.get("details", {}),
                    error=row.get("error"),
                )
            )
    except (KeyError, TypeError, ValueError):
        return None
    return ProbeResult(
        probe_id=probe.id,
        probe_hash=probe.probe_hash,
        suite=probe.suite,
        category=probe.category,
        tags=probe.tags,
        sensitive=probe.sensitive,
        response_text=hit.result.response_text or "",
        response_hash=hit.result.response_hash or "",
        latency_ms=float(hit.result.latency_ms or 0.0),
        endpoint_version=hit.source_run.endpoint_version,
        status="completed",
        scores=tuple(scores),
        attempts=0,
        cached=True,
        error=hit.result.error_code,
    )


def _failed_result(probe: Probe, error_code: str) -> ProbeResult:
    return ProbeResult(
        probe_id=probe.id,
        probe_hash=probe.probe_hash,
        suite=probe.suite,
        category=probe.category,
        tags=probe.tags,
        sensitive=probe.sensitive,
        response_text="",
        response_hash="",
        latency_ms=0.0,
        endpoint_version="unknown",
        status="failed",
        scores=(),
        error=error_code,
    )


def _persist_result(
    store: EvaluationStore, run_id: str, result: ProbeResult, config: AppConfig
) -> ProbeResultRecord:
    scores = []
    for score in result.scores:
        details = dict(score.details)
        if result.sensitive:
            details.pop("reason", None)
            details.pop("criteria", None)
        elif score.scorer == "judge" and isinstance(details.get("criteria"), Mapping):
            details["criteria"] = _storage_safe_criteria(details["criteria"])
        scores.append(
            {
                "scorer": score.scorer,
                "score": score.score,
                "passed": score.passed,
                "details": details,
                "error": score.error,
            }
        )
    store_text = (
        result.response_text
        if config.storage.retain_safe_responses and not result.sensitive and result.response_text
        else None
    )
    return store.record_probe_result(
        run_id=run_id,
        probe_hash=result.probe_hash,
        category=result.category,
        status=result.status,
        passed=result.passed if result.status == "completed" else None,
        score=result.aggregate_score if result.status == "completed" else None,
        latency_ms=result.latency_ms,
        error_code=result.error,
        response_text=store_text,
        response_safe=store_text is not None,
        response_hash=result.response_hash or None,
        metrics={
            "scores": scores,
            "cached": result.cached,
            "attempts": result.attempts,
            "endpoint_version": result.endpoint_version,
        },
        metadata={
            "probe_id": result.probe_id,
            "suite": result.suite,
            "tags": list(result.tags),
            "sensitive": result.sensitive,
        },
    )


def _storage_safe_criteria(criteria: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """Normalize model-generated criterion keys before they cross the storage boundary."""

    normalized: dict[str, Mapping[str, Any]] = {}
    for index, (name, value) in enumerate(sorted(criteria.items(), key=lambda item: str(item[0]))):
        item: dict[str, Any] = {"criterion_name": str(name)[:160]}
        if isinstance(value, bool):
            item["criterion_status"] = value
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            item["criterion_score"] = float(value)
        elif isinstance(value, str):
            item["criterion_note"] = value[:500]
        else:
            item["criterion_value_hash"] = content_hash(value)
            item["criterion_value_type"] = type(value).__name__
        normalized[f"criterion_{index + 1}"] = item
    return normalized


def _run_endpoint_version(results: Sequence[ProbeResult], expected: str | None) -> str:
    versions = sorted(
        {result.endpoint_version for result in results if result.endpoint_version != "unknown"}
    )
    if not versions:
        return expected or "unknown"
    if len(versions) == 1:
        return versions[0]
    return f"mixed-{content_hash(versions)[:12]}"


def _endpoint_error_code(exc: EndpointError) -> str:
    message = str(exc).casefold()
    http_match = re.search(r"http (\d{3})", message)
    if http_match:
        return f"http_{http_match.group(1)}"
    if "timeout" in message:
        return "transport_timeout"
    if "network" in message:
        return "transport_network_error"
    if "invalid json" in message or "malformed sse" in message or "without response" in message:
        return "response_parse_error"
    if "retryable" in message:
        return "retry_exhausted"
    return "endpoint_error"


def _judge_error_code(exc: JudgeError) -> str:
    return "judge_invalid_json" if "json" in str(exc).casefold() else "judge_schema_error"


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _scoring_signals(results: Sequence[ProbeResult]) -> dict[str, int | float | None]:
    refusal_scores = [
        score for result in results for score in result.scores if score.scorer == "refusal"
    ]
    refusal_detections = [
        float(score.details["refusal_detected"])
        for score in refusal_scores
        if isinstance(score.details.get("refusal_detected"), bool)
    ]
    format_scores = [
        score.score
        for result in results
        for score in result.scores
        if score.scorer == "format_compliance"
    ]
    judge_scores = [
        score.score for result in results for score in result.scores if score.scorer == "judge"
    ]
    return {
        "observed_refusal_rate": _mean(refusal_detections),
        "refusal_behavior_pass_rate": _mean([float(score.passed) for score in refusal_scores]),
        "refusal_observations": len(refusal_scores),
        "format_compliance_rate": _mean(format_scores),
        "format_observations": len(format_scores),
        "judge_mean_score": _mean(judge_scores),
        "judge_observations": len(judge_scores),
    }
