"""Strict YAML configuration loading with explicit environment indirection."""

from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigurationError
from .hashing import content_hash

_ENV_PATTERN = re.compile(r"^\$\{([A-Z][A-Z0-9_]*)\}$")


@dataclass(frozen=True, slots=True)
class EndpointConfig:
    url: str
    jwt_env: str = "XEVYO_JWT"
    version_header: str = "x-xevyo-version"
    expected_version: str | None = None


@dataclass(frozen=True, slots=True)
class RequestConfig:
    model: str = "xevyo"
    stream: bool = True
    temperature: float = 0.2
    max_tokens: int = 1500
    send_conversation_ids: bool = True


@dataclass(frozen=True, slots=True)
class RunnerOptions:
    concurrency: int = 4
    rate_limit_per_minute: int = 60
    retries: int = 4
    timeout_seconds: float = 90.0
    backoff_initial_seconds: float = 0.5
    backoff_max_seconds: float = 30.0
    seed: int = 2026
    cache: bool = True
    fail_on_partial: bool = False


@dataclass(frozen=True, slots=True)
class JudgeConfig:
    enabled: bool = True
    prompt_version: str = "judge-v1"
    pass_threshold: float = 0.7
    temperature: float = 0.0
    max_tokens: int = 500
    system_prompt: str | None = None


@dataclass(frozen=True, slots=True)
class StorageConfig:
    path: Path = Path("artifacts/xeval.sqlite3")
    retain_safe_responses: bool = True


@dataclass(frozen=True, slots=True)
class ReportConfig:
    output: Path = Path("artifacts/scorecard.html")
    title: str = "Xevyo Evaluation Scorecard"
    regression_delta: float = 0.05
    confidence_level: float = 0.95
    bootstrap_samples: int = 10_000
    compare_previous: bool = True
    include_regression_samples: bool = True
    redact_sensitive: bool = True


@dataclass(frozen=True, slots=True)
class AppConfig:
    path: Path
    version: int
    name: str
    suites: tuple[Path, ...]
    endpoint: EndpointConfig
    request: RequestConfig = RequestConfig()
    runner: RunnerOptions = RunnerOptions()
    judge: JudgeConfig = JudgeConfig()
    storage: StorageConfig = StorageConfig()
    report: ReportConfig = ReportConfig()
    plugins: tuple[str, ...] = ()
    thresholds: Mapping[str, Any] = field(default_factory=dict)
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @property
    def config_hash(self) -> str:
        # The JWT value is never loaded into this object, so it cannot enter the hash.
        data = asdict(self)
        data.pop("raw", None)
        data.pop("path", None)
        # Suite content and versions are pinned separately by probe_set_hash;
        # absolute filesystem locations must not make the same run machine-specific.
        data.pop("suites", None)
        # Artifact destinations and display-only labels do not affect execution or
        # baseline compatibility.
        data.pop("storage", None)
        endpoint = data.get("endpoint", {})
        if isinstance(endpoint, dict):
            # The observed response header is pinned in the final run hash; a
            # changed expected version must not make the prior release incomparable.
            endpoint.pop("expected_version", None)
        report = data.get("report", {})
        if isinstance(report, dict):
            report.pop("output", None)
            report.pop("title", None)
        return content_hash(data)


