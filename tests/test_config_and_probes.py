from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from xeval.config import load_config
from xeval.errors import ConfigurationError
from xeval.probes import load_probe_file, load_probes


def _write_yaml(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    return path


def _base_config() -> dict[str, object]:
    return {
        "version": 1,
        "name": "unit-test",
        "endpoint": {
            "url": "${TEST_XEVYO_URL}",
            "jwt_env": "TEST_XEVYO_JWT",
            "version_header": "X-Service-Version",
        },
        "suites": ["suites/example.yaml"],
        "storage": {"path": "state/history.sqlite3"},
        "report": {"output": "reports/scorecard.html"},
    }


def _base_suite(probe_id: str = "probe-001") -> dict[str, object]:
    return {
        "version": 1,
        "suite": "unit_suite",
        "probes": [
            {
                "id": probe_id,
                "category": "known_answer",
                "messages": [{"role": "user", "content": "Say hello"}],
                "scorers": ["exact_match"],
                "tags": ["unit"],
                "metadata": {"fixture": True},
            }
        ],
    }


def test_config_resolves_environment_and_paths_without_hashing_jwt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _write_yaml(tmp_path / "config" / "nightly.yaml", _base_config())
    monkeypatch.setenv("TEST_XEVYO_URL", "https://staging.example.test")
    monkeypatch.setenv("TEST_XEVYO_JWT", "first-secret")

    first = load_config(config_path, require_credentials=True)
    monkeypatch.setenv("TEST_XEVYO_JWT", "different-secret")
    second = load_config(config_path, require_credentials=True)

    config_dir = config_path.parent.resolve()
    assert first.endpoint.url == "https://staging.example.test"
    assert first.endpoint.jwt_env == "TEST_XEVYO_JWT"
    assert first.endpoint.version_header == "x-service-version"
    assert first.suites == ((config_dir / "suites" / "example.yaml").resolve(),)
    assert first.storage.path == (config_dir / "state" / "history.sqlite3").resolve()
    assert first.report.output == (config_dir / "reports" / "scorecard.html").resolve()
    assert first.config_hash == second.config_hash
    assert "first-secret" not in repr(first)


def test_url_env_overrides_checked_in_url_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = _base_config()
    raw["endpoint"] = {
        "url": "https://checked-in.example.test",
        "url_env": "TEST_OVERRIDE_URL",
        "jwt_env": "TEST_XEVYO_JWT",
    }
    config_path = _write_yaml(tmp_path / "config.yaml", raw)
    monkeypatch.setenv("TEST_OVERRIDE_URL", "https://override.example.test/")
    monkeypatch.setenv("TEST_XEVYO_JWT", "secret")

    config = load_config(config_path, require_credentials=True)

    assert config.endpoint.url == "https://override.example.test/"


def test_api_key_environment_alias_is_supported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = _base_config()
    raw["endpoint"] = {
        "url": "https://qa.example.test/product/v1",
        "api_key_env": "TEST_XEVYO_API_KEY",
    }
    config_path = _write_yaml(tmp_path / "config.yaml", raw)
    monkeypatch.setenv("TEST_XEVYO_API_KEY", "test-only-key")

    config = load_config(config_path, require_credentials=True)

    assert config.endpoint.jwt_env == "TEST_XEVYO_API_KEY"


def test_required_environment_values_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _write_yaml(tmp_path / "config.yaml", _base_config())
    monkeypatch.delenv("TEST_XEVYO_URL", raising=False)
    monkeypatch.delenv("TEST_XEVYO_JWT", raising=False)

    with pytest.raises(ConfigurationError, match="TEST_XEVYO_URL"):
        load_config(config_path, require_credentials=True)

    raw = _base_config()
    raw["endpoint"] = {
        "url": "https://staging.example.test",
        "jwt_env": "TEST_XEVYO_JWT",
    }
    _write_yaml(config_path, raw)
    with pytest.raises(ConfigurationError, match="TEST_XEVYO_JWT"):
        load_config(config_path, require_credentials=True)


@pytest.mark.parametrize(
    ("section", "value", "message"),
    [
        ("request", {"temperature": 2.1}, "temperature"),
        ("request", {"max_tokens": 0}, "max_tokens"),
        (
            "request",
            {"candidate_prompt_version": "safety-v1"},
            "must be set together",
        ),
        ("runner", {"concurrency": 0}, "concurrency"),
        ("runner", {"rate_limit_per_minute": 0}, "rate limit"),
        ("runner", {"retries": -1}, "retries"),
        ("runner", {"timeout_seconds": 0}, "timeout"),
        ("judge", {"pass_threshold": 1.01}, "pass_threshold"),
        ("judge", {"format_retries": 3}, "format_retries"),
        ("report", {"confidence_level": 1}, "confidence_level"),
        ("report", {"bootstrap_samples": 99}, "bootstrap_samples"),
        ("plugins", "not-a-list", "plugins"),
        ("suites", [], "suites"),
    ],
)
def test_config_rejects_invalid_values(
    tmp_path: Path, section: str, value: object, message: str
) -> None:
    raw = _base_config()
    raw[section] = value
    config_path = _write_yaml(tmp_path / f"{section}.yaml", raw)

    with pytest.raises(ConfigurationError, match=message):
        load_config(config_path)


def test_config_wraps_invalid_numeric_input_as_configuration_error(tmp_path: Path) -> None:
    raw = _base_config()
    raw["request"] = {"max_tokens": "many"}
    config_path = _write_yaml(tmp_path / "config.yaml", raw)

    with pytest.raises(ConfigurationError, match="max_tokens"):
        load_config(config_path)


def test_config_rejects_unknown_keys_instead_of_ignoring_typos(tmp_path: Path) -> None:
    raw = _base_config()
    raw["request"] = {"temperatur": 0.0}
    config_path = _write_yaml(tmp_path / "config.yaml", raw)

    with pytest.raises(ConfigurationError, match="request.*temperatur"):
        load_config(config_path)


def test_semantic_config_hash_ignores_artifact_paths_and_expected_release(
    tmp_path: Path,
) -> None:
    raw = _base_config()
    raw["endpoint"]["expected_version"] = "release-v1"  # type: ignore[index]
    first = load_config(_write_yaml(tmp_path / "first.yaml", raw))

    equivalent = deepcopy(raw)
    equivalent["endpoint"]["expected_version"] = "release-v2"  # type: ignore[index]
    equivalent["storage"] = {"path": "other/history.sqlite3"}
    equivalent["report"] = {
        "output": "other/scorecard.html",
        "title": "Different display title",
    }
    second = load_config(_write_yaml(tmp_path / "second.yaml", equivalent))

    changed_request = deepcopy(raw)
    changed_request["request"] = {"temperature": 0.0}
    third = load_config(_write_yaml(tmp_path / "third.yaml", changed_request))

    assert first.config_hash == second.config_hash
    assert first.config_hash != third.config_hash


def test_probe_loader_parses_nested_scorer_params_and_stable_hash(tmp_path: Path) -> None:
    raw = _base_suite()
    probe_row = raw["probes"][0]  # type: ignore[index]
    probe_row["messages"] = [  # type: ignore[index]
        {"role": "system", "content": "Be concise."},
        {"role": "user", "content": "Say hello"},
    ]
    probe_row["scorers"] = [  # type: ignore[index]
        {
            "name": "contains",
            "params": {"values": ["hello"], "mode": "all"},
            "case_sensitive": True,
        }
    ]
    first_path = _write_yaml(tmp_path / "first.yaml", raw)
    first = load_probe_file(first_path)[0]

    reordered = deepcopy(raw)
    reordered_row = reordered["probes"][0]  # type: ignore[index]
    reordered_row["metadata"] = {"fixture": True}  # type: ignore[index]
    second_path = _write_yaml(tmp_path / "second.yaml", reordered)
    second = load_probe_file(second_path)[0]

    assert first.id == "probe-001"
    assert first.last_user_message == "Say hello"
    assert first.scorers[0].name == "contains"
    assert first.scorers[0].params == {
        "values": ["hello"],
        "mode": "all",
        "case_sensitive": True,
    }
    assert first.probe_hash == second.probe_hash
    assert len(first.probe_hash) == 64


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value.update(version=2), "unsupported.*version"),
        (lambda value: value.update(suite="  "), "suite"),
        (lambda value: value["probes"][0].update(id="A"), "invalid id"),
        (lambda value: value["probes"][0].update(category=""), "category"),
        (lambda value: value["probes"][0].update(messages=[]), "messages"),
        (
            lambda value: value["probes"][0].update(
                messages=[{"role": "tool", "content": "not allowed"}]
            ),
            "valid role",
        ),
        (lambda value: value["probes"][0].update(scorers=[]), "scorers"),
        (lambda value: value["probes"][0].update(tags="unit"), "tags"),
        (lambda value: value["probes"][0].update(metadata=[]), "metadata"),
    ],
)
def test_probe_loader_rejects_invalid_suite_shapes(
    tmp_path: Path, mutate: object, message: str
) -> None:
    raw = _base_suite()
    mutate(raw)  # type: ignore[operator]
    suite_path = _write_yaml(tmp_path / "suite.yaml", raw)

    with pytest.raises(ConfigurationError, match=message):
        load_probe_file(suite_path)


def test_load_probes_sorts_ids_and_rejects_cross_file_duplicates(tmp_path: Path) -> None:
    later = _write_yaml(tmp_path / "later.yaml", _base_suite("probe-200"))
    earlier = _write_yaml(tmp_path / "earlier.yaml", _base_suite("probe-100"))

    loaded = load_probes([later, earlier])

    assert [probe.id for probe in loaded] == ["probe-100", "probe-200"]

    duplicate = _write_yaml(tmp_path / "duplicate.yaml", _base_suite("probe-100"))
    with pytest.raises(ConfigurationError, match="duplicate probe id.*probe-100"):
        load_probes([earlier, duplicate])
