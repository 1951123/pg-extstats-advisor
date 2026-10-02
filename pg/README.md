# PostgreSQL source discipline

The authoritative modified PostgreSQL source is the separate Git repository
`git@github.com:1951123/postgresql-pgextadv.git`. It retains official
PostgreSQL ancestry and is consumed at a frozen commit by this advisor.

- upstream repository: `https://github.com/postgres/postgres.git`
- upstream tag: `REL_16_14`
- upstream base commit: `0d1c00c624fa7367d4a895f44381887757289682`
- authoritative repository: `https://github.com/1951123/postgresql-pgextadv`
- current authoritative commit: `6d7f5c9cd6cf1b0f73e84a4bacc45a31d1cb0cd6`

## Preferred build

Build directly from a clean checkout of the authoritative commit with
`scripts/build_postgres16_authoritative.sh`:

```bash
scripts/build_postgres16_authoritative.sh \
  "$HOME/projects/postgresql-src-pgextadv" \
  "$HOME/projects/postgresql-build-pgextadv-authoritative-16.14" \
  "$HOME/projects/postgresql-install-pgextadv-authoritative-16.14" \
  6d7f5c9cd6cf1b0f73e84a4bacc45a31d1cb0cd6
```

The source checkout must be clean and descend from the upstream base. Build
and install directories are separate and existing paths are refused. The
script uses `--enable-debug --enable-cassert --with-openssl`, runs `make -j`,
`make check`, and `make install` without sudo.

## Derived patch reproduction

`pg/patches/postgresql-16.14-pgextadv.patch` is a derived artifact, never a
source of truth. Generate it with:

```bash
scripts/export_postgres_patch.sh \
  "$HOME/projects/postgresql-src-pgextadv" \
  6d7f5c9cd6cf1b0f73e84a4bacc45a31d1cb0cd6
```

The script runs the canonical command:

```text
git diff --binary --full-index --no-ext-diff --no-renames \
  0d1c00c624fa7367d4a895f44381887757289682 \
  6d7f5c9cd6cf1b0f73e84a4bacc45a31d1cb0cd6
```

It refuses dirty source trees, validates ancestry, writes only below this
advisor repository, and reports the SHA256. Applying the generated patch to a
clean official `REL_16_14` checkout must produce the same tracked source tree
as the authoritative commit.

The former split patches are retired from the active tree; their provenance is
preserved by Git history. No PostgreSQL implementation is manually maintained
in this repository.
