# M2.11 Census top-5% singleton screening and ADD-only feasibility

M2.11 is a development feasibility experiment, not an authoritative M2.7
search, a production screening policy, or a claim that five percent is an
optimal threshold. It reuses the complete M2.9 singleton artifact and frozen
M2.7 native payload repository, retaining the deterministic raw singleton top
`ceil(4506 * 0.05) = 226` candidates. Ranking is unchanged from M2.9:
descending singleton improvement, ascending maintenance cost, precedence rank,
then candidate ID. The retained set is benchmark-specific to Census; other
benchmarks must profile singleton utility and inspect interaction evidence
before choosing their own development profile.

The reusable replacement for the milestone-specific runner is the formal
workflow in [`development-screening-workflow.md`](development-screening-workflow.md)
and [`m2-13-formal-development-workflow.md`](m2-13-formal-development-workflow.md):
`singleton-profile`, `screen-candidates`, screened `search --search-mode
add-only`, and `recommend`. The M2.13 Census smoke reproduces this experiment's
membership, order, and result through those generic interfaces. The historical
M2.11 script and artifacts remain retained for provenance, but are not the
recommended operational path.

The visible catalog is the screened set, while workload, incidence, payload
repository, CE semantics, and empirical maintenance model remain frozen. The
search uses the existing exact q-error lower-bound pruning and deterministic
best-improvement ADD neighborhood only. DROP and SWAP are not examined, and
the budget is deliberately generous: 100% of the screened-universe total
maintenance cost. No acquisition, ANALYZE, statistics DDL, M2.7 resume,
physical validation, or M3 work is part of this milestone.

## Result

The frozen artifact is [`experiments/census-m2-11-top5-add/`](../experiments/census-m2-11-top5-add/).
The 226-candidate screen contains 214 MCV and 12 FD candidates; all 226 are
`PRESENT` in the frozen repository. Its total maintenance cost and search
budget are `414.0398034077412444` milliseconds-per-analyze model units.

The ADD-only run reached `add-local-optimum` after 111 rounds (110 accepted
moves) in 350.515 seconds. It selected 110 candidates (108 MCV and 2 FD), at
cost `195.4027612476062514`, and reduced the fixed-workload objective from
`5586.930692724469` to `936.3584041825216`. The exact repeat matched the
accepted sequence, final design, objective, cost, round count, and termination
reason; elapsed time was 355.147 seconds. This is a feasibility result for a
development iteration profile, not a quality comparison against the full
4,506-candidate search.

The rank-768 M2.10 boundary candidate
`cand_ea654d977d77db152ccf` (singleton improvement `0.06053194589821942`) was
excluded by the frozen top-5% screen. The screened trajectory continued to
improve without it. Comparisons with the M2.7-v2 partial trajectory are
descriptive only because the candidate universes differ; the accepted
sequence first diverges at round 27.

## Development interpretation and limitation

The profile is usable for current Census development iteration because it
finishes deterministically within the ten-minute ceiling. It does not justify
the top-5% threshold as optimal, does not establish screening recall, and does
not transfer the fraction to another benchmark. The main remaining limitation
is that ADD-only screening has not been validated against a complete
full-universe optimum or a prospective workload-specific threshold study.

The full protocol and machine-readable metrics are in
[`protocol.json`](../experiments/census-m2-11-top5-add/protocol.json),
[`screening-summary.json`](../experiments/census-m2-11-top5-add/screening-summary.json),
[`final-result.json`](../experiments/census-m2-11-top5-add/final-result.json),
and [`report-summary.json`](../experiments/census-m2-11-top5-add/report-summary.json).
