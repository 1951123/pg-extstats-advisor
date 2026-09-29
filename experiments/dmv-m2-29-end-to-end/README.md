# M2.29 — DMV fixed-T end-to-end advisor

This directory contains compact evidence for one fixed-T workflow.  The
production source was stock PostgreSQL 16.14 and the capture used one
read-only `REPEATABLE READ` snapshot.  The production simulator was stopped
after the bundle was sealed; no offline step reads the DMV source CSV or the
production relation.

The evaluated external target is exactly `T=100`.  It is not a decision
variable.  The frozen candidate universe contains 72 candidates (36 MCV and
36 FD), and the existing deterministic ADD-only search is run with the
historical budget `372.045872636249472` in the accepted empirical model's
units.  The workflow does not claim that the reservoir sample is
`ANALYZE`-equivalent or that the local search result is a global optimum.

`capture-summary.json` records the sealed bundle and shutdown proof;
`preparation-summary.json` records offline identities and the baseline;
`search-summary.json` records search counters and the recommendation digest;
`replay-summary.json` records the cold/warm exactness gate and the disposable
stock-PostgreSQL DDL smoke; `recommendation.json`, `deploy.sql`, and
`rollback.sql` are the handoff artifacts.