def _mapping(value: Any, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigurationError(f"{field_name} must be a mapping")
    return value


def _reject_unknown(value: Mapping[str, Any], allowed: set[str], field_name: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ConfigurationError(f"{field_name} contains unknown keys: {', '.join(unknown)}")


def _resolve_env_literal(value: Any, *, required: bool = False) -> Any:
    if not isinstance(value, str):
        return value
    match = _ENV_PATTERN.fullmatch(value.strip())
    if not match:
        return value
    name = match.group(1)
    resolved = os.getenv(name)
    if resolved is None and required:
        raise ConfigurationError(f"required environment variable {name} is not set")
    return resolved or ""


def _as_int(value: Any, name: str) -> int:
    try:
        if isinstance(value, bool):
            raise ValueError
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc


def _as_float(value: Any, name: str) -> float:
    try:
        if isinstance(value, bool):
            raise ValueError
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{name} must be a number") from exc
    if not math.isfinite(result):
        raise ConfigurationError(f"{name} must be finite")
    return result


def _as_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigurationError(f"{name} must be true or false")
    return value


def _path(base: Path, value: str | Path) -> Path:
    result = Path(value)
    return result if result.is_absolute() else (base / result).resolve()


def load_config(path: str | Path, *, require_credentials: bool = False) -> AppConfig:
    config_path = Path(path).expanduser().resolve()
    if not config_path.is_file():
        raise ConfigurationError(f"configuration file does not exist: {config_path}")
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"invalid YAML in {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigurationError("configuration root must be a mapping")
    _reject_unknown(
        raw,
        {
            "version",
            "name",
            "endpoint",
            "model",
            "suites",
            "probe_files",
            "request",
            "runner",
            "run",
            "judge",
            "storage",
            "report",
            "thresholds",
            "plugins",
        },
        "configuration",
    )

    version = _as_int(raw.get("version", 1), "version")
    if version != 1:
        raise ConfigurationError(f"unsupported config version {version}; expected 1")
    name = str(raw.get("name", config_path.stem)).strip()
    if not name:
        raise ConfigurationError("config name cannot be empty")
    if "runner" in raw and "run" in raw:
        raise ConfigurationError("use runner or run, not both")
    if "suites" in raw and "probe_files" in raw:
        raise ConfigurationError("use suites or probe_files, not both")

    endpoint_data = _mapping(raw.get("endpoint"), "endpoint")
    _reject_unknown(
        endpoint_data,
        {
            "url",
            "url_env",
            "env",
            "jwt_env",
            "api_key_env",
            "version_header",
            "expected_version",
        },
        "endpoint",
    )
    url = endpoint_data.get("url")
    url_env = endpoint_data.get("url_env", endpoint_data.get("env"))
    if url_env and os.getenv(str(url_env)):
        url = os.getenv(str(url_env))
    url = _resolve_env_literal(url or "${XEVYO_STAGING_URL}", required=require_credentials)
    if require_credentials and not url:
        raise ConfigurationError("endpoint URL is empty")
    if "jwt_env" in endpoint_data and "api_key_env" in endpoint_data:
        raise ConfigurationError("endpoint may define jwt_env or api_key_env, not both")
    credential_env = str(
        endpoint_data.get("api_key_env", endpoint_data.get("jwt_env", "XEVYO_JWT"))
    )
    if require_credentials and not os.getenv(credential_env):
        raise ConfigurationError(f"required environment variable {credential_env} is not set")
    endpoint = EndpointConfig(
        url=str(url),
        # Kept as jwt_env internally for backwards compatibility with existing
        # configurations. It may name either a JWT or an OpenAI-style API key.
        jwt_env=credential_env,
        version_header=str(endpoint_data.get("version_header", "x-xevyo-version")).lower(),
        expected_version=endpoint_data.get("expected_version"),
    )

    runner_data = _mapping(raw.get("runner", raw.get("run")), "runner")
    _reject_unknown(
        runner_data,
        {
            "concurrency",
            "rate_limit_per_minute",
            "retries",
            "max_retries",
            "timeout_seconds",
            "request_timeout_seconds",
            "backoff_initial_seconds",
            "backoff_max_seconds",
            "seed",
            "cache",
            "fail_on_partial",
            "model",
            "stream",
            "temperature",
            "max_tokens",
            "send_conversation_ids",
        },
        "runner",
    )
    explicit_request_data = _mapping(raw.get("request"), "request")
    _reject_unknown(
        explicit_request_data,
        {"model", "stream", "temperature", "max_tokens", "send_conversation_ids"},
        "request",
    )
    request_data = explicit_request_data or runner_data
    request = RequestConfig(
        model=str(request_data.get("model", raw.get("model", "xevyo"))),
        stream=_as_bool(request_data.get("stream", True), "request.stream"),
        temperature=_as_float(request_data.get("temperature", 0.2), "request.temperature"),
        max_tokens=_as_int(request_data.get("max_tokens", 1500), "request.max_tokens"),
        send_conversation_ids=_as_bool(
            request_data.get("send_conversation_ids", True),
            "request.send_conversation_ids",
        ),
    )
    if not request.model.strip():
        raise ConfigurationError("request.model cannot be empty")
    if not 0 <= request.temperature <= 2:
        raise ConfigurationError("request.temperature must be between 0 and 2")
    if request.max_tokens < 1:
        raise ConfigurationError("request.max_tokens must be positive")

    runner = RunnerOptions(
        concurrency=_as_int(runner_data.get("concurrency", 4), "runner.concurrency"),
        rate_limit_per_minute=_as_int(
            runner_data.get("rate_limit_per_minute", 60), "runner.rate_limit_per_minute"
        ),
        retries=_as_int(
            runner_data.get("retries", runner_data.get("max_retries", 4)),
            "runner.retries",
        ),
        timeout_seconds=_as_float(
            runner_data.get("timeout_seconds", runner_data.get("request_timeout_seconds", 90)),
            "runner.timeout_seconds",
        ),
        backoff_initial_seconds=_as_float(
            runner_data.get("backoff_initial_seconds", 0.5),
            "runner.backoff_initial_seconds",
        ),
        backoff_max_seconds=_as_float(
            runner_data.get("backoff_max_seconds", 30.0), "runner.backoff_max_seconds"
        ),
        seed=_as_int(runner_data.get("seed", 2026), "runner.seed"),
        cache=_as_bool(runner_data.get("cache", True), "runner.cache"),
        fail_on_partial=_as_bool(
            runner_data.get("fail_on_partial", False), "runner.fail_on_partial"
        ),
    )
    if runner.concurrency < 1 or runner.rate_limit_per_minute < 1:
        raise ConfigurationError("runner concurrency and rate limit must be positive")
    if runner.retries < 0 or runner.timeout_seconds <= 0:
        raise ConfigurationError("runner retries must be non-negative and timeout must be positive")
    if runner.backoff_initial_seconds < 0 or runner.backoff_max_seconds < 0:
        raise ConfigurationError("runner backoff values must be non-negative")
    if runner.backoff_initial_seconds > runner.backoff_max_seconds:
        raise ConfigurationError("runner backoff_initial_seconds cannot exceed backoff_max_seconds")

    judge_data = _mapping(raw.get("judge"), "judge")
    _reject_unknown(
        judge_data,
        {
            "enabled",
            "prompt_version",
            "pass_threshold",
            "temperature",
            "max_tokens",
            "system_prompt",
        },
        "judge",
    )
    judge = JudgeConfig(
        enabled=_as_bool(judge_data.get("enabled", True), "judge.enabled"),
        prompt_version=str(judge_data.get("prompt_version", "judge-v1")),
        pass_threshold=_as_float(judge_data.get("pass_threshold", 0.7), "judge.pass_threshold"),
        temperature=_as_float(judge_data.get("temperature", 0.0), "judge.temperature"),
        max_tokens=_as_int(judge_data.get("max_tokens", 500), "judge.max_tokens"),
        system_prompt=judge_data.get("system_prompt"),
    )
    if not 0 <= judge.pass_threshold <= 1:
        raise ConfigurationError("judge.pass_threshold must be between 0 and 1")

    report_data = _mapping(raw.get("report"), "report")
    storage_data = _mapping(raw.get("storage"), "storage")
    _reject_unknown(storage_data, {"path", "retain_safe_responses"}, "storage")
    _reject_unknown(
        report_data,
        {
            "output",
            "history_db",
            "title",
            "regression_delta",
            "confidence_level",
            "bootstrap_samples",
            "compare_previous",
            "include_regression_samples",
            "redact_sensitive",
        },
        "report",
    )
    storage_path = storage_data.get(
        "path", report_data.get("history_db", "../artifacts/xeval.sqlite3")
    )
    storage = StorageConfig(
        path=_path(config_path.parent, storage_path),
        retain_safe_responses=_as_bool(
            storage_data.get("retain_safe_responses", True), "storage.retain_safe_responses"
        ),
    )
    report = ReportConfig(
        output=_path(config_path.parent, report_data.get("output", "../artifacts/scorecard.html")),
        title=str(report_data.get("title", "Xevyo Evaluation Scorecard")),
        regression_delta=_as_float(
            report_data.get("regression_delta", 0.05), "report.regression_delta"
        ),
        confidence_level=_as_float(
            report_data.get("confidence_level", 0.95), "report.confidence_level"
        ),
        bootstrap_samples=_as_int(
            report_data.get("bootstrap_samples", 10_000), "report.bootstrap_samples"
        ),
        compare_previous=_as_bool(
            report_data.get("compare_previous", True), "report.compare_previous"
        ),
        include_regression_samples=_as_bool(
            report_data.get("include_regression_samples", True),
            "report.include_regression_samples",
        ),
        redact_sensitive=_as_bool(
            report_data.get("redact_sensitive", True), "report.redact_sensitive"
        ),
    )
    if not 0 < report.confidence_level < 1:
        raise ConfigurationError("report.confidence_level must be between 0 and 1")
    if report.bootstrap_samples < 100:
        raise ConfigurationError("report.bootstrap_samples must be at least 100")
    if not report.redact_sensitive:
        raise ConfigurationError("report.redact_sensitive cannot be disabled")

    plugin_values = raw.get("plugins", [])
    if not isinstance(plugin_values, list) or not all(
        isinstance(plugin, str) and plugin.strip() for plugin in plugin_values
    ):
        raise ConfigurationError("plugins must be a list of importable module names")

    suite_values = raw.get("suites", raw.get("probe_files"))
    if not isinstance(suite_values, list) or not suite_values:
        raise ConfigurationError("suites must be a non-empty list of YAML paths")
    suites: list[Path] = []
    for item in suite_values:
        suite_path = item.get("path") if isinstance(item, dict) else item
        if not isinstance(suite_path, str) or not suite_path:
            raise ConfigurationError("each suite must be a path string or a mapping with path")
        suites.append(_path(config_path.parent, suite_path))

    return AppConfig(
        path=config_path,
        version=version,
        name=name,
        suites=tuple(suites),
        endpoint=endpoint,
        request=request,
        runner=runner,
        judge=judge,
        storage=storage,
        report=report,
        plugins=tuple(plugin_values),
        thresholds=_mapping(raw.get("thresholds"), "thresholds"),
        raw=raw,
    )
