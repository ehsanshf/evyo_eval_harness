from __future__ import annotations

from xeval.gates import evaluate_regression
from xeval.reporting import build_scorecard, render_scorecard_html
from xeval.storage import ProbeResultRecord


def _record(*, run_id: str, passed: bool, tags: list[str]) -> ProbeResultRecord:
    return ProbeResultRecord(
        result_id=1,
        run_id=run_id,
        probe_hash="critical-probe-hash",
        category="jailbreak",
        status="completed",
        passed=passed,
        score=0.8,
        latency_ms=100.0,
        error_code=None,
        response_text=None,
        response_hash="response-hash",
        response_disposition="not_stored",
        metrics={},
        metadata={"tags": tags, "sensitive": True},
        created_at="2026-07-18T00:00:00.000Z",
    )


def test_critical_safety_flip_gates_even_without_aggregate_significance() -> None:
    previous = [_record(run_id="previous", passed=True, tags=["red_team", "jailbreak"])]
    current = [_record(run_id="current", passed=False, tags=["red_team", "jailbreak"])]

    result = evaluate_regression(
        current,
        previous,
        seed=2026,
        confidence_level=0.95,
        n_resamples=100,
        alpha=0.05,
        minimum_delta=0.05,
    )

    assert result is not None
    assert not result["material_statistical_regression"]
    assert result["critical_regressions"] == ["critical-probe-hash"]
    assert result["regression"]


def test_scorecard_surfaces_absolute_and_prior_run_release_gates() -> None:
    run = {
        "run_id": "current",
        "status": "completed",
        "started_at": "2026-07-18T00:00:00.000Z",
        "completed_at": "2026-07-18T00:01:00.000Z",
        "endpoint_version": "endpoint-v2",
        "execution_fingerprint": "execution-hash",
        "suite_hash": "suite-hash",
        "config_hash": "config-hash",
        "judge_prompt_hash": "judge-hash",
        "summary": {
            "thresholds_passed": False,
            "gate_checks": [
                {
                    "name": "overall_pass_rate",
                    "value": 0.5,
                    "threshold": 0.85,
                    "operator": ">=",
                    "passed": False,
                    "observations": 2,
                }
            ],
            "regression": {
                "n": 2,
                "delta": -0.2,
                "p_value": 0.04,
                "minimum_delta": 0.05,
                "alpha": 0.05,
                "critical_regressions": [],
                "regression": True,
            },
            "observed_refusal_rate": 0.5,
            "refusal_behavior_pass_rate": 0.75,
            "refusal_observations": 4,
            "format_compliance_rate": 1.0,
            "format_observations": 2,
            "judge_mean_score": 0.6,
            "judge_observations": 3,
        },
    }
    results = [
        {
            "probe_hash": "probe-1",
            "category": "general",
            "status": "completed",
            "passed": False,
            "score": 0.5,
            "latency_ms": 100.0,
            "response_disposition": "not_stored",
        }
    ]

    scorecard = build_scorecard(run, results, seed=2026, bootstrap_samples=100)
    html = render_scorecard_html(scorecard)

    assert scorecard.release_status == "fail"
    assert scorecard.gate_checks[0]["name"] == "overall_pass_rate"
    assert scorecard.regression_gate is not None
    assert "Release gates" in html
    assert "Overall Pass Rate" in html
    assert "Prior-run regression" in html
    assert "Release gate fail" in html
    assert "Observed refusal rate" in html
    assert "Format compliance rate" in html
    assert "Mean judge score" in html
