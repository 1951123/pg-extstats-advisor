# M2.33 Docker clean-room evidence

This directory records the local Docker clean-room reproduction for the
fixed-target MVP. The experiment uses only the tracked synthetic fixture in
`examples/docker-cleanroom/`; it is not a DMV run and it does not change CE
semantics or the deterministic search algorithm.

Run `scripts/docker_cleanroom_m233.sh` with Docker available and the verified
PostgreSQL 16.14 tarball. The runner builds separate capture, patched-advisor,
stock-production, and stock-validation roles, then records the lifecycle in
`lifecycle-summary.json` and the source/build identities in
`build-provenance.json`.

The only reproducibility claim is semantic equality of the cold and warm
advisor outputs (selected design, objective, and recommendation digest). No
byte-level image, wheel, or PostgreSQL-binary reproducibility claim is made.
Runtime logs and caches are disposable and ignored by Git.
