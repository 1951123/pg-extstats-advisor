# M2.25 — Reservoir-to-native design transfer

This milestone asks a product-facing question: does a design selected from
the M2.24 stock-compatible deterministic reservoir capture transfer to the
five persisted native PostgreSQL sample realizations A–E?

M2.20 already established native-family realization stability through
sample-local searches and a full cross matrix. M2.25 changes the capture
lineage: the reservoir design is selected from the sealed M2.24 Production
Capture Bundle v1 sample, using the unchanged full-72 exact-bound-pruned
ADD-only search and the accepted maintenance model.

The reservoir search selected 16 candidates (10 MCV and 6 FD) at objective
`39629.54881675947` and modeled maintenance cost
`78.613960753452712`. Two fresh-backend runs matched in accepted sequence,
trajectory, final design/objective/cost, counters, and termination.

The fixed reservoir design was then evaluated twice on each persisted native
sample A–E. Candidate identities were resolved against each sample's own
repository; payload bytes were never transferred. Every selected reservoir
candidate was `PRESENT` in all five native repositories. Relative gaps to the
M2.20 native-local objectives ranged from 95.137064% to 97.470025%, and
benefit retention ranged from 68.294022% to 69.762966%.

The cross-evaluation staging uses each persisted 30k-row native sample and its
frozen `totalrows`; it does not copy the full DMV relation into the advisor.
Therefore the staging baseline is reported separately from the authoritative
M2.20 native-local full-relation reference. Sampling mechanism, population/
`totalrows` semantics, ordinary-statistics realization, and one reservoir
realization remain jointly confounded. The result supports a bounded
product-facing transfer-fidelity observation, not sampling equivalence,
30k-row sufficiency, or a universal production guarantee.

Artifacts are in
`experiments/dmv-m2-25-reservoir-to-native-transfer/`; the formal M2.24
bundle and its contract are unchanged.
