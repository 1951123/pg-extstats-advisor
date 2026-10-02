# PostgreSQL source discipline

The authoritative upstream source is the clean Git checkout
`~/projects/postgresql-src` at `REL_16_14`, commit
`0d1c00c624fa7367d4a895f44381887757289682`. It is read-only input: never patch
or build in that tree.

## Three separate trees

1. `~/projects/postgresql-src` is the clean upstream source.
2. `~/projects/postgresql-build-pgextadv-cleanroom-<run>-src` is a disposable
   source archive produced by the clean-room script and patched in place.
3. `~/projects/postgresql-build-pgextadv-cleanroom-<run>` and
   `~/projects/postgresql-install-pgextadv-cleanroom-<run>` are generated build
   and install prefixes.

A binary is attributable to this project only when the clean build script
verified the upstream SHA, extracted a fresh source tree, applied both tracked
patches, ran the build and regression suite, and printed the resulting binary
SHA256.

## Tracked patch stack

Patches are applied with GNU `patch -p1 --batch --forward` in this order:

1. `patches/postgresql-16.14-hypothetical-extstats.patch` (SHA256
   `22c7f48632585e81fd8a557dc8bffba873ac5da070aca31713e22c60261c3b4f`).
2. `patches/postgresql-16.14-analyze-sample-cache.patch` (SHA256
   `0d8c3fb24d59c52e2548875b04403d81c3fc1dfe22c1cfd12c935fb7f1691bc5`).

The second patch is intentionally generated against the tree produced by the
first, so its context and application order are part of provenance. The sample
cache source fix is recorded in PostgreSQL source commit `960bfebc9c4`.

## Clean-room workflow

From the advisor repository:

```bash
scripts/build_postgres16_cleanroom.sh \
  "$HOME/projects/postgresql-src" \
  "$HOME/projects/postgresql-build-pgextadv-cleanroom-<run>" \
  "$HOME/projects/postgresql-install-pgextadv-cleanroom-<run>"
```

The script refuses a dirty or wrong upstream checkout and refuses to replace
existing output paths. It configures with debug symbols, assertions, and
OpenSSL, then runs `make -j$(nproc)`, `make check`, and `make install` without
sudo. It performs no recursive cleanup.
