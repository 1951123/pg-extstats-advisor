# M2.10 Singleton Screening Interaction-Rescue Analysis

## Scope

This pre-study uses the 4,506 M2.9 singleton results and a strict replay of the 36 completed M2.7-v2 greedy rounds. It captures only actual native-evaluated ADD moves. Bound-pruned moves are excluded because they have no observed native objective. The replay stopped before round 37; no search, screening threshold, ANALYZE, DDL, or M3 work was performed.

For each observed move, contextual improvement is `F(Y_t) - F(Y_t ∪ {c})`, and interaction gain is contextual improvement minus the frozen M2.9 singleton improvement. All conclusions are observational and trajectory-dependent.

## Coverage and overall contextual distribution

The replay analyzed 86098 native evaluations over 3759 unique candidates; 747 candidates were never natively observed. Positive/zero/negative contextual evaluations: 28656 / 28786 / 28656 (positive rate 33.2830%).

Maximum contextual improvement: 2636.952462; maximum interaction gain: 510.578922.

## Singleton percentile behavior

See `singleton-percentile-context-summary.csv` for unique-candidate and evaluation denominators, contextual rates, quantiles, and accepted counts by raw singleton rank bucket.

## Rescue regions

Accepted contextual-improvement reference levels are median=5.544890, p75=9.265633, p90=146.194479.

For singleton-nonpositive candidates: observed 2412, candidates with any positive contextual rescue 201, positive contextual evaluations 3372, accepted moves 0, and maximum contextual improvement 3.705273137956283.

ABSENT_NATIVE: observed 905, positive contextual evaluations 0, accepted 0, maximum contextual improvement 0.0. This is evidence for this trajectory only, not a pruning theorem.
Among PRESENT singleton-zero candidates, observed 354 of 468; candidates with positive contextual rescue 6, accepted 0. Among singleton-negative candidates, observed 1153 of 1358; candidates with positive contextual rescue 195, accepted 0.

## Screening recall (retrospective only)

`screening-recall.csv` reports raw and cost-aware singleton-ranking retention at the requested fractions. Accepted-gain recall is the fraction of the 36 accepted contextual improvement sum retained; it is not a rerun objective guarantee.

The accepted candidate outside raw top-500 but inside top-1000: [{'accepted_round': 27, 'candidate_id': 'cand_ea654d977d77db152ccf', 'mechanism': 'mcv', 'realization_state': 'PRESENT', 'singleton_improvement': 0.06053194589821942, 'singleton_rank': 768, 'singleton_percentile': 82.97825122059476, 'contextual_improvement': 3.79975150442192, 'interaction_gain': 3.7392195585237005, 'selected_count_before': 26}].

## Contextual surprise and limitations

Surprise rows are stored in `surprise-candidates.csv` (60 rows across the requested regions). Candidates never observed were bound-pruned or otherwise absent from native evaluation; never-observed-positive is not evidence of contextual uselessness.

Accepted-rank trend: {'accepted_ranks': [1, 4, 6, 11, 12, 13, 28, 41, 45, 46, 49, 47, 53, 59, 61, 65, 50, 68, 76, 74, 83, 21, 63, 100, 94, 99, 768, 88, 97, 101, 105, 109, 116, 118, 127, 133], 'spearman_round_vs_rank': 0.9438867438867439, 'first_half_median_rank': 45.5, 'second_half_median_rank': 99.5}. Mechanism-specific rescue, correlation, round-segment, and cost-aware recall details are in `summary.json` and the CSV artifacts.

No screening rule is selected. The evidence is sufficient to quantify the observed trajectory but not to claim that singleton utility is globally contextual or to finalize a production cutoff.
