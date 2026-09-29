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

## M2.7 preflight parser migration — complete

Preparation SQL analysis is being migrated from handwritten lexical inspection to
the PostgreSQL-compatible `pglast` AST parser. This preflight is not the
authoritative M2.7 search and does not mark M2.7 complete.

## M2.7 payload-absence semantics hardening — preflight complete

The preparation repository now distinguishes `PRESENT` from PostgreSQL-native
`ABSENT_NATIVE` realizations. This resolves the acquisition blocker but does
not start or complete the M2.7 authoritative budget search.

## M2.7 authoritative Census search — blocked by scale

The authoritative Census preparation and one frozen acquisition passed, but the
4506-candidate exact best-improvement search was interrupted during the initial
10% greedy round after 215.04 seconds. The failed run is retained as historical
evidence in `experiments/census-m2-7/search-failure.json`; M2.7 is not complete.

## M2.7 protocol-v2 authoritative search — failed-performance

Protocol-v2 reused the frozen Census repository and enabled the exact
q-error-bound pruning path without changing the workload, candidates, incidence,
repository, maintenance model, or search semantics. The 0% budget completed two
exactly matching runs. The first 10% run reached the 1800-second per-budget
ceiling after 36 greedy rounds; no later budget, repeat, full-reference control,
or physical validation was started. The retained partial evidence is under
`experiments/census-m2-7/v2/`, and no protocol-v2 budget curve is authoritative.

## M2.8 exact search scalability hardening — first-round gate complete

The exact q-error lower-bound path, exhaustive oracle, deterministic streaming
neighborhoods, and incremental additive-cost checks passed the small-fixture and
controlled Census first-round gates. The protocol-v2 result shows that this
correctness-preserving pruning is still insufficient to complete the full Census
multi-round search within the frozen ceiling. M2.8 does not change the candidate
universe, CE semantics, maintenance model, or M2.7 status.

## M2.11 Census development screening — complete, non-authoritative

The frozen M2.9 singleton ranking was reused to retain Census's deterministic
raw top five percent (226 of 4,506 candidates) for a development-only
ADD-only feasibility run. The screened run reached an exact deterministic
`add-local-optimum` within the predeclared ten-minute ceiling, and its repeated
run matched the accepted sequence and final result. This does not complete or
replace M2.7, establish a production screening threshold, or transfer the
fraction to other benchmarks.

## M2.12 Census one-local-round probe — complete, non-authoritative

Starting from the persisted M2.11 ADD-local optimum, one exact ADD/DROP/SWAP
neighborhood over the 226-candidate screened catalog completed within the
predeclared ten-minute ceiling and reproduced exactly. It found a small
contextual SWAP improvement, but the SWAP pass dominated runtime. Census
development therefore remains ADD-only by default; this probe does not run a
second local round, change screening, or complete M2.7.

## M2.13 formal singleton-screened development workflow — complete, development-only

The singleton profile and screened candidate set are now first-class,
versioned artifacts with explicit lineage and deterministic ranking. The CLI
supports `singleton-profile`, `screen-candidates`, screened ADD-only search,
and provenance-carrying recommendation/validation stages. The Census smoke
reuses the frozen M2.9 profile and M2.11 five-percent membership; it does not
perform new singleton native evaluations. This workflow formalizes a
development path and does not promote the screen to an authoritative M2.7
optimization or a general screening policy.

## M2.14 screened recommendation/deployment/validation smoke — complete, development-only

The formal Census screened recommendation was exercised through deterministic
`K=1`, `K=5`, and `K=10` prefixes using the normal deployment, fresh-ANALYZE,
physical validation, and cleanup path. Frozen hypothetical objectives matched
the persisted M2.13 trajectory exactly for all prefixes; fresh physical
objectives and compact q-error drift summaries were recorded, and no smoke
statistics remained after cleanup. This integrates the development lifecycle
without changing screening, search, CE semantics, or the unresolved
authoritative M2.7/M3 status.

## M2.15 DMV onboarding and singleton profiling — complete, development-only

The authoritative DMV source was retained as 1,965 raw queries and explicitly
preprocessed with `truth > 0`, excluding `dmv.173` and `dmv.943` for an
effective 1,963-query CE workload. The complete 36-pair/72-candidate universe
was acquired on source-built PostgreSQL 16.14, including native
`ABSENT_NATIVE` realizations, and profiled with the frozen native evaluator.
At the M2.15 checkpoint DMV had no accepted maintenance model, so its singleton
ranking was explicitly unpriced and no Census calibration was transferred. No
budget search or screening fraction was run at that checkpoint; M2.16 below
resolves only the calibration question.

## M2.16 DMV maintenance-cost calibration — complete

An independent source-built PostgreSQL 16.14 calibration on the frozen DMV
arity-two catalog passed the preregistered fit, held-out, repeated-measurement,
same-count subset, and positive-slope gates. The accepted aggregate model is
benchmark-, relation-, target-, arity-, and environment-specific; it does not
transfer Census coefficients or claim per-candidate ANALYZE accuracy. A derived
pricing catalog preserves all 72 M2.15 candidate IDs and supplies descriptive
cost-aware singleton rankings only. No DMV budget search or screening fraction
has been run; the next step is a bounded full-universe ADD-only feasibility
study.

## M2.17c DMV frozen-sample singleton refresh — complete

The DMV singleton profile was refreshed twice from the persisted M2.17b sample,
including a fresh-backend repeat. All 72 candidate identities and the frozen
baseline vector matched exactly; 70 payloads were `PRESENT` and two remained
explicit `ABSENT_NATIVE`. This is a frozen-sample semantic validation artifact,
not a search result, a new acquisition, or a refit of the M2.16 model. The
historical M2.15 profile is retained only for descriptive comparison and is not
used as search input. The next bounded step is the unscreened full-catalog
ADD-only feasibility run.

## M2.17d DMV frozen-sample full-72 ADD-only search — complete

The complete 72-candidate DMV universe was searched from the frozen-sample
empty design with the exact full-catalog-total M2.16 maintenance budget,
deterministic best-improvement ADD-only semantics, and M2.8 lower-bound
pruning. The run reached an add-local-optimum after 32 rounds and 226.178
seconds, selecting 31 candidates (8 MCV and 23 FD) at objective
`22014.061316846422`; a fresh-backend repeat matched the full semantic search
trajectory exactly. The frozen sample, repository, workload, and truth remain
unchanged, and cleanup left no experiment statistics or data rows. DMV's
authoritative CE lineage is now the persisted frozen acquisition sample; its
development search profile uses the full candidate universe and ADD-only
search, without candidate screening.

Earlier DMV experiments without persisted sample lineage remain historical
evidence only and are not exact-continuation inputs. M2.16 remains accepted for
its separate production-style native-ANALYZE maintenance question.

## M3 — plan/runtime validation

Capture baseline and selected-design CE and plans, classify plan-shape changes,
and measure repeated execution latency with medians and distributions.

## Next implementation order

Payload schema and types -> pristine-based PG patch -> clean build -> small fixture
and acquisition -> workload/incidence -> evaluator -> correctness integration tests.
