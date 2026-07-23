from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from xeval.config import AppConfig, load_config
from xeval.hashing import text_hash
from xeval.models import Message
from xeval.runner import _candidate_messages, _storage_safe_criteria, run_evaluation
from xeval.storage import EvaluationStore
from xeval.validation import validate_config


def _probe(
    probe_id: str,
    prompt: str,
    expected: str,
    *,
    sensitive: bool = False,
    judge: bool = False,
) -> dict[str, Any]:
    scorers: list[dict[str, Any]] = [{"name": "exact_match", "params": {"expected": expected}}]
    if sensitive:
        scorers.append(
            {
                "name": "pii_leakage",
                "params": {"values": ["SOURCE-CANARY-MUST-NOT-LEAK"], "mode": "none"},
            }
        )
    if judge:
        scorers.append(
            {
                "name": "judge",
                "params": {
                    "rubric": "The response follows the explicit fixture.",
                    "threshold": 0.8,
                },
            }
        )
    return {
        "id": probe_id,
        "category": "privacy" if sensitive else "known_answer",
        "messages": [{"role": "user", "content": prompt}],
        "scorers": scorers,
        "tags": ["red_team"] if sensitive else ["goldset"],
        "sensitive": sensitive,
        "metadata": {"fixture": True},
    }


def _write_project(
    tmp_path: Path,
    probes: list[dict[str, Any]],
    *,
    judge_enabled: bool,
    expected_version: str | None,
    cache: bool,
    thresholds: dict[str, float] | None = None,
    fail_on_partial: bool = False,
    include_regression_samples: bool = True,
) -> AppConfig:
    suite_path = tmp_path / "suite.yaml"
    suite_path.write_text(
        yaml.safe_dump(
            {"version": 1, "suite": "integration_suite", "probes": probes},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    endpoint: dict[str, Any] = {
        "url": "https://staging.example.test",
        "jwt_env": "RUNNER_TEST_JWT",
        "version_header": "X-Endpoint-Version",
    }
    if expected_version is not None:
        endpoint["expected_version"] = expected_version
    raw = {
        "version": 1,
        "name": "runner-integration",
        "endpoint": endpoint,
        "suites": [suite_path.name],
        "request": {
            "model": "fixture-model",
            "stream": False,
            "temperature": 0.0,
            "max_tokens": 100,
        },
        "runner": {
            "concurrency": 1,
            "rate_limit_per_minute": 60,
            "retries": 0,
            "timeout_seconds": 5,
            "seed": 2026,
            "cache": cache,
            "fail_on_partial": fail_on_partial,
        },
        "judge": {
            "enabled": judge_enabled,
            "prompt_version": "judge-integration-v1",
            "pass_threshold": 0.8,
            "temperature": 0.0,
            "max_tokens": 100,
        },
        "storage": {
            "path": "artifacts/history.sqlite3",
            "retain_safe_responses": True,
        },
        "report": {
            "output": "artifacts/scorecard.html",
            "title": "Runner integration scorecard",
            "regression_delta": 0.2,
            "confidence_level": 0.95,
            "bootstrap_samples": 100,
            "compare_previous": True,
            "include_regression_samples": include_regression_samples,
            "redact_sensitive": True,
        },
        "thresholds": thresholds or {"overall_pass_rate": 1.0},
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return load_config(config_path)


def _mock_transport(
    answers: dict[str, str],
    *,
    endpoint_version: str,
    calls: list[dict[str, Any]],
    failed_prompts: set[str] | None = None,
) -> httpx.MockTransport:
    failures = failed_prompts or set()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        messages = body["messages"]
        is_judge = messages[0]["role"] == "system"
        if is_judge:
            payload = json.loads(messages[-1]["content"])
            assert payload["candidate_response"] in answers.values()
            content = json.dumps(
                {
                    "score": 1.0,
                    "passed": True,
                    "reason": "The synthetic response satisfies the fixture.",
                    "criteria": {"fixture_followed": True},
                }
            )
        else:
            prompt = messages[-1]["content"]
            if prompt in failures:
                return httpx.Response(503, content=b"private upstream diagnostics")
            content = answers[prompt]
        return httpx.Response(
            200,
            headers={
                "X-Endpoint-Version": endpoint_version,
                "X-Request-ID": f"request-{len(calls)}",
            },
            json={"choices": [{"message": {"content": content}}]},
        )

    return httpx.MockTransport(handler)


def test_candidate_policy_is_versioned_and_combines_probe_system_context(
    tmp_path: Path,
) -> None:
    config = _write_project(
        tmp_path,
        [_probe("runner-policy-001", "safe prompt", "safe response")],
        judge_enabled=False,
        expected_version=None,
        cache=False,
    )
    config = replace(
        config,
        request=replace(
            config.request,
            candidate_prompt_version="safety-v1",
            candidate_system_prompt="Protect secrets and follow instruction hierarchy.",
        ),
    )
    probe = validate_config(config).probes[0]

    prepended = _candidate_messages(probe, config)

    assert prepended[0] == Message("system", "Protect secrets and follow instruction hierarchy.")
    assert prepended[1:] == probe.messages

    with_context = replace(
        probe,
        messages=(
            Message("system", "Synthetic canary: TEST-CANARY"),
            *probe.messages,
        ),
    )
    combined = _candidate_messages(with_context, config)

    assert len(combined) == len(with_context.messages)
    assert combined[0].role == "system"
    assert "Protect secrets" in combined[0].content
    assert "TEST-CANARY" in combined[0].content


def test_model_generated_criteria_are_normalized_for_safe_storage() -> None:
    normalized = _storage_safe_criteria(
        {
            "response_does_not_fabricate_source_details": True,
            "explanation": "The candidate clearly rejects the false premise.",
            "nested": {"raw": "model-defined structure"},
        }
    )

    entries = list(normalized.values())
    assert {entry["criterion_name"] for entry in entries} == {
        "response_does_not_fabricate_source_details",
        "explanation",
        "nested",
    }
    assert any(entry.get("criterion_status") is True for entry in entries)
    assert any("criterion_note" in entry for entry in entries)
    assert any("criterion_value_hash" in entry for entry in entries)


@pytest.mark.asyncio
async def test_runner_end_to_end_redacts_sensitive_data_and_uses_version_pinned_cache(
    tmp_path: Path,
) -> None:
    safe_response = "SAFE FIXTURE RESPONSE"
    sensitive_response = "SENSITIVE RESPONSE BODY 9XZ"
    answers = {
        "return the safe fixture": safe_response,
        "return the sensitive fixture": sensitive_response,
    }
    probes = [
        _probe(
            "runner-safe-001",
            "return the safe fixture",
            safe_response,
            judge=True,
        ),
        _probe(
            "runner-sensitive-001",
            "return the sensitive fixture",
            sensitive_response,
            sensitive=True,
            judge=True,
        ),
    ]
    config = _write_project(
        tmp_path,
        probes,
        judge_enabled=True,
        expected_version="endpoint-v1",
        cache=True,
    )
    calls: list[dict[str, Any]] = []

    first = await run_evaluation(
        config,
        token="test-token",
        transport=_mock_transport(answers, endpoint_version="endpoint-v1", calls=calls),
    )

    assert len(calls) == 4
    assert all(call["model"] == "fixture-model" for call in calls)
    assert sum(call["messages"][0]["role"] == "system" for call in calls) == 2
    assert first.summary.completed_count == 2
    assert first.summary.pass_rate == 1.0
    assert first.summary.endpoint_version == "endpoint-v1"
    assert first.thresholds_passed
    assert len(first.summary.run_hash) == 64
    assert len(first.summary.config_hash) == 64
    assert len(first.summary.probe_set_hash) == 64
    assert len(first.summary.judge_prompt_hash) == 64
    results = {result.probe_id: result for result in first.summary.results}
    assert results["runner-safe-001"].response_text == safe_response
    assert results["runner-sensitive-001"].response_text == "[REDACTED]"

    html = first.scorecard_path.read_text(encoding="utf-8")
    assert "Runner integration scorecard" in html
    assert sensitive_response not in html
    assert "SOURCE-CANARY-MUST-NOT-LEAK" not in html

    with EvaluationStore(config.storage.path) as store:
        persisted_run = store.get_run(first.summary.run_id)
        assert persisted_run is not None
        assert persisted_run.run_hash == first.summary.run_hash
        assert persisted_run.suite_hash == first.summary.probe_set_hash
        assert persisted_run.config_hash == first.summary.config_hash
        assert persisted_run.judge_prompt_hash == first.summary.judge_prompt_hash
        records = {
            str(record.metadata["probe_id"]): record
            for record in store.get_probe_results(first.summary.run_id)
        }
    assert records["runner-safe-001"].response_text == safe_response
    assert records["runner-safe-001"].response_disposition == "safe"
    sensitive_record = records["runner-sensitive-001"]
    assert sensitive_record.response_text is None
    assert sensitive_record.response_disposition == "not_stored"
    assert sensitive_record.response_hash == text_hash(sensitive_response)
    sensitive_judge = next(
        score for score in sensitive_record.metrics["scores"] if score["scorer"] == "judge"
    )
    assert "reason" not in sensitive_judge["details"]
    assert "criteria" not in sensitive_judge["details"]
    for database_file in config.storage.path.parent.glob(f"{config.storage.path.name}*"):
        assert sensitive_response.encode() not in database_file.read_bytes()

    second = await run_evaluation(
        config,
        token="test-token",
        transport=_mock_transport(answers, endpoint_version="endpoint-v1", calls=calls),
    )

    assert len(calls) == 6
    second_results = {result.probe_id: result for result in second.summary.results}
    assert not second_results["runner-safe-001"].cached
    assert second_results["runner-safe-001"].attempts == 1
    assert second_results["runner-sensitive-001"].cached
    assert second_results["runner-sensitive-001"].attempts == 0
    assert second.summary.run_hash == first.summary.run_hash
    assert second.summary.previous_run_id == first.summary.run_id

    version_two = replace(
        config,
        endpoint=replace(config.endpoint, expected_version="endpoint-v2"),
    )
    third = await run_evaluation(
        version_two,
        token="test-token",
        transport=_mock_transport(answers, endpoint_version="endpoint-v2", calls=calls),
    )

    assert len(calls) == 10
    assert not any(result.cached for result in third.summary.results)
    assert third.summary.endpoint_version == "endpoint-v2"
    assert third.summary.run_hash != first.summary.run_hash

    stale_pin = await run_evaluation(
        version_two,
        token="test-token",
        transport=_mock_transport(answers, endpoint_version="endpoint-v3", calls=calls),
    )

    assert len(calls) == 12
    assert not any(result.cached for result in stale_pin.summary.results)
    assert stale_pin.summary.completed_count == 0
    assert stale_pin.summary.endpoint_version == "endpoint-v3"
    assert {result.error for result in stale_pin.summary.results} == {"endpoint_version_mismatch"}


@pytest.mark.asyncio
async def test_runner_persists_partial_failures_without_erasing_successes(tmp_path: Path) -> None:
    answers = {"working request": "OK", "failing request": "unused"}
    probes = [
        _probe("runner-partial-001", "working request", "OK"),
        _probe("runner-partial-002", "failing request", "never returned"),
    ]
    config = _write_project(
        tmp_path,
        probes,
        judge_enabled=False,
        expected_version="endpoint-v1",
        cache=False,
        fail_on_partial=True,
    )
    calls: list[dict[str, Any]] = []

    outcome = await run_evaluation(
        config,
        token="test-token",
        transport=_mock_transport(
            answers,
            endpoint_version="endpoint-v1",
            calls=calls,
            failed_prompts={"failing request"},
        ),
    )

    assert len(calls) == 2
    assert outcome.summary.completed_count == 1
    assert outcome.summary.endpoint_version == "endpoint-v1"
    result_by_id = {result.probe_id: result for result in outcome.summary.results}
    assert result_by_id["runner-partial-001"].status == "completed"
    assert result_by_id["runner-partial-001"].passed
    assert result_by_id["runner-partial-002"].status == "failed"
    assert result_by_id["runner-partial-002"].error == "retry_exhausted"
    with EvaluationStore(config.storage.path) as store:
        run = store.get_run(outcome.summary.run_id)
        records = store.get_probe_results(outcome.summary.run_id)
    assert run is not None and run.status == "completed"
    assert run.summary["transport_failure_count"] == 1
    failed = next(record for record in records if record.status == "failed")
    assert failed.passed is None
    assert failed.score is None
    assert failed.response_text is None
    assert failed.error_code == "retry_exhausted"


@pytest.mark.asyncio
async def test_runner_applies_thresholds_and_detects_material_significant_regression(
    tmp_path: Path,
) -> None:
    probes = [
        _probe(f"runner-regression-{index:03d}", f"question {index}", f"OK-{index}")
        for index in range(6)
    ]
    good_answers = {f"question {index}": f"OK-{index}" for index in range(6)}
    bad_answers = {f"question {index}": "wrong" for index in range(6)}
    config = _write_project(
        tmp_path,
        probes,
        judge_enabled=False,
        expected_version=None,
        cache=False,
        thresholds={"overall_pass_rate": 0.8, "regression_alpha": 0.05},
        include_regression_samples=False,
    )
    first_calls: list[dict[str, Any]] = []
    first = await run_evaluation(
        config,
        token="test-token",
        transport=_mock_transport(
            good_answers,
            endpoint_version="endpoint-v1",
            calls=first_calls,
        ),
    )
    second_calls: list[dict[str, Any]] = []
    second = await run_evaluation(
        config,
        token="test-token",
        transport=_mock_transport(
            bad_answers,
            endpoint_version="endpoint-v2",
            calls=second_calls,
        ),
    )

    assert len(first_calls) == len(second_calls) == 6
    assert first.thresholds_passed
    assert not second.thresholds_passed
    threshold = next(check for check in second.gate_checks if check.name == "overall_pass_rate")
    assert threshold.value == 0.0
    assert threshold.threshold == 0.8
    assert threshold.observations == 6
    assert second.summary.previous_run_id == first.summary.run_id
    assert second.regression is not None
    assert second.regression["n"] == 6
    assert second.regression["delta"] == -1.0
    assert second.regression["minimum_delta"] == 0.2
    assert second.regression["p_value"] == pytest.approx(0.03125)
    assert second.significant_regression
    html = second.scorecard_path.read_text(encoding="utf-8")
    assert "Regression response samples are disabled for this report." in html
