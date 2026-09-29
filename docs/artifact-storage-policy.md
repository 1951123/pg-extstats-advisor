# Derived artifact storage policy

Persisted acquisition samples are the authoritative inputs for frozen native
payload lineages.  Reconstructed payload repositories are derived artifacts and
must live under `.build/artifact-cache/`; they are validated by a cache manifest
whose identity binds the sample semantic and binary digests, relation schema,
candidate catalog, statistics target, PostgreSQL upstream/build/patch identity,
and acquisition schema version.  Cache entries are disposable and are never a
fallback to tracked experiment payload directories.

Tracked experiment packages retain compact repository summaries and semantic
evidence (candidate/state counts, payload-digest-vector digests, and build
provenance), not reconstructible payload blobs or volatile backend manifests.
The six frozen sample binaries remain tracked because they are the reproducible
source artifacts from which those repositories are rebuilt.  A cache miss must
reconstruct from the persisted sample, validate the expected semantic digest,
and install atomically; corrupt, partial, incompatible, or wrong-identity
entries are rejected and rebuilt safely.

The policy does not claim PostgreSQL maintenance-cost universality or eliminate
historical evidence.  Large historical trajectories and contextual evaluation
artifacts remain preserved unless they are explicitly outside the artifact
lineage and independently reproducible.
