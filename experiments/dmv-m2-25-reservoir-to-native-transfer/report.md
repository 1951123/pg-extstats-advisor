# M2.25 — Reservoir-selected design transfer to native DMV samples

The M2.24 formal 30k-row deterministic reservoir sample was evaluated with the unchanged full-72 deterministic exact-bound-pruned ADD-only search. No new sample capture, production access, or search algorithm was used.

The reservoir design selected 16 candidates (10 MCV and 6 FD), at maintenance cost 78.613960753452712 and objective 39629.548816759467. The two fresh-backend searches were exact in accepted sequence, trajectory/design/objective/cost and termination; run times were 133.645s and 135.620s.

Fixed-design cross-evaluation on native M2.20 samples A–E was repeated exactly per sample. The advisor staging uses each persisted 30k native sample and its frozen `totalrows`; it does not copy the full DMV relation. Accordingly, the cross-evaluation baseline is a replay/staging baseline, while the M2.20 native-local objective remains the authoritative full-relation reference used for the requested gap and retention metrics.

Relative gaps A–E had min/median/mean/max 95.137064%/96.410278%/96.406012%/97.470025%. Benefit retention had min/median/mean/max 68.294022%/68.761134%/68.999548%/69.762966%. The reservoir recommendation did not beat any native-local reference. Every selected reservoir candidate was PRESENT in every native repository.

This is a product-facing transfer-fidelity result, not a claim of sampling equivalence. Sampling mechanism, totalrows/population metadata, ordinary-statistics realization, and one reservoir realization remain jointly confounded. The previously audited M2.24 nested `postgres_version` metadata normalization defect was not changed in this milestone.

## Exit answers

1. Yes. The current stock-production reservoir bundle selected a deterministic ADD-local design.
2. It transfers reproducibly to A–E, but with materially worse objective than each native-local reference; this is not “nearly equivalent” quality.
3. The maximum relative gap is `97.47002504%` (C); min/median/mean/max are `95.137064% / 96.410278% / 96.406012% / 97.470025%`.
4. Minimum benefit retention is `68.29402190%` (B); min/median/mean/max are `68.294022% / 68.761134% / 68.999548% / 69.762966%`.
5. The result supports a bounded recommendation-transfer observation, not sampling equivalence.
6. The next useful milestone is to isolate the `totalrows`/sampling-mechanism confounder before adding reservoir realizations or attempting reverse-direction evaluation.
