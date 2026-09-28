#!/usr/bin/env bash
set -euo pipefail

readonly REPO_ROOT=/root/projects/pg-extstats-advisor
readonly PSQL=${REPO_ROOT}/.build/postgresql-16.14-install/bin/psql
readonly OUTPUT=${REPO_ROOT}/.build/datasets/census-climate.csv
readonly SOURCE_SOCKET=/var/run/postgresql

install -d -o postgres -g postgres "$(dirname "$OUTPUT")"
[[ "$($PSQL --version)" == "psql (PostgreSQL) 16.14" ]] || exit 1
runuser -u postgres -- "$PSQL" -h "$SOURCE_SOCKET" -d census -v ON_ERROR_STOP=1 \
  -c "\copy public.climate TO '$OUTPUT' WITH (FORMAT csv, HEADER true)"
[[ "$(($(wc -l < "$OUTPUT") - 1))" == 2458285 ]] || {
  printf 'Unexpected Census logical export row count\n' >&2; exit 1;
}
sha256sum "$OUTPUT"
