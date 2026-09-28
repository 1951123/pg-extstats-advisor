# M2.12 Census top-5% one-local-round probe

M2.12 is a development probe starting from the persisted M2.11 Census
top-5% ADD-local optimum. It evaluates exactly one complete local
ADD/DROP/SWAP best-improvement neighborhood over the same 226-candidate
screened catalog. It does not run a second local round, resume M2.7, change
the screening fraction, perform acquisition or physical validation, or start
M3.

The starting state has 110 selected and 116 unselected candidates, objective
`936.3584041825216`, and maintenance cost `195.4027612476062514`. The single
neighborhood contains 116 ADD moves, 110 DROP moves, and 12,760 ordered SWAP
moves, for 12,986 conceptual moves.

## Probe result

The first round completed in `533.245306284 s`, below the predeclared 600 s
ceiling. It evaluated all 116 ADDs, 93 of 110 DROPs, and 12,562 of 12,760
SWAPs. Exact-bound pruning removed 17 DROPs and 198 SWAPs; no moves were
budget-infeasible. The aggregate was 12,986 conceptual moves, 215 bound-pruned
moves, 12,771 native evaluations, and 127,696 planner calls, for a 1.655629%
pruning rate.

The best move was a SWAP at unchanged maintenance cost:

```text
DROP cand_0f6d24f9d2bd31d7fb7b  (MCV, PRESENT, singleton rank 53)
ADD  cand_25ad575ae2a9b8b250c6  (MCV, PRESENT, singleton rank 224)
```

It reduced the objective from `936.3584041825216` to
`932.026559804811`, an absolute improvement of `4.331844377710581` and a
relative improvement of `0.4626267%`. No strict-improving ADD existed, so the
M2.11 ADD-local-optimum gate held. The exact repeat started from the same
persisted state and matched move counts, pruning/native counts, best move,
objective, and cost; repeat elapsed time was `516.316555702 s`.

For scale, the local-round gain is larger than the M2.11 last ADD gain
(`0.003227273313200385`) and the median of the last ten ADD gains
(`0.012695100281007399`), but tiny relative to the total ADD-only improvement
(`4650.572288541947`). These comparisons are descriptive only.

## Development interpretation

A complete local round is reproducible and fits the ten-minute development
ceiling, but it is not cheap: nearly nine minutes are spent in the SWAP
neighborhood. It can yield a measurable additional CE improvement, and this
probe found a contextual replacement. The current Census development default
should therefore remain ADD-only for routine iteration; a complete local
refinement remains worth exploring only as an occasional, explicitly bounded
diagnostic rather than an automatic policy change.

The machine-readable protocol and results are in
[`experiments/census-m2-12-one-local-round/`](../experiments/census-m2-12-one-local-round/).
