#!/usr/bin/env bash
set -euo pipefail

# Export the authoritative PostgreSQL Git delta as a derived advisor artifact.
# This script never edits the PostgreSQL source tree.
readonly REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
readonly UPSTREAM_COMMIT=0d1c00c624fa7367d4a895f44381887757289682
readonly SOURCE_DIR=${1:?usage: $0 AUTHORITATIVE_SOURCE AUTHORITATIVE_COMMIT [OUTPUT]}
readonly AUTHORITATIVE_COMMIT=${2:?usage: $0 AUTHORITATIVE_SOURCE AUTHORITATIVE_COMMIT [OUTPUT]}
readonly OUTPUT=${3:-${REPO_ROOT}/pg/patches/postgresql-16.14-pgextadv.patch}

[[ -d "$SOURCE_DIR/.git" ]] || {
  printf 'Expected authoritative Git checkout: %s\n' "$SOURCE_DIR" >&2
  exit 1
}
[[ "$(git -C "$SOURCE_DIR" rev-parse HEAD)" == "$AUTHORITATIVE_COMMIT" ]] || {
  printf 'Authoritative checkout HEAD does not match requested commit\n' >&2
  exit 1
}
[[ -z "$(git -C "$SOURCE_DIR" status --porcelain)" ]] || {
  printf 'Authoritative PostgreSQL checkout is dirty: %s\n' "$SOURCE_DIR" >&2
  exit 1
}
git -C "$SOURCE_DIR" merge-base --is-ancestor "$UPSTREAM_COMMIT" "$AUTHORITATIVE_COMMIT" || {
  printf 'Authoritative commit does not descend from upstream base %s\n' "$UPSTREAM_COMMIT" >&2
  exit 1
}

case "$OUTPUT" in
  "$REPO_ROOT"/*) ;;
  *) printf 'Output must remain below the advisor repository: %s\n' "$OUTPUT" >&2; exit 1 ;;
esac
mkdir -p "$(dirname "$OUTPUT")"
git -C "$SOURCE_DIR" diff --binary --full-index --no-ext-diff --no-renames \
  "$UPSTREAM_COMMIT" "$AUTHORITATIVE_COMMIT" -- . > "$OUTPUT"

printf 'upstream commit: %s\n' "$UPSTREAM_COMMIT"
printf 'authoritative commit: %s\n' "$AUTHORITATIVE_COMMIT"
printf 'derived patch: %s\n' "$OUTPUT"
printf 'derived patch sha256: '
sha256sum "$OUTPUT" | awk '{print $1}'
