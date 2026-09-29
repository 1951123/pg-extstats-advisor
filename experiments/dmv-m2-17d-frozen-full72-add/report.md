# M2.17d — DMV frozen-sample full-72 ADD-only search

This authoritative development search uses the persisted frozen-sample lineage, the complete 72-candidate catalog, the accepted M2.16 mechanism-count model, deterministic best-improvement ADD-only semantics, and the existing M2.8 exact lower-bound pruning. No candidate screening, DROP, SWAP, acquisition, repository write, calibration, or hot-path ANALYZE was performed.

The full-catalog-total budget is `372.045872636249472` milliseconds-per-analyze; the pre-search replay preflight built ordinary statistics once and then left 72 physical definitions with zero `pg_statistic_ext_data` rows. The search reached `add-local-optimum` after 32 rounds and 226.178s.

Objective decreased from 42791.986480127205 to 22014.061316846422 (absolute improvement 20777.925163280783; relative improvement 48.555645%). The selected design contains 31 candidates (8 MCV, 23 FD; 31 PRESENT and 0 ABSENT_NATIVE) at cost 175.422291756478746.

The independent repeat matched accepted sequence, mechanism and realization sequences, objective/cost trajectory, final design, counters, and termination exactly: `True`.

Efficiency: 1808 conceptual ADD moves, 1808 feasible, 0 budget-infeasible, 41 bound-pruned, 1767 native-evaluated, 878693 planner calls, and 2.267699% pruning. Round mean/median/p90/max was 7.056/7.071/8.547/8.843s.

Cleanup left 0 experiment statistics and 0 extended-statistics data rows; the replay sample relation was removed: `True`.
