"""Typed value objects shared by the runner, scorers, storage, and reporting."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

JSONValue = str | int | float | bool | None | list["JSONValue"] | dict[str, "JSONValue"]


@dataclass(frozen=True, slots=True)
class Message:
    role: str
    content: str

    def as_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True, slots=True)
class ScorerSpec:
    name: str
    params: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Probe:
    id: str
    category: str
    messages: tuple[Message, ...]
    scorers: tuple[ScorerSpec, ...]
    suite: str
    suite_version: str
    source: Path
    tags: tuple[str, ...] = ()
    sensitive: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)
    probe_hash: str = ""

    @property
    def last_user_message(self) -> str:
        for message in reversed(self.messages):
            if message.role == "user":
                return message.content
        return ""


@dataclass(frozen=True, slots=True)
class EndpointResponse:
    text: str
    latency_ms: float
    endpoint_version: str
    status_code: int
    attempts: int
    request_id: str | None = None
    raw_usage: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ScoreResult:
    scorer: str
    score: float
    passed: bool
    details: Mapping[str, Any] = field(default_factory=dict)
    error: str | None = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.score <= 1.0:
            raise ValueError(f"score must be in [0, 1], got {self.score}")


@dataclass(frozen=True, slots=True)
class ProbeResult:
    probe_id: str
    probe_hash: str
    suite: str
    category: str
    tags: tuple[str, ...]
    sensitive: bool
    response_text: str
    response_hash: str
    latency_ms: float
    endpoint_version: str
    status: str
    scores: tuple[ScoreResult, ...]
    attempts: int = 1
    cached: bool = False
    error: str | None = None

    @property
    def aggregate_score(self) -> float:
        if not self.scores:
            return 0.0
        return sum(score.score for score in self.scores) / len(self.scores)

    @property
    def passed(self) -> bool:
        return (
            self.status == "completed"
            and bool(self.scores)
            and all(score.passed for score in self.scores)
        )


@dataclass(frozen=True, slots=True)
class RunSummary:
    run_id: str
    run_hash: str
    config_hash: str
    probe_set_hash: str
    judge_prompt_hash: str
    started_at: str
    finished_at: str
    endpoint_version: str
    results: tuple[ProbeResult, ...]
    config_name: str
    seed: int
    previous_run_id: str | None = None

    @property
    def completed_count(self) -> int:
        return sum(result.status == "completed" for result in self.results)

    @property
    def pass_rate(self) -> float:
        completed = [result for result in self.results if result.status == "completed"]
        if not completed:
            return 0.0
        return sum(result.passed for result in completed) / len(completed)

    @property
    def mean_score(self) -> float:
        completed = [result for result in self.results if result.status == "completed"]
        if not completed:
            return 0.0
        return sum(result.aggregate_score for result in completed) / len(completed)
