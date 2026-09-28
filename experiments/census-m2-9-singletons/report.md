# M2.9 Candidate Singleton Utility Profiling

## Scope and semantics

This independent pre-study evaluates all 4,506 frozen Census candidates as singletons `{c}` against the empty design. It reuses the frozen native repository and query-local PostgreSQL 16.14 replay. No ANALYZE, statistics DDL, search resume, screening rule, or M3 work was performed.

The singleton improvement is `F(empty) - F({c})`, with strict floating comparison and no epsilon or clipping. Ranking ties use higher improvement, lower maintenance cost, lower precedence rank, then candidate ID. Percentile is top-oriented: rank one is 100%.

## Baseline and coverage

The empty-design objective is `5586.930692724469`, matching the M2.7 baseline. All 2,253 MCV and all 2,253 FD candidates were evaluated; positive/zero/negative counts are {'positive': 1571, 'zero': 1577, 'negative': 1358}.

## Distribution

MCV: {'positive': 1102, 'zero': 52, 'negative': 1099}; FD: {'positive': 469, 'zero': 1525, 'negative': 259}. PRESENT: {'positive': 1571, 'zero': 468, 'negative': 1358}; ABSENT_NATIVE: {'positive': 0, 'zero': 1109, 'negative': 0}.

Quantiles of singleton improvement (linear interpolation):

| group | p10 | p25 | p50 | p75 | p90 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| all | -0.230576 | -0.004562 | 0.000000 | 0.009520 | 0.396061 | 1.548201 | 9.249515 | 2636.952462 |
| mcv | -0.674140 | -0.082921 | 0.000000 | 0.105334 | 1.428393 | 3.137950 | 35.983222 | 2636.952462 |
| fd | -0.000159 | 0.000000 | 0.000000 | 0.000000 | 0.008301 | 0.063471 | 0.690525 | 7.364799 |
| PRESENT | -0.397181 | -0.032312 | 0.000000 | 0.041650 | 0.713937 | 2.009173 | 14.311915 | 2636.952462 |
| ABSENT_NATIVE | 0.000000 | 0.000000 | 0.000000 | 0.000000 | 0.000000 | 0.000000 | 0.000000 | 0.000000 |

## Cost-aware diagnostic and concentration

Positive-rate MCV/FD: 48.913% / 20.817%. Median positive improvement MCV/FD: 0.118997 / 0.006535; maximum MCV/FD: 2636.952462 / 7.364799.

The concentration values below are descriptive singleton-utility shares only; they are not cumulative achievable CE gains for a multi-candidate design.

| top positive candidates | utility share |
|---|---:|
| top_1pct | 84.718% |
| top_5pct | 94.437% |
| top_10pct | 96.685% |
| top_20pct | 98.711% |
| top_50pct | 99.908% |

Candidates needed for 50/80/90/95% of positive singleton utility: 4 / 12 / 27 / 94.

Top-100 and top-500 mechanism/state compositions are in `ranking-summary.json`; the same artifact contains query diversity and pair-redundancy diagnostics for raw and cost-aware rankings.

## ABSENT_NATIVE

There are 1109 ABSENT_NATIVE candidates: positive 0, zero 1109, negative 0. No ABSENT_NATIVE candidate was filtered or assigned a fabricated payload.

## Retrospective against M2.7 protocol-v2 partial run

The exact 36-candidate accepted sequence from the frozen checkpoint was mapped to singleton ranks. Singleton positive/zero/negative: {'positive': 36, 'zero': 0, 'negative': 0}. Raw ranking recall at top 50/100/200/500/1000: {'50': 14, '100': 28, '200': 35, '500': 35, '1000': 36}. Cost-aware recall: {'50': 14, '100': 29, '200': 35, '500': 35, '1000': 36}.

Contextual-surprise candidates (singleton improvement <= 0 but contextual strict improvement > 0): 0. Details are in `partial-run-retrospective.csv` and its `contextual_surprises` summary.

## Interpretation boundary

Singleton utility is evidence about usefulness in the empty-design context. A singleton-zero candidate is not thereby globally useless; contextual interactions remain possible. This pre-study chooses no screening rule and does not resume M2.7.

## Performance and limitations

The profile made 4506 native singleton evaluations and 20464 planner calls, with 19996 affected-query replans in 26.816 seconds. It is tied to the frozen workload, repository, source-built PostgreSQL 16.14, and current incidence contract; it does not establish a global utility decomposition or a production screening policy.
