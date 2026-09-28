#!/usr/bin/env bash
set -euo pipefail

readonly REPO_ROOT=/root/projects/pg-extstats-advisor
readonly TARBALL=/root/projects/extended-stats-optim/postgresql-16.14.tar.bz2
readonly CHECKSUM_FILE=${REPO_ROOT}/pg/SHA256SUMS
readonly PATCH=${REPO_ROOT}/pg/patches/postgresql-16.14-hypothetical-extstats.patch
readonly BUILD_ROOT=${REPO_ROOT}/.build
readonly SOURCE_TREE=${BUILD_ROOT}/postgresql-16.14-src
readonly INSTALL_PREFIX=${BUILD_ROOT}/postgresql-16.14-install
readonly PROVENANCE=${REPO_ROOT}/experiments/environment/postgresql-16.14-build.json
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

readonly UPSTREAM_SHA256=$(sha256sum "$TARBALL" | awk '{print $1}')
readonly PATCH_SHA256=$(sha256sum "$PATCH" | awk '{print $1}')
readonly COMPILER=$(command -v gcc)
readonly COMPILER_VERSION=$(gcc --version | head -n1)
readonly CFLAGS_VALUE=${CFLAGS:-}

(
  cd "$SOURCE_TREE"
  ./configure "${CONFIGURE_FLAGS[@]}"
  make -j"$(nproc)"
  make install
)

[[ "$($INSTALL_PREFIX/bin/postgres --version)" == "postgres (PostgreSQL) 16.14" ]] || {
  printf 'Built server version is not PostgreSQL 16.14\n' >&2
  exit 1
}
readonly EFFECTIVE_CFLAGS=$($INSTALL_PREFIX/bin/pg_config --cflags)
readonly RECIPE_DIGEST=$(printf '%s\0' \
  "$UPSTREAM_SHA256" "$PATCH_SHA256" "${CONFIGURE_FLAGS[@]}" \
  "$COMPILER" "$COMPILER_VERSION" "$CFLAGS_VALUE" "$EFFECTIVE_CFLAGS" \
  | sha256sum | awk '{print $1}')

mkdir -p "$(dirname "$PROVENANCE")"
export TARBALL_VALUE="$TARBALL"
export UPSTREAM_SHA256_VALUE="$UPSTREAM_SHA256"
export PATCH_VALUE="$PATCH"
export PATCH_SHA256_VALUE="$PATCH_SHA256"
export COMPILER_VALUE="$COMPILER"
export COMPILER_VERSION_VALUE="$COMPILER_VERSION"
export CONFIGURE_ARGS_VALUE
CONFIGURE_ARGS_VALUE=$(printf '%s\n' "${CONFIGURE_FLAGS[@]}")
export CFLAGS_VALUE_EXPORTED="$EFFECTIVE_CFLAGS"
export INSTALL_PREFIX_VALUE="$INSTALL_PREFIX"
export POSTGRES_BINARY_SHA256_VALUE
POSTGRES_BINARY_SHA256_VALUE=$(sha256sum "$INSTALL_PREFIX/bin/postgres" | awk '{print $1}')
export RECIPE_DIGEST_VALUE="$RECIPE_DIGEST"
export GIT_COMMIT_VALUE
GIT_COMMIT_VALUE=$(git -C "$REPO_ROOT" rev-parse HEAD)
export CREATED_AT_VALUE
CREATED_AT_VALUE=$(date --utc +%Y-%m-%dT%H:%M:%SZ)
python3 - "$PROVENANCE" <<'PY'
import json, os, pathlib, sys
value = {
    "format_version": 1,
    "postgres_version": "16.14",
    "profile": "release-like-authoritative-timing-v1",
    "upstream_tarball_path": os.environ["TARBALL_VALUE"],
    "upstream_sha256": os.environ["UPSTREAM_SHA256_VALUE"],
    "patch_path": os.environ["PATCH_VALUE"],
    "patch_sha256": os.environ["PATCH_SHA256_VALUE"],
    "compiler": os.environ["COMPILER_VALUE"],
    "compiler_version": os.environ["COMPILER_VERSION_VALUE"],
    "configure_args": os.environ["CONFIGURE_ARGS_VALUE"].split("\n"),
    "cflags": os.environ["CFLAGS_VALUE_EXPORTED"],
    "assertions_enabled": False,
    "debug_enabled": False,
    "llvm_jit_built": False,
    "install_prefix": os.environ["INSTALL_PREFIX_VALUE"],
    "postgres_binary_sha256": os.environ["POSTGRES_BINARY_SHA256_VALUE"],
    "build_recipe_digest": os.environ["RECIPE_DIGEST_VALUE"],
    "git_commit": os.environ["GIT_COMMIT_VALUE"],
    "created_at": os.environ["CREATED_AT_VALUE"],
}
path = pathlib.Path(sys.argv[1])
temporary = path.with_suffix(path.suffix + ".tmp")
temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
temporary.replace(path)
PY

printf 'Upstream tarball: %s\n' "$TARBALL"
printf 'Upstream SHA256: '
sha256sum "$TARBALL" | awk '{print $1}'
printf 'Patch: %s\n' "$PATCH"
printf 'Repository commit: %s\n' "$(git -C "$REPO_ROOT" rev-parse HEAD)"
printf 'Configure flags:'
printf ' %q' "${CONFIGURE_FLAGS[@]}"
printf '\nInstall prefix: %s\n' "$INSTALL_PREFIX"
printf 'Build recipe digest: %s\n' "$RECIPE_DIGEST"
printf 'Build provenance: %s\n' "$PROVENANCE"
