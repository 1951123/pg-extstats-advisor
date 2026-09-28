# Roadmap

## Bootstrap — complete

- Establish a new Git history and package skeleton.
- Freeze architecture, scope, and decisions.
- Establish immutable upstream, read-only reference, tracked-patch, and disposable
  build discipline.
- Audit the legacy overlay and specify its minimal migration.
- Track a pristine-based API-contract patch without claiming M0 capability.

## M0 — native evaluator vertical slice — complete

1. Define typed workload, candidate, design, move, payload, and evaluation records.
2. Implement a versioned native-payload repository and provenance manifest.
3. Produce the minimal PG16 backend-local overlay patch from pristine upstream.
4. Build PostgreSQL exclusively through the clean build script.
5. Implement workload ingestion and conservative MCV/FD incidence.
6. Implement native design activation and target CE extraction.
7. Implement `evaluate_design` and query-local `evaluate_move`.
8. Verify empty, single-MCV, overlapping-MCV, FD-only, mixed, ADD, DROP, SWAP,
   query-reuse, and catalog-immutability cases against physical/native reference.
9. Record zero per-move `ANALYZE` and zero per-move catalog mutation.

M0 deliberately excludes complete search.

## M1 — deterministic search — complete

Contextual ADD-only initialization and deterministic ADD/DROP/SWAP refinement
are clients of the frozen evaluator API, with a typed recurring-maintenance
budget and a replaceable frozen cost-model interface. The current preset is
development-only. Empirical maintenance measurement, fitting, and validation are
a future experimental substage before production interpretation.

## M2 — deployment loop — complete

Emit deployable `CREATE STATISTICS`, run fresh `ANALYZE`, collect new native
payloads, and separate semantic fidelity from realization drift.

## M2.5-A/B — MVP preparation — complete

Versioned workload ingestion, deterministic automatic/explicit candidate generation,
isolated native-payload acquisition, conservative incidence, and structured run
artifacts now produce the existing evaluator inputs.

## M2.5-C — restartable CLI orchestration — complete

Artifact reload, frozen maintenance model, persisted search state, recommendation,
explicit physical validation, acquisition cleanup, and non-interactive stage commands
form the end-to-end MVP. M3 remains future work.

## M2.6 — empirical aggregate maintenance calibration — complete

The offline subsystem provides deterministic arity-two MCV/FD aggregate
configurations, repeated full-ANALYZE timing, same-count deterministic subsets,
transparent OLS, held-out mixed validation, environment/build gates, and a
fail-closed empirical artifact type. The apt PostgreSQL 16.15 run remains rejected
and diagnostic. The source-built PostgreSQL 16.14 Census rerun passed every
predeclared gate and emitted the frozen empirical mechanism-count model. M3 remains
future work.

## M3 — plan/runtime validation

Capture baseline and selected-design CE and plans, classify plan-shape changes,
and measure repeated execution latency with medians and distributions.

## Next implementation order

Payload schema and types -> pristine-based PG patch -> clean build -> small fixture
and acquisition -> workload/incidence -> evaluator -> correctness integration tests.
