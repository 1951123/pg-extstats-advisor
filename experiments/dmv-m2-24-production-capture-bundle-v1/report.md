# M2.24 — Production Capture Bundle v1

M2.23's ignored DMV prototype was normalized into a formal,
multi-relation-ready `production-capture-bundle-v1` directory. The bundle
contains only environment/schema metadata, workload, exact truth, and one
persisted acquisition sample; it contains no full DMV table and no extstats
payload bytes. Derived advisor payloads and the reconstruction report are
written beside, rather than inside, this sealed input directory. Its root digest is
`f503bf80d9f10d8c364a8ef4197117ed5214bfca8ab815afb54b9a67259c5e97`.

Repeated normalization produced identical component and root digests. Runtime
timestamps and local paths are excluded from semantic identity. The verifier
checks required files, relation/query identity, exact truth membership, sample
schema and population metadata, method/serialization capability declarations,
binary and semantic digests, snapshot mode, sensitivity declaration, and root
integrity. All tested tampering and malformed compatibility cases fail closed.

The DMV acquisition method is `deterministic_reservoir_v1` with the explicit
capabilities production-compatible, stock-PostgreSQL-only, and deterministic
replay. It is authoritative for this bundle's statistics realization but is
not claimed native-ANALYZE-equivalent. `source_population_rows` and
`statistics_population_rows` are both `11591877`; `sample_row_count` is
`30000`.

With production still stopped, the formal bundle-only advisor path passed the
exact 16.14 compatibility gate and reconstructed the state without reading the
M2.23 prototype, canonical CSV, or production data directory. It derived 70
PRESENT and 2 ABSENT_NATIVE realizations, ordinary-statistics digest
`b8330158…7604`, semantic repository digest `eb786799…eff3f`, baseline
`88053.61940613187`, and estimate-vector digest `6be99060…1274`. All five
comparison fields—ordinary statistics, semantic repository, realization-state
counts, baseline objective, and estimate vector—are recorded as exact matches
to the M2.23 semantic reconstruction result. Runtime repository manifests may
differ in OIDs, relfilenodes, and timestamps, so those non-portable fields are
not part of the semantic repository identity.

## Explicit non-goals and unresolved items

No public capture CLI, search, CE change, patch change, join support, sampling-
fidelity claim, sample-size claim, cross-version support, encryption, or live
production workload capture was implemented. Truth acquisition cost at scale,
strong-snapshot ergonomics, multi-relation execution, and stock sample fidelity
remain open research/engineering questions.
