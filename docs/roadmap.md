# Roadmap

## Bootstrap — current

- Establish a new Git history and package skeleton.
- Freeze architecture, scope, and decisions.
- Establish immutable upstream, read-only reference, tracked-patch, and disposable
  build discipline.
- Audit the legacy overlay and specify its minimal migration.
- Track a pristine-based API-contract patch without claiming M0 capability.

## M0 — native evaluator vertical slice

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

## M1 — deterministic search

Attach contextual ADD-only initialization and deterministic ADD/DROP/SWAP
refinement as clients of the frozen evaluator API.

## M2 — deployment loop

Emit deployable `CREATE STATISTICS`, run fresh `ANALYZE`, collect new native
payloads, and separate semantic fidelity from realization drift.

## M3 — plan/runtime validation

Capture baseline and selected-design CE and plans, classify plan-shape changes,
and measure repeated execution latency with medians and distributions.

## Next implementation order

Payload schema and types -> pristine-based PG patch -> clean build -> small fixture
and acquisition -> workload/incidence -> evaluator -> correctness integration tests.
