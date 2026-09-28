# M2.8 Exact Search Scalability Hardening

## Scope and status

M2.7 established a real scalability blocker: its 4506-candidate Census search
was interrupted during the initial 10% greedy ADD round after 215.04 seconds.
M2.8 keeps the exact deterministic best-improvement definition and tests an
admissible objective-side pruning implementation. It does not change CE
semantics, the candidate universe, incidence generation, budget semantics,
maintenance model, PostgreSQL patch, or M2.7 artifacts.

The first-round gate passed. The complete M2.7 budget search has not been
resumed.

## Exact lower bound

For a current state `Y`, objective `F(Y)`, and a conservative affected-query
superset `A(m)`, the implementation uses:

```text
LB(m) = F(Y) - sum(q_i(Y) for i in A(m)) + |A(m)|
```

The current `EvaluationState` supplies both `F(Y)` and every stored q-error
contribution; no SQL, EXPLAIN, or planner round trip is used to compute the
bound. PostgreSQL's positive-truth q-error has estimate floor 1, so every
affected contribution after a move is at least one. Unaffected contributions
remain unchanged. Therefore the expression is an optimistic lower bound.

Incidence is conservative: it may contain false positives but no false
negatives. A false positive enlarges `A(m)`, subtracts an existing contribution
and adds another one, and therefore only makes the bound more optimistic. It
can reduce pruning, but cannot make a valid move disappear.

The safety tests are deliberately asymmetric:

* with no incumbent, prune only when `LB(m) >= F(Y)`, because search accepts
  strict objective improvements only;
* with an incumbent neighbor, prune only when `LB(m) > F_best`;
* equality with the incumbent is always evaluated, preserving maintenance-cost
  and precedence-rank tie behavior.

No ABSENT_NATIVE candidate receives special treatment. It is pruned only if the
same bound proves that its move cannot win.

## Implementation

`SearchConfig(exact_bound_pruning=False)` retains the exhaustive reference
path. The default exact path uses the evaluator's frozen `IncidenceIndex`,
streams moves in the historical order (ADD, DROP, SWAP with the existing
precedence nesting), and keeps only the current best evaluated neighbor. The
previous full move-list materialization is avoided in the optimized path.

Additive maintenance cost is updated exactly as:

```text
ADD   current + cost(incoming)
DROP  current - cost(outgoing)
SWAP  current - cost(outgoing) + cost(incoming)
```

Search results now persist total neighbor moves, no-improvement bound prunes,
incumbent bound prunes, and the new search configuration. Format-1 artifacts
remain readable and are explicitly interpreted as exhaustive pre-pruning runs;
new artifacts use format 2. Detailed pruned-move records are optional and are
disabled by default for large runs.

## Differential evidence

The unit suite covers q-error floor validation, unknown-query rejection,
no-incumbent pruning, strict incumbent inequality, cost/rank tie handling,
incremental cost equality, full greedy/local ADD/DROP/SWAP differential
behavior, and 40 fixed-seed randomized exhaustive-vs-pruned landscapes.

The disposable PostgreSQL fixture passed an optimized-vs-exhaustive native
differential test. Selected design, objective, maintenance cost, and accepted
trajectory were identical; the full-reference exhaustive control also agreed.

## Census first-round profile

The profile uses the frozen M2.7 repository, 468 queries, all 4506 candidates,
and the predefined 10% budget `1146.45031219779468381`. It executes only the
initial greedy ADD round, not the M2.7 search.

| mode | feasible moves | bound-pruned | native moves | neighbor planner calls | elapsed |
| --- | ---: | ---: | ---: | ---: | ---: |
| exhaustive | 4506 | 0 | 4506 | 19996 | 27.269 s |
| exact-pruned | 4506 | 4292 | 214 | 1080 | 4.181 s |

The pruning rate is `4292 / 4506 = 95.25%`; neighbor planner calls fall by
`94.60%`, and the controlled elapsed speedup is `6.52x`. Both modes select the
same first ADD move, `cand_449fa599b32af3a5039b`, with objective
`2949.9782311993554` and cost `1.747411898796996`. The optimized run first
finds the final first-round winner at evaluated-neighbor index 209.

An additional optimized-only profile of three greedy rounds evaluated 214,
272, and 316 neighbors respectively, with per-round pruning counts 4292,
4233, and 4188. Objectives decreased from `5586.930692724469` to
`2949.9782311993554`, `2085.776550877465`, and `1671.317096999538`; the three
rounds took 14.65 seconds in total. This is projection evidence only, not an
authoritative M2.7 budget result.

Starting from that three-candidate state, one optimized local ADD/DROP/SWAP
round enumerated 18,015 moves, pruned 17,397 (96.57%), evaluated 618 native
moves, and took 28.20 seconds. It accepted a local improvement from
`1671.317096999538` to `1521.7271293414956`. This profile is also not an
authoritative M2.7 result, but it shows that the first measured local
neighborhood is not obviously infeasible under exact pruning.

The descriptive neighborhood sizes remain unchanged. With `N=4506` and `k`
selected candidates, greedy ADD and local ADD each enumerate `N-k` moves,
DROP enumerates `k`, and SWAP enumerates `k(N-k)`. The profile demonstrates
that the initial greedy round is no longer obviously infeasible, but it does
not establish the cost of every later local round or complete M2.7.

## Artifacts and limitations

* `experiments/census-m2-8/census-first-round.json`
* `experiments/census-m2-8/census-rounds.json`
* `experiments/census-m2-8/census-local-round.json`
* `experiments/census-m2-8/native-differential.json`
* `tools/m2_8_search_scalability.py`

No candidate cap, workload reduction, heuristic score, query memoization, or
PostgreSQL patch change was introduced. Full M2.7 resumption remains a separate
decision after review of this exact-pruning evidence.
