# M1 deterministic budget-aware search

## Maintenance resource semantics

The constrained resource is recurring statistics-maintenance cost, intended for
future `ANALYZE`/refresh work. It is not storage, DDL creation time, query runtime,
planner runtime, or evaluator runtime. A model is fitted or loaded and frozen
before search; search never updates it.

M1 uses development-only abstract `maintenance-cost-unit` values. They are not an
empirical PostgreSQL cost claim. A future `maintenance-model.json` may record the
model/version, unit, feature schema, fitted parameters, measurement digest,
fitting procedure, and diagnostics.

## Preset model and budget

The replaceable `MaintenanceCostModel` interface estimates candidates and whole
designs and performs unit-aware feasibility checks. The current additive preset
is:

```text
MCV cost = base_mcv + per_column_mcv * arity
FD cost  = base_fd  + per_column_fd  * arity
design cost = sum(candidate costs)
```

Parameters and budgets use non-negative finite `Decimal` values. Provenance
records model type/version, unit, parameter strings, `preset-development`, and
the `(mechanism, arity)` feature schema. Its digest is SHA256 of canonical JSON.
The integration fixture uses `(base_mcv=0, per_column_mcv=1, base_fd=1,
per_column_fd=1)` only to exercise functionality.

## Search boundary and provenance

`DeterministicBudgetSearch` depends only on `CandidateCatalog`, the evaluator
protocol, frozen cost model, and typed budget. It never calls PostgreSQL, EXPLAIN,
payload APIs, or adapter internals. A result records design/objective/cost,
budget/unit, workload/repository/model/catalog digests, configuration, all move
records, call/skip/accept counts, and termination reason.

Every ADD/DROP/SWAP is normalized through fixed global precedence. Its complete
counterfactual cost is checked before evaluator invocation. An infeasible move is
recorded and skipped with zero evaluator and planner calls.

## Phase 1: contextual greedy ADD

Starting from a full evaluation of empty design, each round enumerates unselected
candidates by `(precedence rank, candidate ID)`, evaluates every feasible ADD
against the current accepted state, selects the strict best improvement, accepts
its returned state directly, and repeats. Marginals are never reused across
rounds. The phase stops when no feasible strict improvement exists.

## Phase 2: local refinement

Each round deterministically enumerates ADD, DROP, then the outgoing-by-incoming
SWAP cross product. Feasible moves use `evaluate_move`; reference-test mode uses
`evaluate_design`. The strict best improvement is accepted and enumeration
restarts. Termination means a one-move local optimum, not a global optimum.

Best-improvement tie-breaking is:

1. lower resulting objective;
2. lower resulting maintenance cost;
3. move kind and endpoint precedence ranks;
4. stable endpoint candidate IDs.

No epsilon is used: improvement is exactly `new_objective < current_objective`.

## Correctness evidence

Unit fixtures cover candidate mechanism/arity cost, invalid values and units,
digest determinism, empty/additive designs, budget prechecks, contextual search,
DROP and SWAP improvements, infeasible ADD/SWAP, one-move local optimality,
full-reference equality, and repeatability.

The real M0 5-query/6-candidate PostgreSQL fixture produced:

| Budget | Final design | Cost | Objective | Evaluator calls | Planner calls | Skipped infeasible |
|---:|---|---:|---:|---:|---:|---:|
| 0 | empty | 0 | 646.6666666666666 | 1 | 5 | 12 |
| 4 | overlap_ab, mixed_mcv | 4 | 142.66666666666666 | 16 | 23 | 14 |
| 20 | single_mcv, overlap_ab, fd_ab, mixed_mcv | 9 | 34.666666666666664 | 35 | 45 | 0 |

Budget zero made only the mandatory empty-design full evaluation; all 12
neighborhood attempts were rejected before evaluator invocation. Intermediate
query-local search made 23 planner query calls. Its accepted trajectory, every
intermediate design/objective, final design, and final objective matched the
full-reference search. Repeated loose-budget runs were identical. Registration
remained six total calls, and catalogs remained unchanged.

## Limitations and future fitting

The preset additive model is only a development baseline. Real `ANALYZE` costs
may contain multi-statistics interactions. Future work will collect repeated,
baseline-adjusted maintenance observations, fit and validate prediction error,
freeze a versioned artifact, and then supply that immutable model to the same
search interface. M1 adds no fitting, deployment, batching, global optimization,
or runtime study.
