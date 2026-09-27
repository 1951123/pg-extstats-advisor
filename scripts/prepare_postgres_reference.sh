#!/usr/bin/env bash
set -euo pipefail

readonly TARBALL=/root/projects/extended-stats-optim/postgresql-16.14.tar.bz2
readonly REFERENCE_PARENT=/root/projects/postgresql-reference
readonly REFERENCE_TREE=${REFERENCE_PARENT}/postgresql-16.14
readonly REPO_ROOT=/root/projects/pg-extstats-advisor
readonly CHECKSUM_FILE=${REPO_ROOT}/pg/SHA256SUMS

sha256sum --check "$CHECKSUM_FILE"

if [[ -e "$REFERENCE_TREE" ]]; then
  if [[ -f "$REFERENCE_TREE/.PG_EXTSTATS_READ_ONLY_REFERENCE" ]]; then
    printf 'Reference tree already prepared: %s\n' "$REFERENCE_TREE"
    exit 0
  fi
  printf 'Refusing to replace unmarked existing path: %s\n' "$REFERENCE_TREE" >&2
  exit 1
fi

mkdir -p "$REFERENCE_PARENT"
tar -xjf "$TARBALL" -C "$REFERENCE_PARENT"
touch "$REFERENCE_TREE/.PG_EXTSTATS_READ_ONLY_REFERENCE"
chmod -R a-w "$REFERENCE_TREE"

printf 'READ-ONLY REFERENCE TREE: %s\n' "$REFERENCE_TREE"
printf 'Upstream archive: %s\n' "$TARBALL"
sha256sum "$TARBALL"
