# DMV frozen acquisition sample v1

This artifact is one native PostgreSQL 16.14 ANALYZE sample captured from `public.dmv`. The binary COPY stream preserves sample row values and order; the manifest checksums both serialization and canonical sample semantics. Replay uses the backend-local frozen-sample GUC path and never materializes this artifact as a production relation.

The sample is authoritative only for M2.17b acquisition/replay validation. It does not alter the frozen candidate catalog, incidence, workload, or M2.16 maintenance model.
