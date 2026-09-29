# M2.20 — DMV persisted multi-sample design-selection stability

Five persisted acquisition worlds (A–E) share the canonical DMV relation, workload/truth, candidate universe, maintenance model, and PostgreSQL build. A is the existing authoritative frozen sample; B–E were independently captured once and then used only through persisted replay.

Each new sample passed two clean replay gates for ordinary statistics, repository payloads, baseline vector, and baseline objective. Each of B–E then ran the complete 72-candidate deterministic ADD-only search from its own baseline; B was repeated exactly. The 5×5 matrix used hypothetical replay with each evaluation sample's own ordinary statistics and payload repository.

Design Jaccard ranged from 0.7429 to 0.9375. Cross-sample evaluation and local-design gaps are in the CSV artifacts; the combined interpretation is **multiple near-equivalent designs**.

No sample screening, DROP/SWAP search, maintenance refit, data drift, workload drift, or PostgreSQL patch change was performed.
