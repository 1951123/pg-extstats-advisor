# M2.7 Census Search Evaluation

## Status

The authoritative M2.7 search is **failed — performance blocker**. This is
not a complete budget-to-design-to-CE result and must not be reported as an
authoritative optimization curve.

## Frozen preparation that passed

The run used the frozen baseline commit `4cf85c4b83473090d63fb42fc940f8d76e3fa099`,
the source-built PostgreSQL 16.14 installation, the patched planner-side
hypothetical overlay, the Census workload with 468 precise parsed queries,
and one new isolated acquisition realization. The prepared repository contains
4506 candidates (2253 MCV and 2253 FD), with 2253 PRESENT MCV, 0 ABSENT_NATIVE
MCV, 1144 PRESENT FD, 1109 ABSENT_NATIVE FD, and zero missing or corrupt
realizations. Its repository digest is
`c0167aa2a48d2c9cccf1249643dccffd737356ae0d97dcab84ea9ec8a97ad7fe`.

The acquisition performed exactly one ANALYZE before repository freeze. Search
used the frozen repository and did not run ANALYZE, CREATE/ALTER/DROP STATISTICS,
or maintenance measurements.

## Search attempt

The predefined schedule was 0%, 10%, 25%, 50%, 75%, and 100% of the frozen
candidate maintenance cost. The 0% budget completed twice independently with
an identical empty design and objective `5586.930692724469`; both runs stopped
at the one-move local optimum with 1 evaluator call, 0 evaluated moves, 9012
infeasible moves skipped, and 0 accepted moves.

The first 10% run (`1146.45031219779468381` maintenance units) was interrupted
after 215.04 seconds during the initial `greedy-add` phase. It was still
performing native EXPLAIN calls over the 468-query workload and had not
persisted a result. Under the experiment's explicit performance-blocker policy,
the remaining budgets, repeat runs, full-reference control, and physical
validation were not started. No candidate cap, workload reduction, algorithm
change, or payload filtering was introduced.

## Authority and limitations

`experiments/census-m2-7/search-failure.json` and `summary.json` record the
failure status. No budget curve, per-query matrix, local/full control, or
physical validation is claimed. The frozen preparation artifacts remain useful
for a future separately scoped scalability milestone, but this run does not
support conclusions about Census budget-to-design behavior.

## Protocol v2 — exact bound-pruned resumption

Protocol-v1 exposed exhaustive-neighborhood scalability limits. M2.8 then
introduced and differentially validated correctness-preserving exact bound
pruning. Protocol-v2 resumes the identical Census optimization problem with
that execution optimization; it does not change the optimizer's winner
definition, workload, candidate universe, incidence, cost model, budget
schedule, or physical semantics. The frozen protocol is
`experiments/census-m2-7/protocol-v2.json`, with exact-pruned search enabled.

Protocol-v2 reused the protocol-v1 frozen repository (digest
`c0167aa2a48d2c9cccf1249643dccffd737356ae0d97dcab84ea9ec8a97ad7fe`) and did
not run another ANALYZE. The 0% budget was independently repeated and matched
exactly. The first 10% run reached 36 greedy rounds but exceeded the frozen
30-minute per-budget ceiling before completion. Its checkpoint and compact
failure evidence are under `experiments/census-m2-7/v2/`; they are diagnostic,
not an authoritative budget curve. No later budget, 25% full-reference
control, or physical validation was started.

Protocol-v2 therefore remains `failed-performance`; M2.7 is not complete and
M3 remains out of scope.
