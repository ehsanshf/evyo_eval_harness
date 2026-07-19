# Xevyo black-box eval and red-team harness

`xeval` is a deterministic Python CLI that exercises the Xevyo staging chat endpoint from the outside. It runs a versioned 30-case goldset and 25 categorized red-team probes, scores each response, stores run history in SQLite, and produces a self-contained HTML scorecard with confidence intervals and paired significance against a comparable prior run.

The harness needs only `POST /v2/chat/completions` and a scoped JWT. It does not use backend source, model weights, internal prompts, production data, Postgres, Redis, OpenSearch, a GPU, or an internal tool name.

> This repository contains no staging credentials and makes no live quality or latency claim. A real result exists only after a credentialed run completes against an identified endpoint version.

## Quick start

Python 3.11 or newer is required.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
xeval validate --config evals/nightly.yaml
```

On PowerShell, activate with `.\.venv\Scripts\Activate.ps1` and quote the install target the same way:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e '.[dev]'
xeval validate --config evals/nightly.yaml
```

Validation is offline. It loads all configured YAML, checks the probe schema and unique IDs, imports configured scorer plugins, and verifies scorer names without contacting staging.

## Run the real evaluation

Set the scoped credentials in the process environment. Do not put them in YAML:

```bash
export XEVYO_STAGING_URL='https://staging.example.invalid'
export XEVYO_JWT='replace-with-scoped-token'
xeval run --config evals/nightly.yaml
```

PowerShell:

```powershell
$env:XEVYO_STAGING_URL = 'https://staging.example.invalid'
$env:XEVYO_JWT = 'replace-with-scoped-token'
xeval run --config evals/nightly.yaml
```

The URL may be the service base or the full `/v2/chat/completions` URL. The checked-in config respects the shared 60 request/minute quota; candidate and judge calls use one limiter. `429` and transient server/network failures are retried with bounded backoff.

Default outputs are:

- `artifacts/scorecard.html` - static report with slices, deltas, uncertainty, history, and safe regression samples;
- `artifacts/xeval.sqlite3` - run manifests, per-probe metrics, hashes, and comparison history.

The checked-in [synthetic scorecard example](examples/sample-scorecard.html) demonstrates the complete
report without presenting mock data as a staging result.

Useful options:

```bash
xeval run --config evals/nightly.yaml --output artifacts/custom.html
xeval run --config evals/nightly.yaml --database artifacts/custom.sqlite3
xeval run --config evals/nightly.yaml --no-cache
xeval run --config evals/nightly.yaml --fail-on-regression
xeval run --config evals/nightly.yaml --fail-on-thresholds
xeval list-scorers --config evals/nightly.yaml
```

`--fail-on-regression` makes a statistically significant, practically large paired mean-score regression non-zero. `--fail-on-thresholds` does the same for absolute pass-rate and p95-latency gates. Use both for release/cron runs; the scorecard and database remain the diagnostic artifacts.

## Probe coverage

| Catalog | Cases | Coverage |
| --- | ---: | --- |
| `evals/goldset.yaml` | 30 | general, medical safety, refusal boundary, multi-turn, structured format, known answer |
| `evals/redteam_jailbreak.yaml` | 5 | role-play, authority, obfuscation, instruction-hierarchy bypass |
| `evals/redteam_prompt_injection.yaml` | 5 | direct, indirect, tool/email/markup injection |
| `evals/redteam_system_prompt_extraction.yaml` | 5 | prompt disclosure and reconstruction |
| `evals/redteam_privacy_exfiltration.yaml` | 5 | cross-tenant, prior-user, credential, record, and log extraction |
| `evals/redteam_hallucination.yaml` | 5 | false premise, fabricated source, unsupported certainty, known-answer misinformation |

All medical, identity, and secret-like values are synthetic fixtures or canaries. See [Add a probe in 10 minutes](docs/adding-a-probe.md) for the YAML schema and plugin contract.

## Configuration

`evals/nightly.yaml` is the reference config. Paths are resolved relative to the config file. Its major sections are:

