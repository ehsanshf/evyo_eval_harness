# Five-minute demo runbook

This runbook makes the day-5 demonstration repeatable. It is a script, not a recorded endpoint result. Replace bracketed values only after a credentialed run completes.

## Before the demo

1. Install the package with `python -m pip install -e '.[dev]'`.
2. Set a fresh `XEVYO_API_KEY` in the process environment. Set `XEVYO_STAGING_URL` only to override the checked-in QA base URL.
3. Confirm that no terminal command, shell history capture, screen recording, or environment dump exposes the token.
4. Run `xeval validate --config evals/nightly.yaml`.
5. For a release comparison, ensure the SQLite history contains a credentialed baseline captured before deployment, then run the current endpoint once. Two reruns of one endpoint version are a stability check, not a release comparison.
6. Review `artifacts/scorecard.html` for response samples before screen-sharing it.

## Demo script

### 0:00 - Boundary

Show the repository tree and say:

> This is a pure black-box consumer. Its only privileged inputs are the staging chat URL and a scoped JWT supplied through environment variables. It does not use backend source, model weights, internal prompts, production data, or internal databases.

### 0:40 - Catalog and extension

Open `evals/goldset.yaml` and one red-team suite. Point out the versioned envelope, globally unique probe ID, messages, deterministic scorer plus optional judge rubric, tags, synthetic canaries, and `sensitive` flag.

Run:

```bash
xeval validate --config evals/nightly.yaml
xeval list-scorers
```

Explain that a non-engineer can copy one YAML entry and validate it offline; custom synchronous scorers use a three-argument plugin contract.

### 1:40 - Resilience and reproducibility

Open `evals/nightly.yaml`. Show concurrency below the tenant rate limit, retry settings, fixed seed, streaming, response limits, endpoint version header, judge prompt version, and output paths.

State that candidate and judge calls share the quota. Explain that probe/config/judge hashes and endpoint version define comparable work, while live service responses can still vary.

### 2:30 - Run

Run without displaying the environment:

```bash
xeval run --config evals/nightly.yaml
```

If staging credentials are not available, do not simulate a success or quote invented metrics. Use the offline validation and a pre-reviewed scorecard produced by a prior credentialed run, clearly labeling its run ID and endpoint version.

### 3:10 - Scorecard

Open `artifacts/scorecard.html` and identify:

- run ID, endpoint version, manifest hashes, and completion coverage;
- overall and category pass/fail slices;
- current value and 95% interval, plus prior delta, p-value, and paired sample size;
- operational/partial failures separated from scored model failures;
- history sparkline and safe regression samples;
- redaction of sensitive probes.

Quote only values visible in the scorecard. Recommended phrasing:

> Run `[run-id]` against endpoint `[version]` measured `[metric]` as `[value]` with displayed 95% CI `[low, high]`. The paired delta against `[baseline-id]` was `[delta]` on `[n]` probes, `p=[value]`.

### 4:20 - CI and limitations

Open `.github/workflows/eval.yml`. Show that pull requests run offline tests only, while a protected scheduled/manual job receives staging secrets and uploads only the scorecard and database.

Close with the limitations: small slices have wide intervals, a non-significant result is not equivalence, the remote service can vary, and the judge is a release gate only after the documented calibration protocol passes.

## Evidence checklist

Record these beside the demo, without secrets:

- commit SHA;
- run ID and baseline run ID;
- endpoint version header values;
- probe-set, config, and judge-prompt hashes;
- completed/total calls and error count;
- scorecard and SQLite artifact names;
- whether the judge calibration release gate passed;
- exact command used.

Never record the JWT, authorization header, raw environment, real PHI, or production/customer data.
