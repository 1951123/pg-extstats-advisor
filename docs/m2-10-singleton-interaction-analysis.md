# M2.10 Singleton Screening Interaction-Rescue Analysis

M2.10 is an observational pre-study over the frozen Census problem. M2.9
provides singleton utility for all 4,506 candidates. Because the M2.7 protocol-v2
checkpoint persisted only round aggregates, M2.10 first replayed exactly the 36
completed greedy rounds, verified each accepted candidate/objective and the
native/bound/planner counters, and stopped before round 37.

The replay captured only actual native-evaluated ADD moves. Bound-pruned moves
are not contextual observations because they have no native objective. No
ANALYZE, statistics DDL, search resume, screened search, threshold selection,
candidate filtering, or M3 work was performed.

For an observed context `Y_t`, contextual improvement is
`F(Y_t) - F(Y_t ∪ {c})`; interaction gain is contextual improvement minus the
frozen singleton improvement `F(empty) - F({c})`. The complete protocol is
[`experiments/census-m2-10-interactions/protocol.json`](../experiments/census-m2-10-interactions/protocol.json).

## Artifacts

- [`contextual-evaluations.csv`](../experiments/census-m2-10-interactions/contextual-evaluations.csv): 86,098 native-evaluated ADD moves only.
- [`candidate-context-summary.csv`](../experiments/census-m2-10-interactions/candidate-context-summary.csv): one row per observed candidate.
- [`singleton-percentile-context-summary.csv`](../experiments/census-m2-10-interactions/singleton-percentile-context-summary.csv): rank-bucket rates and quantiles.
- [`screening-recall.csv`](../experiments/census-m2-10-interactions/screening-recall.csv): retrospective raw and cost-aware ranking recall.
- [`accepted-sequence.csv`](../experiments/census-m2-10-interactions/accepted-sequence.csv): the 36 accepted moves.
- [`surprise-candidates.csv`](../experiments/census-m2-10-interactions/surprise-candidates.csv): low-singleton contextual-surprise examples.
- [`summary.json`](../experiments/census-m2-10-interactions/summary.json): complete metrics and lineage.

## Interpretation boundary

This evidence describes one fixed partial search trajectory. Candidates never
observed were bound-pruned or otherwise absent from native evaluation; they must
not be interpreted as contextually useless. In particular, singleton-zero or
singleton-negative does not imply global uselessness. The analysis does not
select a screening threshold or establish a production screening policy.
