# M2.14 screened recommendation/deployment/validation smoke

This is a development-only lifecycle smoke, not an authoritative M2.7
validation or a screening-quality experiment. It reuses the M2.13 screened
recommendation and performs no singleton profiling, payload acquisition, or
search. The full 468-query Census workload is used for every prefix.

The accepted-order prefixes were deployed through the formal raw-catalog
deployment path:

| K | accepted-order candidate IDs | mechanisms | A objective | A expected | A exact | C objective | A-C absolute | A-C relative |
|---:|---|---|---:|---:|---|---:|---:|---:|
| 1 | `cand_449fa599b32af3a5039b` | MCV | 2949.9782311993554 | 2949.9782311993554 | yes | 2930.404650469993 | 19.57358072936222 | -0.006635161074190132 |
| 5 | `cand_449fa599b32af3a5039b`, `cand_57ecad07b9ddfc92cbe9`, `cand_8800308d8e997ed3f8ad`, `cand_ec94d4fcaa39e54f2f3f`, `cand_c9321427349837fbbce7` | all MCV | 1378.928138020749 | 1378.928138020749 | yes | 1373.2229217802108 | 5.7052162405382205 | -0.004137428255490696 |
| 10 | `cand_449fa599b32af3a5039b`, `cand_57ecad07b9ddfc92cbe9`, `cand_8800308d8e997ed3f8ad`, `cand_ec94d4fcaa39e54f2f3f`, `cand_c9321427349837fbbce7`, `cand_50e40f909d08e40748f4`, `cand_6e828ab1e97b874ef25a`, `cand_4fa98ecd2ad6069efe03`, `cand_61bd8d15a2050d1f9514`, `cand_cef18d243e8dc73a4663` | all MCV | 1205.635813513518 | 1205.635813513518 | yes | 1212.0976596212663 | 6.461846107748215 | 0.0053596998656806755 |

The physical path created 1, 5, and 10 statistics respectively, ran one
fresh `ANALYZE` per prefix, evaluated the complete workload natively, and
dropped every smoke-created statistic before the next prefix. Analyze times
were 0.2768 s, 0.2823 s, and 0.2892 s; validation times were 0.0713 s,
0.0690 s, and 0.0715 s. Residual smoke statistics were zero after every
cleanup and at the end. The A/C differences are expected fresh-realization
drift, not a search or CE-semantics change. The B same-realization control was
not run because the existing machinery has no separate low-cost control path;
the protocol therefore uses the permitted A/C validation.

The final successful protocol path totals 16 CREATE, 3 ANALYZE, and 16 DROP
operations. Two earlier runner retries failed before report finalization (one
after K=1 and one after K=5); their exact 7 CREATE, 3 ANALYZE, and 7 DROP
operations were explicitly cleaned and are recorded as aborted retries, not
as additional smoke results. Including them, the physical operation audit is
23/6/23, with zero residual statistics.

The formal recommendation digest, source search digest, screened artifact
digest, singleton profile digest, raw catalog/workload/repository/model
digests, PostgreSQL patch digest, and build recipe digest are recorded in
`protocol.json` and repeated in each prefix validation artifact. The search
hot path performed no `ANALYZE`; fresh `ANALYZE` occurred only in this
validation phase. No move-level trajectory or per-query validation dump is
persisted; prefix artifacts contain compact per-query difference summaries.
