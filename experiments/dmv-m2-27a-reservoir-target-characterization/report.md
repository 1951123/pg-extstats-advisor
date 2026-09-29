# M2.27a DMV reservoir target characterization

Bundle v2 captures targets 100, 300, and 1000 with independent deterministic A/B/C reservoirs from one exported repeatable-read snapshot. The sweep reconstructs ordinary statistics only; no extstats objects or search are involved. `native_analyze_equivalent=false` and no production target recommendation is made. M2.27b native-vs-reservoir fidelity remains deferred.

Metric note: the historical `target-sweep.json` field named `objective` is the per-query mean q-error (`aggregate / 1963`), retained unchanged as a raw artifact. It is not the project's authoritative aggregate objective; M2.27b normalizes it and records the corrected aggregate comparison.
