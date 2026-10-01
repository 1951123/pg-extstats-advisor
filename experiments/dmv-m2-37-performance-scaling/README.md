# M2.37 Performance Scaling & Break-Even Evaluation

This artifact is a bounded PostgreSQL 16.14 / statistics-target-100 timing
study of the DMV hypothetical extended-statistics substrate.  The frozen
implementation under test is `paper-v1-system` at
`88797e4b82ff1d5c8bbba28dc27987af78ce78ad`; the harnesses and result artifacts
are later research provenance.

## Suites

* **A — configuration-count amortization:** 256 deterministic configurations,
  cumulative prefixes `D = 1,2,4,8,16,32,64,128,256`, one warmup and five
  measured repetitions, using all 1,963 DMV queries.  Physical and
  repository-based paths share the frozen DMV realization and planner-call
  count.  `raw/` contains per-design timing rows and correctness vectors;
  `summaries/` contains cumulative medians and break-even data.
* **B — repository acquisition scaling:** Census prefixes
  `C = 8,16,32,72,226,512,1024,2253,4506`, one warmup and five measured
  repetitions.  The full acquire API is timed and integrity-checked.  The
  product acquisition function exposes one total; internal create/analyze/
  serialization subcomponents were not instrumented to avoid product-code
  changes.
* **C — acquisition-input scaling:** deterministic DMV input relations with
  `N = 30,000,60,000,120,000,240,000` rows and the bounded M2.36 six-design
  suite.  This is cost characterization only; q-error/objective values are
  not treated as valid across changed input populations.

No OS cache flushing was performed.  Truth acquisition and query execution
timing are excluded; only `EXPLAIN (FORMAT JSON)` planning is measured.
SVG figures are dependency-free renderings from the persisted summaries.
