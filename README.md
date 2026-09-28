# pg-extstats-advisor

PostgreSQL workload-aware extended-statistics offline physical-design advisor.

The system acquires candidate payloads once, freezes PostgreSQL-native payloads,
evaluates hypothetical extended-statistics designs with native PostgreSQL CE,
replans only structurally affected workload queries, reuses unaffected objective
contributions, and emits deployable recommendations. Search is a pluggable client
of the evaluator rather than the system's semantic core.

M0 native evaluation, M1 deterministic budget-aware search, and the M2 explicit
recommend/deploy/fresh-ANALYZE/validate loop are implemented. Deployment is never
automatic: callers render a typed plan and explicitly execute it in an isolated
environment. See `docs/m2-deployment-validation.md`.

M2.5-A/B adds a Python preparation API for versioned supplied workloads,
deterministic candidates, isolated one-time native-payload acquisition, and
conservative incidence artifacts.

M2.5-C provides restartable artifact-first orchestration:

```bash
export PGEXT_SOURCE_DSN='host=... dbname=...'
export PGEXT_ACQUISITION_DSN='host=... dbname=...'
pg-extstats-advisor prepare config.json
pg-extstats-advisor search run-dir --budget 10
pg-extstats-advisor recommend run-dir
```

Physical validation is a separate explicit command requiring an isolated
validation DSN. See `docs/m2-5-c-cli.md`.

M2.6 adds an offline `calibrate-maintenance` command and a fail-closed empirical
mechanism-count model for fixed-target, arity-two MCV/FD candidates. Calibration
is never triggered by search. The current Census run is retained as a rejected
calibration and did not produce a model artifact. See
`docs/m2-6-maintenance-calibration.md`.

## Current scope

- PostgreSQL 16.14.
- A fixed, supplied offline workload.
- MCV and functional-dependency extended statistics.
- Two-column candidates with one fixed uniform statistics target.
- Base-relation cardinality-estimation objectives.
- PostgreSQL-native planner and CE semantics.
- Fixed, explicit effective precedence.
- Conservative candidate-to-query incidence and query-level reuse.

## Non-goals

- Learned cardinality estimation or optimizer replacement.
- Full CE-Replay or query-internal semantic incrementality.
- A join-cardinality model.
- Online or autonomous tuning.
- Universal PostgreSQL-version support.
- A new global search algorithm.

## Reproducibility invariant

The PostgreSQL binary used by this project must always be derivable as:

```text
immutable postgresql-16.14.tar.bz2
    + Git-tracked pg/patches/postgresql-16.14-hypothetical-extstats.patch
    = disposable patched build and installed binary
```

The extracted reference tree is read-only and never used as a mutable build
checkout. Generated source and install trees live under ignored `.build/`.

## Python environment

Use the repository-local virtual environment so dependencies cannot leak across
projects:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,postgres]'
.venv/bin/python -m pytest
```

The `.venv/` directory is local and ignored. Dependency declarations belong in
`pyproject.toml`; do not install project packages globally.

## Repository map

- `docs/`: frozen architecture, scope, decisions, and roadmap.
- `src/pg_extstats_advisor/`: advisor components with explicit ownership boundaries.
- `pg/`: upstream checksum, authoritative PostgreSQL patch, and patch workflow.
- `scripts/`: reproducible reference preparation and disposable builds.
- `tests/`: unit and PostgreSQL integration tests.
- `experiments/`: isolated evidence produced after implementation begins.

The tracked PostgreSQL patch implements the M0-A backend-local overlay. M0-B adds
the minimal external native evaluator, repository validation, conservative
incidence, and exact query-level reuse for a small fixed workload. Search and
general workload ingestion have not started. See `docs/m0-postgres-overlay.md`,
`docs/m0-external-evaluator.md`, and `docs/m0-implementation-plan.md`.

M1 adds a deterministic contextual-greedy plus ADD/DROP/SWAP search client with
a frozen, unit-aware maintenance budget. Its preset additive costs are strictly
development fixtures, not measured PostgreSQL maintenance-cost results. See
`docs/m1-budget-aware-search.md`.

M2 adds deterministic deployable SQL, physical deployment with a uniform target,
fresh native payload fingerprints and CE, and a structured comparison against the
frozen hypothetical prediction. It does not add runtime validation or maintenance
cost fitting.
