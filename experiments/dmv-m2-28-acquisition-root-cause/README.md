# M2.28 acquisition-fidelity root-cause study

This package replays the existing M2.26 native and M2.27a reservoir samples in the isolated advisor cluster and compares ordinary PostgreSQL statistics. It does not create extstats, run search, capture production, or implement a sampler. `native_analyze_equivalent = false` and `target_selection_fidelity_qualified = false` are intentional.
