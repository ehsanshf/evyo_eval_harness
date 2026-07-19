"""Dependency-light command line interface for local and scheduled evaluations."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from .config import load_config
from .errors import XEvalError
from .runner import run_evaluation
from .scorers import available_scorers, load_plugins
from .validation import validate_config


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xeval",
        description="Black-box evaluation and red-team harness",
    )
    parser.add_argument("--version", action="version", version="%(prog)s 0.1.0")
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser(
        "validate", help="validate config and probe YAML without credentials"
    )
    validate.add_argument("--config", required=True, type=Path)

    run = commands.add_parser("run", help="run probes, persist results, and render a scorecard")
    run.add_argument("--config", required=True, type=Path)
    run.add_argument("--output", type=Path, help="override scorecard path")
    run.add_argument("--database", type=Path, help="override SQLite path")
    run.add_argument("--no-cache", action="store_true", help="disable result cache reads")
    run.add_argument(
        "--fail-on-thresholds",
        action="store_true",
        help="exit non-zero when a configured quality threshold fails",
    )
    run.add_argument(
        "--fail-on-regression",
        action="store_true",
        help="exit non-zero on a statistically significant and practically material regression",
    )

    scorers = commands.add_parser(
        "list-scorers", help="list built-in and configured plugin scorers"
    )
    scorers.add_argument("--config", type=Path, help="load plugin modules from this config")
    return parser


def cli(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate":
            config = load_config(args.config)
            report = validate_config(config)
            print(f"Valid: {report.probe_count} probes across {len(report.suite_counts)} suites")
            print(f"Probe-set hash: {report.probe_set_hash}")
            print(f"Judge-prompt hash: {report.judge_prompt_hash}")
            for name, count in report.suite_counts.items():
                print(f"  {name}: {count}")
            return 0
        if args.command == "list-scorers":
            if args.config:
                config = load_config(args.config)
                load_plugins(config.plugins)
            for name in available_scorers():
                print(name)
            return 0
        if args.command == "run":
            config = load_config(args.config, require_credentials=True)
            if args.output:
                config = replace(
                    config,
                    report=replace(config.report, output=args.output.expanduser().resolve()),
                )
            if args.database:
                config = replace(
                    config,
                    storage=replace(config.storage, path=args.database.expanduser().resolve()),
                )
            if args.no_cache:
                config = replace(config, runner=replace(config.runner, cache=False))
            outcome = asyncio.run(run_evaluation(config))
            summary = outcome.summary
            progress = f"{summary.completed_count}/{len(summary.results)} completed"
            print(f"Run {summary.run_id}: {progress}, {summary.pass_rate:.1%} passed")
            print(f"Endpoint version: {summary.endpoint_version}")
            print(f"Run hash: {summary.run_hash}")
            print(f"Scorecard: {outcome.scorecard_path}")
            failed_checks = [check.name for check in outcome.gate_checks if not check.passed]
            print(
                "Thresholds: "
                + ("PASS" if not failed_checks else f"FAIL ({', '.join(failed_checks)})")
            )
            if outcome.regression:
                print(
                    "Prior-run delta: "
                    f"{float(outcome.regression['delta']):+.3f}, "
                    f"p={float(outcome.regression['p_value']):.4f}"
                )
            gate_failed = args.fail_on_thresholds and bool(failed_checks)
            regression_failed = args.fail_on_regression and outcome.significant_regression
            partial_failed = config.runner.fail_on_partial and (
                summary.completed_count != len(summary.results)
            )
            return 2 if gate_failed or regression_failed or partial_failed else 0
    except (XEvalError, OSError, ImportError, ValueError) as exc:
        print(f"xeval: {exc}", file=sys.stderr)
        return 2
    return 2


def main(argv: Sequence[str] | None = None) -> None:
    raise SystemExit(cli(argv))