```yaml
version: 1
name: nightly

endpoint:
  url_env: XEVYO_STAGING_URL
  jwt_env: XEVYO_JWT
  version_header: X-Endpoint-Version

request:
  model: xevyo
  stream: true
  temperature: 0.0
  max_tokens: 1200

runner:
  concurrency: 4
  rate_limit_per_minute: 55
  retries: 4
  timeout_seconds: 90
  seed: 2026
  cache: true
  fail_on_partial: true

judge:
  enabled: true
  prompt_version: judge-v1
  temperature: 0.0
  pass_threshold: 0.8

storage:
  path: ../artifacts/xeval.sqlite3
  retain_safe_responses: true

report:
  output: ../artifacts/scorecard.html
  regression_delta: 0.05
  confidence_level: 0.95

suites:
  - goldset.yaml
  - redteam_jailbreak.yaml
  # ...remaining suite files
```

`endpoint.url` may be used for a non-secret checked-in URL; `endpoint.url_env` is preferred across environments. `endpoint.jwt_env` names the environment variable containing the token, never the token itself. A top-level `plugins` list can name importable scorer modules:

```yaml
plugins:
  - my_package.scorers
```

Probe, config, suite, response, and judge-prompt hashes are recorded for reproducibility. Safe cache reuse requires a known deployment header: set `endpoint.expected_version` to the version you intend to test. Each cached run first executes one live sentinel probe, verifies its response header, then permits cache hits for the remaining probes; a mismatch disables cache reads and fails the mismatched results. Without an expected version, `cache: true` still makes fresh live calls because `unknown` is not a safe cross-release key. Use `--no-cache` whenever measuring fresh service behavior explicitly.

Missing or mixed endpoint-version headers fail the scorecard's reproducibility gate; the harness never substitutes the configured expected value for an absent response header.

## How it works

```text
YAML probes -> bounded async API calls -> rule/judge scorers -> SQLite -> statistics -> HTML
```

- One async client handles SSE or JSON responses, quota pacing, retries, and redacted errors.
- Built-in scorer plugins cover exact matches, containment, refusal, JSON/format checks, synthetic leakage, and latency. The semantic judge calls the same endpoint with a hash-pinned strict-JSON prompt.
- Sensitive probes store hashes and metrics, not raw response samples. Safe response retention can also be disabled.
- Current and prior results are paired by unchanged probe identity. The report uses a fixed-seed 10,000-resample percentile bootstrap for 95% intervals and an exact/Monte Carlo paired sign-flip test for two-sided p-values.
- Aggregate pass/score release gates combine the configured practical delta with statistical significance; latency ceilings and critical security failures remain visible as operational/individual failures.

The full architecture, threat taxonomy, determinism boundary, privacy decisions, prompt draft, and statistical caveats are in [Design](docs/design.md). Judge release criteria are in [Judge calibration](docs/judge-calibration.md).

## CI and cron

`.github/workflows/eval.yml` runs offline validation, lint, and tests on pushes and pull requests. Pull requests never receive staging secrets and never call the endpoint.

The live job runs only on the weekday schedule or a manual dispatch with `run_live` enabled. Configure `XEVYO_STAGING_URL` and `XEVYO_JWT` as secrets in the protected `staging` GitHub environment. When they are absent, the live step is explicitly skipped. The workflow uploads only the HTML scorecard and SQLite database for 30 days; it does not upload logs, environment dumps, or the JWT.

## Development

```bash
python -m pip install -e '.[dev]'
python -m ruff check .
python -m pytest
xeval validate --config evals/nightly.yaml
```

Offline tests should not require network access or credentials. Any real endpoint result should record the endpoint version header and must be reported as measured, not estimated.

## Limitations

- Remote model serving may remain nondeterministic even with temperature `0.0`; the harness guarantees stable inputs, hashes, seeds, pairing, and report calculations, not byte-identical live responses.
- LLM-as-a-judge output is not trustworthy as a release gate until the credentialed calibration protocol passes. Rule-based scorers remain preferable for exact requirements.
- The catalog is meaningful regression coverage, not proof that all attacks, medical cases, or OWASP risks are covered.
- Small slices have wide intervals and low power. “Not significant” is not evidence of equivalence.
- Automatic PHI detection is not a security boundary. Never add real PHI or production data to a probe.
- Static HTML is self-contained, not automatically safe for unrestricted public posting; review samples before sharing.

## Documentation

- [Design and threat model](docs/design.md)
- [Add a probe in 10 minutes](docs/adding-a-probe.md)
- [Judge calibration protocol](docs/judge-calibration.md)
- [Demo runbook](docs/demo.md)
- [Synthetic scorecard example](examples/sample-scorecard.html)
