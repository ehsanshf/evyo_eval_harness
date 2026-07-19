"""Offline validation for cron preflight and contributor feedback."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from .config import AppConfig
from .errors import ConfigurationError
from .models import Probe
from .probes import load_probes, probe_set_hash
from .scorers import available_scorers, load_plugins
from .scorers.judge import judge_prompt_hash


@dataclass(frozen=True, slots=True)
class ValidationReport:
    probes: tuple[Probe, ...]
    probe_set_hash: str
    judge_prompt_hash: str
    category_counts: dict[str, int]
    suite_counts: dict[str, int]

    @property
    def probe_count(self) -> int:
        return len(self.probes)


def validate_config(config: AppConfig) -> ValidationReport:
    load_plugins(config.plugins)
    probes = load_probes(config.suites)
    known = set(available_scorers())
    category_counts: dict[str, int] = {}
    suite_counts: dict[str, int] = {}
    for probe in probes:
        category_counts[probe.category] = category_counts.get(probe.category, 0) + 1
        suite_counts[probe.suite] = suite_counts.get(probe.suite, 0) + 1
        for scorer in probe.scorers:
            if scorer.name not in known:
                raise ConfigurationError(
                    f"{probe.source}: probe {probe.id}: unknown scorer {scorer.name!r}"
                )
            if scorer.name == "judge" and not config.judge.enabled:
                raise ConfigurationError(
                    f"{probe.source}: probe {probe.id}: judge scorer used while judge is disabled"
                )
            _validate_scorer_params(probe, scorer.name, scorer.params)
    return ValidationReport(
        probes=probes,
        probe_set_hash=probe_set_hash(probes),
        judge_prompt_hash=judge_prompt_hash(config.judge),
        category_counts=dict(sorted(category_counts.items())),
        suite_counts=dict(sorted(suite_counts.items())),
    )


def _validate_scorer_params(probe: Probe, name: str, params: Any) -> None:
    def require_any(*keys: str) -> None:
        if not any(key in params for key in keys):
            raise ConfigurationError(
                f"{probe.source}: probe {probe.id}: scorer {name} requires one of "
                + ", ".join(keys)
            )

    if name == "exact_match":
        require_any("expected", "expected_any", "values")
    elif name == "contains":
        require_any("values", "expected", "contains", "forbidden", "must_not_contain")
        if params.get("mode", "all") not in {"all", "any"}:
            raise ConfigurationError(
                f"{probe.source}: probe {probe.id}: contains mode must be all or any"
            )
    elif name == "format_compliance":
        kind = params.get("format", params.get("type", "json"))
        if kind not in {"json", "regex", "bullets", "bullet_list"}:
            raise ConfigurationError(
                f"{probe.source}: probe {probe.id}: unsupported format {kind!r}"
            )
        schema = params.get("schema")
        if schema is not None:
            if not isinstance(schema, dict):
                raise ConfigurationError(
                    f"{probe.source}: probe {probe.id}: JSON Schema must be a mapping"
                )
            try:
                Draft202012Validator.check_schema(schema)
            except SchemaError as exc:
                raise ConfigurationError(
                    f"{probe.source}: probe {probe.id}: invalid JSON Schema"
                ) from exc
        if kind == "regex":
            pattern = params.get("pattern")
            if not isinstance(pattern, str):
                raise ConfigurationError(
                    f"{probe.source}: probe {probe.id}: regex pattern is required"
                )
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ConfigurationError(
                    f"{probe.source}: probe {probe.id}: invalid regex pattern"
                ) from exc
    elif name == "pii_leakage" and params.get("mode", "none") != "none":
        raise ConfigurationError(f"{probe.source}: probe {probe.id}: pii_leakage mode must be none")
    elif name == "judge" and "threshold" in params:
        threshold = params["threshold"]
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            raise ConfigurationError(
                f"{probe.source}: probe {probe.id}: judge threshold must be numeric"
            )
        if not 0 <= float(threshold) <= 1:
            raise ConfigurationError(
                f"{probe.source}: probe {probe.id}: judge threshold must be between 0 and 1"
            )
