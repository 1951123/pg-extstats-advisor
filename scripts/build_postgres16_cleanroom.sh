#!/usr/bin/env bash
set -euo pipefail

# Build from a clean Git checkout without depending on a mutable PostgreSQL
# working tree. Existing output paths are refused rather than removed.
readonly REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
readonly UPSTREAM_SOURCE=${1:?usage: $0 CLEAN_REL_16_14_SOURCE BUILD_DIR INSTALL_DIR}
readonly BUILD_DIR=${2:?usage: $0 CLEAN_REL_16_14_SOURCE BUILD_DIR INSTALL_DIR}
readonly INSTALL_DIR=${3:?usage: $0 CLEAN_REL_16_14_SOURCE BUILD_DIR INSTALL_DIR}
readonly PATCH_AGGREGATE=${REPO_ROOT}/pg/patches/postgresql-16.14-pgextadv.patch
readonly PGEXTADV_COMMIT=7e992ab6438fef2f8eb98c7a9ed30c9f1c816ce7
readonly SOURCE_DIR=${BUILD_DIR}-src

[[ -d "$UPSTREAM_SOURCE/.git" ]] || {
  printf 'Expected a Git checkout at %s\n' "$UPSTREAM_SOURCE" >&2
  exit 1
}
[[ "$(git -C "$UPSTREAM_SOURCE" rev-parse HEAD)" == \
  "0d1c00c624fa7367d4a895f44381887757289682" ]] || {
  printf 'Upstream source is not the required REL_16_14 commit\n' >&2
  exit 1
}
[[ -z "$(git -C "$UPSTREAM_SOURCE" status --short)" ]] || {
  printf 'Upstream source checkout is dirty: %s\n' "$UPSTREAM_SOURCE" >&2
  exit 1
}
for path in "$SOURCE_DIR" "$BUILD_DIR" "$INSTALL_DIR"; do
  [[ ! -e "$path" ]] || {
    printf 'Refusing to replace existing clean-room path: %s\n' "$path" >&2
    exit 1
  }
done

mkdir -p "$SOURCE_DIR"
git -C "$UPSTREAM_SOURCE" archive --format=tar HEAD | tar -xf - -C "$SOURCE_DIR"
patch -d "$SOURCE_DIR" -p1 --batch --forward < "$PATCH_AGGREGATE"

mkdir -p "$BUILD_DIR"
(
  cd "$BUILD_DIR"
  "$SOURCE_DIR/configure" \
    --prefix="$INSTALL_DIR" \
    --enable-debug \
    --enable-cassert \
    --with-openssl
  make -j"$(nproc)"
  make check
  make install
)

[[ "$($INSTALL_DIR/bin/postgres --version)" == "postgres (PostgreSQL) 16.14" ]] || {
  printf 'Clean-room build did not produce PostgreSQL 16.14\n' >&2
  exit 1
}

printf 'authoritative commit: %s\n' "$PGEXTADV_COMMIT"
printf 'aggregate patch sha256: '
sha256sum "$PATCH_AGGREGATE" | awk '{print $1}'
printf 'clean-room postgres sha256: '
sha256sum "$INSTALL_DIR/bin/postgres" | awk '{print $1}'
printf 'clean-room install: %s\n' "$INSTALL_DIR"
