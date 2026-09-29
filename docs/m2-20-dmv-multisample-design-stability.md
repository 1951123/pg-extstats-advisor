# M2.20 — DMV persisted multi-sample design-selection stability

## Scope and protocol

This milestone measures design-selection stability under sampling noise. It
does not change CE-Replay semantics, the candidate catalog, the M2.16
maintenance model, or the deterministic best-improvement ADD-only search.
Sample A is the existing authoritative M2.17b persisted acquisition. Samples
B–E are four independent persisted native acquisitions from the same canonical
`public.dmv` relation, using the same source-built PostgreSQL 16.14, target
100, schema, workload/truth, and 72-candidate (36 MCV + 36 FD) catalog. Each
sample has 30,000 rows from the 11,591,877-row relation.

Every new sample was replayed twice from its persisted binary sample. Ordinary
statistics, the semantic repository payload state, the baseline estimate
vector, and the baseline objective matched exactly across the two replays. The
semantic repository digest intentionally excludes volatile acquisition
timestamps/OIDs while retaining candidate identity, mechanism, realization
state, and payload bytes. B was also searched twice; the complete search
trajectory and result matched exactly. No screening, DROP/SWAP moves, target
refit, data refresh, or workload change was performed.

## Per-sample searches

The full 72-candidate deterministic ADD-only search reached an add-local
optimum for every sample:

| Sample | Final objective | Selected | MCV | FD |
|---|---:|---:|---:|---:|
| A | 22014.061317 | 31 | 8 | 23 |
| B | 21897.221095 | 31 | 7 | 24 |
| C | 21937.965918 | 31 | 8 | 23 |
| D | 22172.194940 | 28 | 8 | 20 |
| E | 21815.492589 | 30 | 6 | 24 |

The selected identity is not perfectly invariant: pairwise design Jaccard is
0.742857–0.937500 (median 0.846117). Nevertheless, 25 candidates form a
five-of-five consensus core and 28 are selected in at least four samples. The
selection-frequency histogram is 37 candidates selected zero times, 2 once,
3 twice, 2 three times, 3 four times, and 25 five times.

Singleton rankings are substantially more stable than exact final identity:
pairwise Spearman correlation is 0.876069–0.946556 (median 0.924416), top-5
overlap is always 5, top-10 overlap is 8–10, and top-20 overlap is 18–19.
Fourteen candidates change singleton improvement sign across samples. The
accepted trajectories contain contextual rescues of singleton-negative
candidates (10–14 per sample), confirming that final selection is not a
singleton-only decision.

## Cross-sample portability

The 5×5 matrix evaluates every persisted design on every sample's own
ordinary statistics and semantic payload repository. Rows are design source
samples A–E; columns are evaluation samples A–E:

| Source \\ Eval | A | B | C | D | E |
|---|---:|---:|---:|---:|---:|
| A | 22014.061317 | 22014.898573 | 22014.061481 | 22014.072904 | 22015.912308 |
| B | 21897.339238 | 21897.221095 | 21897.338982 | 21897.349917 | 21898.780625 |
| C | 21937.966598 | 21938.533317 | 21937.965918 | 21937.978391 | 21939.919328 |
| D | 22172.195774 | 22172.724235 | 22172.195973 | 22172.194940 | 22173.770838 |
| E | 21816.907080 | 21816.595915 | 21816.907172 | 21816.908527 | 21815.492589 |

All 25 cells are present and each diagonal equals its local search objective
exactly. On every evaluation sample, no foreign design beats the local design.
The maximum foreign-versus-local relative gap is below `8.905e-05` (0.009%).
Thus the final identity varies at boundary candidates, while workload quality
is highly portable. The appropriate characterization is **multiple
near-equivalent designs**, not materially sample-sensitive quality.

## Native realization stability and lineage

Across all 72 candidates, 68 are always `PRESENT`, one is always
`ABSENT_NATIVE`, and three switch state across samples. These states are
reported rather than filtered. All selected objects in each search are
evaluated through the persisted sample-specific repository; no unpersisted
native result is promoted as authoritative.

The machine-readable artifacts in
`experiments/dmv-m2-20-multisample-design-stability/` include the protocol,
sample manifests and compact lineage, singleton profiles, search lineages,
pairwise overlap and frequency tables, rank stability, interaction evidence,
the complete cross matrix and objective gaps, portability summaries, and the
final report. The four new persisted samples are under
`datasets/dmv-frozen-acquisition-sample-v2-{b,c,d,e}/`.

## Conclusion

Independent persisted samples do change the exact boundary composition and
occasionally the selected object count, but they do not materially change
workload quality. Replay and search determinism are exact, and cross-sample
portability remains within 0.009% relative objective gap. This supports
reporting DMV design selection as a stable quality regime with multiple
near-equivalent physical designs, rather than claiming a unique sample-
independent candidate set.
