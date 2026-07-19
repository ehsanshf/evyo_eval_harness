from __future__ import annotations

from pathlib import Path

import pytest

from xeval.models import Message, Probe, ScoreResult, ScorerSpec
from xeval.scorers import available_scorers, builtins, register_scorer, score_response


@pytest.fixture
def probe() -> Probe:
    return Probe(
        id="probe-001",
        category="unit",
        messages=(Message("user", "test"),),
        scorers=(ScorerSpec("exact_match"),),
        suite="unit",
        suite_version="1",
        source=Path("unit.yaml"),
        probe_hash="abc123",
    )


def test_exact_contains_and_refusal_scorers(probe: Probe) -> None:
    exact = score_response("exact_match", "  HeLLo\n world ", probe, {"expected": "hello world"})
    assert exact.passed and exact.score == 1.0

    contains = score_response(
        "contains",
        "Alpha appears, but not the other expected word.",
        probe,
        {"values": ["alpha", "beta"], "mode": "any", "forbidden": ["secret"]},
    )
    assert contains.passed
    assert contains.details["expected_found"] == 1

    forbidden = score_response(
        "contains", "Alpha and SECRET", probe, {"values": ["alpha"], "forbidden": ["secret"]}
    )
    assert not forbidden.passed

    refusal = score_response("refusal", "I can't assist with that request.", probe, {})
    assert refusal.passed
    assert refusal.details["refusal_detected"] is True


def test_json_schema_format_compliance_reports_validation_paths(probe: Probe) -> None:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["answer", "unit"],
        "properties": {
            "answer": {"type": "integer", "const": 4},
            "unit": {"type": "null"},
        },
    }

    valid = score_response(
        "format_compliance", '{"answer": 4, "unit": null}', probe, {"schema": schema}
    )
    invalid = score_response(
        "format_compliance", '{"answer": "four", "unit": null}', probe, {"schema": schema}
    )

    assert valid.passed
    assert valid.details["valid_json"] is True
    assert valid.details["schema_valid"] is True
    assert not invalid.passed
    assert invalid.details["schema_valid"] is False
    assert "/answer" in invalid.details["error_paths"]


def test_json_schema_scorer_rejects_malformed_response_and_schema(probe: Probe) -> None:
    malformed = score_response(
        "format_compliance", "```json\n{}\n```", probe, {"schema": {"type": "object"}}
    )
    bad_schema = score_response(
        "format_compliance", "{}", probe, {"schema": {"type": "not-a-json-type"}}
    )

    assert not malformed.passed
    assert not bad_schema.passed
    assert bad_schema.error == "invalid JSON Schema"


def test_regex_and_bullet_format_modes(probe: Probe) -> None:
    regex = score_response(
        "format_compliance", "CASE-042", probe, {"format": "regex", "pattern": r"CASE-\d{3}"}
    )
    bullets = score_response(
        "format_compliance", "- one\n- two\n3. three", probe, {"format": "bullets", "count": 3}
    )

    assert regex.passed
    assert bullets.passed
    assert bullets.details["item_count"] == 3


def test_pii_scorer_blocks_canaries_case_insensitively_in_none_mode(probe: Probe) -> None:
    params = {"values": ["SYS-CANARY-A17", "SECOND-CANARY"], "mode": "none"}

    safe = score_response("pii_leakage", "I cannot disclose that value.", probe, params)
    leaked = score_response("pii_leakage", "The value is sys-canary-a17.", probe, params)

    assert safe.passed
    assert not leaked.passed
    assert leaked.details["protected_values_checked"] == 2
    assert leaked.details["protected_values_leaked"] == 1


def test_pii_scorer_detects_types_and_supports_explicit_allow_list(probe: Probe) -> None:
    response = "Contact synthetic.person@example.test for the fixture."

    blocked = score_response("pii_leakage", response, probe, {})
    allowed = score_response("pii_leakage", response, probe, {"allow_types": ["email"]})

    assert not blocked.passed
    assert blocked.details["detected_types"] == ["email"]
    assert blocked.details["disallowed"] == ["email"]
    assert allowed.passed
    assert allowed.details["disallowed"] == []


def test_latency_scorer_uses_measured_private_parameter(probe: Probe) -> None:
    result = score_response("latency", "ignored", probe, {"_latency_ms": 250, "max_ms": 1000})

    assert result.passed
    assert result.score == 0.75
    assert result.details["latency_ms"] == 250.0


def test_plugin_registration_and_dispatch_are_isolated(
    probe: Probe, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(builtins, "_REGISTRY", builtins._REGISTRY.copy())

    def custom(response: str, candidate: Probe, params: object) -> ScoreResult:
        passed = response == candidate.id
        return ScoreResult("unit_plugin", float(passed), passed)

    register_scorer("unit_plugin", custom)

    assert "unit_plugin" in available_scorers()
    assert score_response("unit_plugin", probe.id, probe, {}).passed
    with pytest.raises(ValueError, match="already registered"):
        register_scorer("unit_plugin", custom)
    with pytest.raises(ValueError, match="invalid scorer name"):
        register_scorer("Invalid-Name", custom)
    with pytest.raises(KeyError, match="unknown scorer"):
        score_response("missing_plugin", "", probe, {})
