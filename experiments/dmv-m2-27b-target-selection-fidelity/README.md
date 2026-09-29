# M2.27b: native-vs-reservoir target-selection fidelity

The canonical metric is aggregate q-error sum over the fixed 1,963-query positive-truth DMV workload. M2.26 native objectives already used this sum. M2.27a's historical `objective` field was a per-query mean; this milestone preserves that raw artifact and reports corrected aggregate values. Native and reservoir realization labels are mechanism-local and are not paired across mechanisms. `native_analyze_equivalent=false` remains mandatory: decision fidelity is not sampling equivalence.

The conservative five-condition gate is workload-specific and fails all five
conditions. Native quality ordering is `1000 < 300 < 100`, while reservoir
ordering is `1000 < 100 < 300`; native stability improves at 100→300 and
worsens at 300→1000, while reservoir stability improves at both transitions.
The resulting qualitative knees are 300 (native Pattern A) and 1000
(reservoir Pattern B). No production target recommendation is made.
