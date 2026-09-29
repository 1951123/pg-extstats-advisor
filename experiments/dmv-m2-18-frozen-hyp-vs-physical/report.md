# M2.18 — Frozen-sample hypothetical vs physical exact validation

The final 31-stat DMV design was loaded directly from the authoritative M2.17d artifact. Both states use the same persisted acquisition sample, ordinary-statistics realization, PostgreSQL 16.14 build, workload, and truth vector. The only semantic difference is hypothetical payload replay versus ordinary physical catalog payload consumption.

H and P both have objective `22014.061316846422` and estimate-vector digest `24cc3f4f9cc2e4987d877e4bb7874f458cf84a9aa781733adb5d3a29189551a4`. All 1963 query estimates and q-errors are exact; all 31 selected physical payloads match their frozen repository payload bytes in both runs.

Relative to the same empty baseline, H counts are {'improved': 1562, 'unchanged': 84, 'worsened': 317} and P counts are {'improved': 1562, 'unchanged': 84, 'worsened': 317}; these counts are exact between modes and runs. Relation metadata and ordinary-statistics digests are exact between H and P and match the M2.17b digest.

State H used 72 shell definitions, zero physical data rows, and 31 active hypothetical candidates. State P used 31 physical definitions and 31 data rows in a fresh no-overlay backend.

This is conditional same-sample mechanism validation only. It does not test fresh-sample robustness: no random or native heap sampling was run.

Cleanup restored the empty baseline exactly and left no experiment statistics/data rows. DDL artifacts contain 31 CREATE STATISTICS and 31 DROP STATISTICS statements.
