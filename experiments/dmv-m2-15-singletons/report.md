# M2.15 DMV onboarding and singleton profiling

The authoritative raw source contains 1965 queries. Explicit preprocessing uses `truth > 0`, excluding `dmv.173` and `dmv.943`, leaving 1963 effective queries. Current advisor q-error semantics are unchanged.

Raw source SHA256: `1953dc96e00c8caeeb9d363070853d27a6548ce43ab14772d8e7637af9c2cd22`.
The raw workload digest is `6b02d70dc063e1efd577c319de551b7d9f620092142e53486d30dc4eb04af504`; the effective workload digest is `e790933cfc4f0f42d92807170b76cec080621c5b463dab83e23474a0a51151d8`. Parser provenance is pglast `v7.18`, analysis `pg16-mvp-v2`: 1963 precise, 0 fallback, 0 rejected, and 0 errors.

Preparation produced 36 unordered pairs, 36 MCV candidates, 36 FD candidates, and 72 total candidates. The frozen acquisition has 71 PRESENT and 1 ABSENT_NATIVE payloads; no candidate was screened or dropped.

The empty-design objective is 41384.005431775244; singleton classification is {'positive': 37, 'zero': 1, 'negative': 34}, with positive rate 51.389%. Positive utility concentration is descriptive only and is not an additive achievable design-gain claim.

The positive singleton utility mass is 22304.836543655583. The top 1%, 2%, 5%, 10%, 20%, and 50% of positive candidates account for 88.991%, 88.991%, 96.988%, 98.842%, 99.645%, and 99.976% of that descriptive mass, respectively. The 50%, 80%, 90%, 95%, and 99% mass cutoffs require 1, 1, 2, 2, and 5 candidates, respectively. MCV is positive for 20/36 candidates; FD is positive for 17/36; the one ABSENT_NATIVE FD is zero.

The candidate catalog digest is `99de997c7c2b46592523c939f1d03b962aaab8cb76c1204532f351027d0f2b73`, the effective incidence digest is `ff320b0c6c54397ce361f4f83a625f9df34c05e5fa0423282d4169dbe2090d71`, and the frozen repository digest is `17d95816ff1fce59c96b92569a41864b1b4a4e35e5e440ff863ad93493605914`. The source-built PostgreSQL version is 16.14; acquisition ran one relation-grouped ANALYZE and used no Census maintenance model.

Native query-local/full-reference smoke checks were exact for all recorded spots: `True`. Acquisition statistics were removed after validation. No budget search, screening fraction, or M3 work was run. DMV onboarding and singleton profiling are complete; a DMV-specific development screening decision remains pending.
