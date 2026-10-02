#!/usr/bin/env bash
set -euo pipefail

# Build directly from a clean frozen postgresql-pgextadv Git commit.
# The PostgreSQL source checkout is read-only input; no patch is applied.
readonly SOURCE_DIR=${1:?usage: $0 AUTHORITATIVE_SOURCE BUILD_DIR INSTALL_DIR [COMMIT]}
readonly BUILD_DIR=${2:?usage: $0 AUTHORITATIVE_SOURCE BUILD_DIR INSTALL_DIR [COMMIT]}
readonly INSTALL_DIR=${3:?usage: $0 AUTHORITATIVE_SOURCE BUILD_DIR INSTALL_DIR [COMMIT]}
readonly AUTHORITATIVE_COMMIT=${4:-6d7f5c9cd6cf1b0f73e84a4bacc45a31d1cb0cd6}
readonly UPSTREAM_COMMIT=0d1c00c624fa7367d4a895f44381887757289682

[[ -d "$SOURCE_DIR/.git" ]] || { printf 'Expected Git checkout: %s\n' "$SOURCE_DIR" >&2; exit 1; }
[[ "$(git -C "$SOURCE_DIR" rev-parse HEAD)" == "$AUTHORITATIVE_COMMIT" ]] || {
  printf 'Authoritative checkout HEAD does not match requested commit\n' >&2; exit 1;
}
[[ -z "$(git -C "$SOURCE_DIR" status --porcelain)" ]] || {
  printf 'Authoritative PostgreSQL checkout is dirty: %s\n' "$SOURCE_DIR" >&2; exit 1;
}
git -C "$SOURCE_DIR" merge-base --is-ancestor "$UPSTREAM_COMMIT" "$AUTHORITATIVE_COMMIT" || {
  printf 'Authoritative commit does not descend from upstream base %s\n' "$UPSTREAM_COMMIT" >&2; exit 1;
}
for path in "$BUILD_DIR" "$INSTALL_DIR"; do
  [[ ! -e "$path" ]] || { printf 'Refusing to replace existing path: %s\n' "$path" >&2; exit 1; }
done

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
  printf 'Authoritative build did not produce PostgreSQL 16.14\n' >&2; exit 1;
}
printf 'authoritative commit: %s\n' "$AUTHORITATIVE_COMMIT"
printf 'postgres version: '
"$INSTALL_DIR/bin/postgres" --version
printf 'compiler: '
 gcc --version | head -n 1
printf 'configure: --prefix=%s --enable-debug --enable-cassert --with-openssl\n' "$INSTALL_DIR"
printf 'postgres sha256: '
sha256sum "$INSTALL_DIR/bin/postgres" | awk '{print $1}'
