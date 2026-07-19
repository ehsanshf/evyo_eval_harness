from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from xeval.cli import cli


def _write_cli_project(tmp_path: Path) -> Path:
    suite = {
        "version": 1,
        "suite": "cli_suite",
        "probes": [
            {
                "id": "cli-probe-001",
                "category": "known_answer",
                "messages": [{"role": "user", "content": "Reply OK"}],
                "scorers": [{"name": "exact_match", "params": {"expected": "OK"}}],
                "tags": ["goldset"],
            }
        ],
    }
    (tmp_path / "suite.yaml").write_text(yaml.safe_dump(suite, sort_keys=False), encoding="utf-8")
    config = {
        "version": 1,
        "name": "cli-test",
        "endpoint": {
            "url": "https://staging.example.test",
            "jwt_env": "CLI_TEST_JWT",
        },
        "suites": ["suite.yaml"],
        "request": {"stream": False},
        "runner": {"cache": True},
        "judge": {"enabled": False},
        "storage": {"path": "default.sqlite3"},
        "report": {"output": "default.html"},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return path


def test_validate_command_is_offline_and_prints_reproducibility_hashes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _write_cli_project(tmp_path)
    monkeypatch.delenv("CLI_TEST_JWT", raising=False)

    exit_code = cli(["validate", "--config", str(config_path)])

    output = capsys.readouterr()
    assert exit_code == 0
    assert "Valid: 1 probes across 1 suites" in output.out
    assert "Probe-set hash:" in output.out
    assert "Judge-prompt hash:" in output.out
    assert "cli_suite: 1" in output.out
    assert output.err == ""


def test_list_scorers_is_sorted_and_includes_rule_and_judge_scorers(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli(["list-scorers"])

    output = capsys.readouterr()
    names = output.out.splitlines()
    assert exit_code == 0
    assert names == sorted(names)
    assert {"exact_match", "format_compliance", "judge", "pii_leakage"} <= set(names)


def test_run_command_fails_closed_when_jwt_environment_variable_is_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _write_cli_project(tmp_path)
    monkeypatch.delenv("CLI_TEST_JWT", raising=False)

    exit_code = cli(["run", "--config", str(config_path)])

    output = capsys.readouterr()
    assert exit_code == 2
    assert output.out == ""
    assert "required environment variable CLI_TEST_JWT is not set" in output.err


def test_run_command_applies_overrides_and_optional_threshold_exit_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    cli_module = importlib.import_module("xeval.cli")
    config_path = _write_cli_project(tmp_path)
    monkeypatch.setenv("CLI_TEST_JWT", "test-token")
    captured: list[Any] = []

    async def fake_run(config: Any) -> SimpleNamespace:
        captured.append(config)
        summary = SimpleNamespace(
            run_id="run-cli",
            completed_count=1,
            results=(object(),),
            pass_rate=0.0,
            endpoint_version="endpoint-v1",
            run_hash="a" * 64,
        )
        return SimpleNamespace(
            summary=summary,
            scorecard_path=config.report.output,
            gate_checks=(SimpleNamespace(name="overall_pass_rate", passed=False),),
            regression=None,
            significant_regression=False,
        )

    monkeypatch.setattr(cli_module, "run_evaluation", fake_run)
    output_path = tmp_path / "override" / "scorecard.html"
    database_path = tmp_path / "override" / "history.sqlite3"

    exit_code = cli(
        [
            "run",
            "--config",
            str(config_path),
            "--output",
            str(output_path),
            "--database",
            str(database_path),
            "--no-cache",
            "--fail-on-thresholds",
        ]
    )

    output = capsys.readouterr()
    assert exit_code == 2
    assert captured[0].report.output == output_path.resolve()
    assert captured[0].storage.path == database_path.resolve()
    assert captured[0].runner.cache is False
    assert "Run run-cli: 1/1 completed, 0.0% passed" in output.out
    assert "Thresholds: FAIL (overall_pass_rate)" in output.out
    assert output.err == ""
