#!/usr/bin/env bash
set -euo pipefail

# Compatibility entry point. The authoritative Git build is the supported flow.
readonly REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
readonly SOURCE_DIR=${PGEXTADV_SOURCE:?set PGEXTADV_SOURCE to a clean postgresql-pgextadv checkout}
readonly BUILD_DIR=${PGEXTADV_BUILD_DIR:-${REPO_ROOT}/.build/postgresql-16.14-authoritative-build}
readonly INSTALL_DIR=${PGEXTADV_INSTALL_DIR:-${REPO_ROOT}/.build/postgresql-16.14-authoritative-install}
readonly COMMIT=${PGEXTADV_COMMIT:-7e992ab6438fef2f8eb98c7a9ed30c9f1c816ce7}

exec "${REPO_ROOT}/scripts/build_postgres16_authoritative.sh" \
  "$SOURCE_DIR" "$BUILD_DIR" "$INSTALL_DIR" "$COMMIT"
