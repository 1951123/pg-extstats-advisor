#!/usr/bin/env bash
set -euo pipefail

readonly REPO_ROOT=/root/projects/pg-extstats-advisor
readonly PG_BIN=${REPO_ROOT}/.build/postgresql-16.14-install/bin
readonly DATA=${REPO_ROOT}/.build/pg16.14-experiment-cluster
readonly SOCKET=${REPO_ROOT}/.build/pg16.14-experiment-socket
readonly LOG=${DATA}/server.log
readonly PORT=55436

[[ "$($PG_BIN/postgres --version)" == "postgres (PostgreSQL) 16.14" ]] || {
  printf 'Refusing non-16.14 PostgreSQL installation\n' >&2; exit 1;
}
if [[ ! -s "$DATA/PG_VERSION" ]]; then
  install -d -o postgres -g postgres "$DATA" "$SOCKET"
  runuser -u postgres -- "$PG_BIN/initdb" -D "$DATA" --no-locale --encoding=UTF8 >/dev/null
  {
    printf "unix_socket_directories = '%s'\n" "$SOCKET"
    printf "port = %s\n" "$PORT"
    printf "jit = off\n"
    printf "max_parallel_workers_per_gather = 2\n"
    printf "default_statistics_target = 100\n"
  } >> "$DATA/postgresql.conf"
fi
install -d -o postgres -g postgres "$SOCKET"
if ! runuser -u postgres -- "$PG_BIN/pg_ctl" -D "$DATA" status >/dev/null 2>&1; then
  runuser -u postgres -- "$PG_BIN/pg_ctl" -D "$DATA" -l "$LOG" start >/dev/null
fi
version=$(runuser -u postgres -- "$PG_BIN/psql" -h "$SOCKET" -p "$PORT" -d postgres -Atqc "SHOW server_version")
[[ "$version" == "16.14" ]] || { printf 'Server version mismatch: %s\n' "$version" >&2; exit 1; }
printf 'PGHOST=%s\nPGPORT=%s\nPGVERSION=%s\n' "$SOCKET" "$PORT" "$version"
