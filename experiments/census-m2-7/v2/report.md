# M2.7 protocol-v2 — failure report

## Status

`experiment_status = failed-performance`. Protocol-v1 and its failure
artifacts remain untouched. Protocol-v2 reused the exact frozen M2.7
acquisition repository; it did not run ANALYZE or statistics DDL during search.

The frozen exact-pruned implementation from M2.8 was enabled explicitly. No
heuristic pruning, candidate cap, workload reduction, ABSENT_NATIVE special
case, or algorithm change was introduced.

## Completed baseline

The 0% budget was run twice independently. Both runs selected the empty design,
had objective `5586.930692724469`, maintenance cost `0`, zero native neighbor
evaluations, and termination `one-move-local-optimum`. The results were exact
matches. Their round checkpoints are retained under `v2/budgets/b000-run1/`
and `v2/budgets/b000-run2/`.

## Performance blocker

The first 10% run used budget `1146.45031219779468381` and the frozen 468-query,
4506-candidate problem. It completed 36 greedy ADD rounds before the frozen
1800-second per-budget ceiling was reached. The runner was stopped before
persisting a selected SearchResult; the round checkpoint is retained under
`v2/budgets/b100-run1/checkpoint.json`.

Across those partial rounds it considered 161,586 moves, pruned 75,488 by the
exact bound, evaluated 86,098 native neighbors, and issued 446,733 affected
query planner calls. Cumulative pruning was 46.72%; the last round pruned only
20.20% and took 87.77 seconds. The selected state at the checkpoint contained
36 candidates, cost `62.906828356691856`, and objective
`1078.5290139604795`.

The early M2.8 first-round result therefore does not extrapolate to the full
multi-round Census search: pruning effectiveness degrades as the selected set
grows and local/greedy rounds become more expensive. The later budgets, the
second 10% repeat, the 25% local/full-reference control, the budget curve, and
physical validation were not started.

## Authority

No partial 10% state is an authoritative budget result. The exact-pruned
implementation remains correctness-tested, but this protocol-v2 run does not
meet the all-six-budget exit criteria. M2.7 remains performance-blocked and M3
remains prohibited.
