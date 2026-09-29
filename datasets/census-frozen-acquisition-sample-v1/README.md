# Census frozen acquisition sample v1

This is one native PostgreSQL 16.14 acquisition sample from `public.climate`.
All 4,506 extended-statistics payload states and ordinary statistics in the
M2.21 authoritative replay are derived from this persisted sample; no random
`ANALYZE` fallback is permitted.

- File: `sample.copy.bin`
- Rows: 30,000
- Sample SHA256:
  `84de07e3f60cb539bf75f7a8829501cdaee75d502063e61fa1850921268e20c6`
- Semantic SHA256:
  `1cb881fa32920c1edc9473117ceacffe5f10f946204d8eb726066fb7b14649a9`
- Manifest: `manifest.json`
