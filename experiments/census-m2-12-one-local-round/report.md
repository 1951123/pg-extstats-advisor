# M2.12 Census top-5% one-local-round probe

This development probe starts from the persisted M2.11 ADD-local optimum and evaluates exactly one complete local ADD/DROP/SWAP neighborhood. It does not run a second round, resume M2.7, perform physical validation, or change the screening universe.

The starting design has 110 selected and 116 unselected candidates, objective 936.358404182522, and cost 195.4027612476062514. The conceptual neighborhood is 12986 moves (expected 116 ADD + 110 DROP + 110*116 SWAP = 12986).

The first round took 533.245s, evaluated 12771 moves, bound-pruned 215 (1.655629%), and issued 127696 planner calls. It found swap strict-improving best move.

The exact repeat matched move counts, pruning/native counts, best move, objective, and cost: True. Repeat elapsed time was 516.317s.

No improving ADD existed: True. Relative to M2.11's last ADD gain (0.003227273313) and median last-10 gain (0.012695100281), the local refinement gain was 4.331844377711.

This is descriptive development evidence; it does not change the Census default profile or establish a policy.
