# M2.36 — Bounded DMV Physical-Per-Design Materialization Pilot

This diagnostic pilot measures repeated physical materialization work for six deterministic DMV designs. It does not claim universal speedup, advisor superiority, production acceleration, or Census-scale physical replay.

The suite contains 6 designs: empty, accepted trajectory prefixes 1/5/16/30, and the final 31-object recommendation. Each path uses one untimed warmup and 3 measured repetitions.

Median physical materialization component is 0.116797s (range 0.065887–0.218108s); median hypothetical activation component is 0.000421s (range 0.000296–0.000542s).
Median descriptive total ratio is 1.414× (range 1.326–1.668); one-time hypothetical setup is 0.037350s and the transparent median-delta break-even estimate is 0.32093972627401274 design evaluations.

All physical rows had payload matching: 18/18. Objective values are retained for diagnostic comparison only; M2.18/M2.35 remain the authoritative fidelity evidence.

Practical signal: **MODERATE**, bounded to this DMV environment and timing protocol.
