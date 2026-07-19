from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from xeval.config import JudgeConfig
from xeval.errors import JudgeError
from xeval.models import EndpointResponse, Message, Probe, ScorerSpec
from xeval.scorers.judge import LLMJudge, _parse_judge_json


def _probe() -> Probe:
    return Probe(
        id="judge-001",
        category="quality",
        messages=(Message("user", "What is 2 + 2?"),),
        scorers=(ScorerSpec("judge"),),
        suite="unit",
        suite_version="1",
        source=Path("unit.yaml"),
        metadata={"rubric": "The answer must be correct."},
        probe_hash="probe-hash",
    )


def test_parse_judge_json_accepts_exact_strict_object() -> None:
    text = json.dumps(
        {"score": 0.75, "passed": True, "reason": "Minor wording issue", "criteria": {}}
    )

    value = _parse_judge_json(text)

    assert value["score"] == 0.75
    assert value["passed"] is True


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (
            '```json\n{"score":1,"passed":true,"reason":"ok","criteria":{}}\n```',
            "malformed JSON",
        ),
        ('{"score":1,"passed":true,"reason":"ok","criteria":{}} trailing', "malformed JSON"),
        ("[1, 2, 3]", "JSON object"),
        ('{"score":true,"passed":true,"reason":"ok","criteria":{}}', "score"),
        ('{"score":1.1,"passed":true,"reason":"ok","criteria":{}}', "score"),
        ('{"score":1,"passed":1,"reason":"ok","criteria":{}}', "passed"),
        ('{"score":1,"passed":true,"reason":null,"criteria":{}}', "reason"),
        ('{"score":1,"passed":true,"reason":"ok","criteria":[]}', "criteria"),
    ],
)
def test_parse_judge_json_rejects_framing_and_schema_violations(text: str, message: str) -> None:
    with pytest.raises(JudgeError, match=message):
        _parse_judge_json(text)


@pytest.mark.asyncio
async def test_llm_judge_applies_local_threshold_and_records_reported_disagreement() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def complete(self, messages: tuple[Message, ...], **kwargs: Any) -> EndpointResponse:
            self.calls.append({"messages": messages, **kwargs})
            return EndpointResponse(
                text=json.dumps(
                    {
                        "score": 0.8,
                        "passed": True,
                        "reason": "Acceptable but below the configured gate",
                        "criteria": {"correct": True},
                    }
                ),
                latency_ms=5,
                endpoint_version="judge-v2",
                status_code=200,
                attempts=1,
            )

    client = FakeClient()
    judge = LLMJudge(client, JudgeConfig(pass_threshold=0.9))  # type: ignore[arg-type]

    result = await judge.score("Four", _probe(), {})

    assert result.score == 0.8
    assert not result.passed
    assert result.details["judge_reported_passed"] is True
    assert result.details["judge_pass_consistent"] is False
    assert result.details["judge_endpoint_version"] == "judge-v2"
    assert client.calls[0]["stream"] is False
    assert client.calls[0]["temperature"] == 0.0
    assert client.calls[0]["chat_id"]


@pytest.mark.asyncio
async def test_llm_judge_rejects_invalid_per_probe_threshold_before_calling_endpoint() -> None:
    class NeverClient:
        async def complete(self, *_: object, **__: object) -> EndpointResponse:
            raise AssertionError("endpoint must not be called")

    judge = LLMJudge(NeverClient(), JudgeConfig())  # type: ignore[arg-type]

    with pytest.raises(JudgeError, match="threshold"):
        await judge.score("response", _probe(), {"threshold": 2})
