#!/usr/bin/env bash
set -euo pipefail

readonly REPO_ROOT=/root/projects/pg-extstats-advisor
readonly TARBALL=/root/projects/extended-stats-optim/postgresql-16.14.tar.bz2
readonly CHECKSUM_FILE=${REPO_ROOT}/pg/SHA256SUMS
readonly PATCH=${REPO_ROOT}/pg/patches/postgresql-16.14-hypothetical-extstats.patch
readonly BUILD_ROOT=${REPO_ROOT}/.build
readonly SOURCE_TREE=${BUILD_ROOT}/postgresql-16.14-src
readonly INSTALL_PREFIX=${BUILD_ROOT}/postgresql-16.14-install
readonly CONFIGURE_FLAGS=(
  "--prefix=${INSTALL_PREFIX}"
  --without-readline
  --without-zlib
  --without-icu
)

sha256sum --check "$CHECKSUM_FILE"
[[ -f "$PATCH" ]] || { printf 'Missing tracked patch: %s\n' "$PATCH" >&2; exit 1; }

mkdir -p "$BUILD_ROOT"
rm -rf -- "$SOURCE_TREE" "$INSTALL_PREFIX"
mkdir -p "$SOURCE_TREE" "$INSTALL_PREFIX"
tar -xjf "$TARBALL" -C "$SOURCE_TREE" --strip-components=1

patch -d "$SOURCE_TREE" -p1 --batch --forward < "$PATCH"

(
  cd "$SOURCE_TREE"
  ./configure "${CONFIGURE_FLAGS[@]}"
  make -j"$(nproc)"
  make install
)

printf 'Upstream tarball: %s\n' "$TARBALL"
printf 'Upstream SHA256: '
sha256sum "$TARBALL" | awk '{print $1}'
printf 'Patch: %s\n' "$PATCH"
printf 'Repository commit: %s\n' "$(git -C "$REPO_ROOT" rev-parse HEAD)"
printf 'Configure flags:'
printf ' %q' "${CONFIGURE_FLAGS[@]}"
printf '\nInstall prefix: %s\n' "$INSTALL_PREFIX"
