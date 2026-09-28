# M2.9 Candidate Singleton Utility Profiling

M2.9 is an isolated pre-study over the authoritative 4,506-candidate Census
universe. For every candidate `c`, it evaluates `{c}` against the empty design
using the frozen PostgreSQL 16.14 native evaluator and defines utility as
`F(empty) - F({c})`. The run reuses the frozen M2.7 repository and query-local
incidence replay; it performs no ANALYZE, statistics DDL, search resume, or M3
work.

The frozen protocol is [`experiments/census-m2-9-singletons/protocol.json`](../experiments/census-m2-9-singletons/protocol.json).
The complete per-candidate data is in
[`singleton-results.csv`](../experiments/census-m2-9-singletons/singleton-results.csv),
with aggregate distributions in
[`singleton-summary.json`](../experiments/census-m2-9-singletons/singleton-summary.json),
ranking diagnostics in
[`ranking-summary.json`](../experiments/census-m2-9-singletons/ranking-summary.json),
and the M2.7-v2 retrospective in
[`partial-run-retrospective.csv`](../experiments/census-m2-9-singletons/partial-run-retrospective.csv).

## Interpretation boundary

Singleton utility is evidence about usefulness in the empty-design context. A
zero or negative singleton result is not a proof of global contextual
uselessness: interactions can make a candidate useful after other statistics
are selected. The concentration and ranking results are descriptive evidence,
not a cumulative-achievable-gain decomposition and not a screening rule.

Ranking ties are resolved deterministically by higher utility, lower maintenance
cost, lower precedence rank, then candidate ID. The report uses strict floating
comparisons without an epsilon. ABSENT_NATIVE candidates are evaluated normally;
none are filtered or assigned fabricated payloads.

## Coverage and retrospective

The empty baseline is required to equal the M2.7 value before singleton replay.
The profiling artifact records all 2,253 MCV and all 2,253 FD candidates,
including the 1,109 ABSENT_NATIVE FD realizations. The retrospective maps the
exact 36-candidate accepted sequence from the M2.7 protocol-v2 10% checkpoint
to raw and cost-aware singleton ranks. It reports contextual-surprise
candidates separately and does not resume the failed M2.7 search.

## Limitations

The measurements are tied to the frozen Census workload, incidence artifact,
payload repository, empirical maintenance model, and source-built PostgreSQL
16.14 environment. They do not establish a global optimizer decomposition,
universal maintenance costs, or a production candidate-screening policy.
