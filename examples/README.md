# Synthetic scorecard example

[`sample-scorecard.html`](sample-scorecard.html) is generated from three deterministic synthetic
runs by `python scripts/generate_sample_scorecard.py`. It exists only to demonstrate the static report
layout, history, paired comparisons, and safe regression samples without staging credentials.

The example contains no production prompts, real endpoint outputs, PHI, credentials, or measured quality
claims. Run `xeval run --config evals/nightly.yaml` with the scoped staging environment variables to create
an actual scorecard under `artifacts/`.
