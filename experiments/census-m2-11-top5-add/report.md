# M2.11 Census Top-5% Singleton Screening, ADD-only Search

This is a development feasibility experiment, not an authoritative screening policy or M2.7 replacement.

The frozen M2.9 singleton ranking retained 226 of 4506 candidates (5%) using ceil rounding. The screened-universe budget is 414.0398034077412444 maintenance units.

The 226-candidate screen contains 214 MCV and 12 FD candidates, all in
`PRESENT` state. The ADD-only search terminated at an add-local-optimum after
111 rounds (110 accepted moves) and 350.515s. It selected 110 candidates (108
MCV and 2 FD; all `PRESENT`) with objective 936.358404182522 (baseline
5586.930692724469) and maintenance cost 195.4027612476062514.

The search considered 18,981 conceptual ADD moves, skipped zero for budget,
bound-pruned 2,338, natively evaluated 16,643, and issued 87,877 planner
calls. Overall bound-pruning rate was 12.32%; round elapsed time averaged
3.148s (median 3.561s, maximum 4.289s). The final design's singleton ranks
range from 1 through 226 (median 118.5); its lowest singleton improvement was
1.549639893785752. Query q-error summary is mean 2.000765820902824, median
1.4225031768819285, p90 3.7315540387171544, and maximum 14.766355140186915;
195 queries improved, 235 were unchanged, and 38 worsened.

The exact repeat matched the accepted sequence, final design, objective, cost, round count, and termination reason: True.

The M2.10 rank-768 boundary candidate cand_ea654d977d77db152ccf was excluded:
True. The screened trajectory continued to improve without it. At rounds 1,
5, 10, 20, and 36 the screened objectives were 2949.978231,
1378.928139, 1205.635814, 1136.970112, and 1079.662804 respectively. The
accepted sequence first diverged from the raw M2.7-v2 partial trajectory at
round 27; the round-36 accepted-design Jaccard overlap was 0.945945946.
Raw-v2 comparisons are descriptive only because the candidate universes differ.

No DROP, SWAP, ANALYZE, statistics DDL, M2.7 resume, physical validation, or M3 work was performed.
