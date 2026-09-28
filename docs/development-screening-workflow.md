# Singleton-screened development workflow

The reusable development path is deliberately explicit:

```text
prepare raw candidate universe
    -> singleton profile artifact
    -> inspect and choose a benchmark-specific fraction
    -> screened candidate-set artifact
    -> deterministic ADD-only development search
    -> recommendation
```

Singleton profiling is an ordinary library/CLI capability. It evaluates or
imports one frozen singleton result per raw candidate and records workload,
catalog, incidence, repository, maintenance-model, and evaluator-build
lineage. A screening artifact then applies the generic
`singleton_top_fraction` rule with the frozen M2.9 ordering:

1. descending singleton improvement;
2. ascending maintenance cost;
3. ascending candidate precedence;
4. ascending candidate ID.

The retained count is `ceil(N * fraction)`, with `0 < fraction <= 1` and at
least one retained candidate. Screening is a heuristic visibility restriction;
it is not an exactness, dominance, or globally-uselessness claim. Search is
deterministic and exact only within the visible candidate set.

Census currently has an explicit development configuration of top 5% followed
by ADD-only search. This is not a universal default and is not used implicitly
for DMV or future benchmarks. Each benchmark must profile singleton utility
first and choose its own explicit development fraction (or use the full
candidate universe). Invalid or mismatched artifacts fail closed; the search
path never silently recomputes a profile or falls back to the raw catalog.

The workflow performs no acquisition, `ANALYZE`, sampling, statistics DDL, or
payload rebuild in the screened-search hot path. Recommendations remain
deployable physical-statistics definitions and carry the candidate-set and
singleton-profile provenance needed to distinguish development outputs from
full-universe results.
