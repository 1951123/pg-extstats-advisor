# Production capture architecture

The system now has an explicit two-backend boundary:

```text
Pristine stock PostgreSQL production simulator
        |  read-only capture
        v
Prototype production capture (sealed)
        |  production disconnected
        v
Patched PostgreSQL advisor
        v
derived ordinary statistics / extstats repository / recommendation
```

Production owns the full base tables and exact truth authority.  A capture
contains metadata, workload, truth, and a persisted acquisition sample.  The
advisor consumes the sealed artifact and owns derived statistics, payload
repository, and search state.  After sealing, the normal advisor path does not
require the production database, production data directory, or the patched
server on the production side.

This is an architecture overview for the current Bundle v1 workflow. The
persisted frozen sample is authoritative for one advisor statistics
realization; a future production `ANALYZE` may produce a different native
realization. That accepted realization uncertainty is distinct from same-
realization replay correctness.
