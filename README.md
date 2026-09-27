# pg-extstats-advisor

PostgreSQL workload-aware extended-statistics offline physical-design advisor.

The system acquires candidate payloads once, freezes PostgreSQL-native payloads,
evaluates hypothetical extended-statistics designs with native PostgreSQL CE,
replans only structurally affected workload queries, reuses unaffected objective
contributions, and emits deployable recommendations. Search is a pluggable client
of the evaluator rather than the system's semantic core.

## Current scope

- PostgreSQL 16.14.
- A fixed, supplied offline workload.
- MCV and functional-dependency extended statistics.
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

This bootstrap contains architecture and build preparation only. M0 implementation
has not started.

The tracked PostgreSQL patch implements the M0-A backend-local overlay. M0-B adds
the minimal external native evaluator, repository validation, conservative
incidence, and exact query-level reuse for a small fixed workload. Search and
general workload ingestion have not started. See `docs/m0-postgres-overlay.md`,
`docs/m0-external-evaluator.md`, and `docs/m0-implementation-plan.md`.
