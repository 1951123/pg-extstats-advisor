# M2.17b — Frozen acquisition sample

M2.17b makes one native PostgreSQL 16.14 `ANALYZE` acquisition sample a
first-class, checksummed input to DMV validation.  The captured sample is
stored under `datasets/dmv-frozen-acquisition-sample-v1/` as a binary COPY
stream with a manifest containing a semantic digest, serialization digest,
ordered schema, row count, target, and source-build provenance.

The patched backend has three opt-in settings:

```text
pg_extstats.frozen_sample_mode = off | capture | replay
pg_extstats.frozen_sample_relation = schema.relation
pg_extstats.frozen_totalrows = captured relation row estimate
```

`capture` runs PostgreSQL's ordinary block/reservoir sampler once and stores
the sorted sample tuples.  `replay` reads exactly those tuples from an
auxiliary relation after validating relation shape and uses the captured
`totalrows`; the target relation is not sampled.  Ordinary column statistics,
all 72 MCV/FD definitions, the serialized native payloads, and the baseline
estimate vector therefore derive from the same sample.

The M2.17b run performs three builds (capture, replay, and replay from a fresh
backend session).  Their ordinary-statistics digests, native payload states
and payload bytes, and baseline estimate vectors are compared exactly.  It
also records negative controls for normal `ANALYZE`, missing/schema/type/
empty samples, and a corrupted artifact digest.  This milestone does not run
DMV search or screening and does not alter the frozen candidate catalog,
incidence, workload truth, or the accepted M2.16 maintenance model.

The old historical DMV ordinary-statistics state is not restored.  The frozen
sample lineage is the authoritative M2.17b acquisition/replay prerequisite;
it is not itself a maintenance-cost or workload-generalization result.
