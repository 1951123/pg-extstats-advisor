#!/usr/bin/env bash
set -euo pipefail
umask 0007

PG_BIN=${PG_BIN:-/opt/postgresql-16.14-advisor/bin}
PGDATA=${PGDATA:-/work/advisor-pgdata}
PGSOCKET=${PGSOCKET:-/work/advisor-pgsocket}
PGPORT=${PGPORT:-55433}
mkdir -p "$PGDATA" "$PGSOCKET"
if [[ ! -s "$PGDATA/PG_VERSION" ]]; then
  "$PG_BIN/initdb" --pgdata="$PGDATA" --username=advisor --auth=trust >/dev/null
  {
    printf "listen_addresses = ''\n"
    printf "port = %s\n" "$PGPORT"
    printf "default_statistics_target = 100\n"
  } >>"$PGDATA/postgresql.conf"
fi
"$PG_BIN/postgres" -D "$PGDATA" -k "$PGSOCKET" -p "$PGPORT" &
server_pid=$!
cleanup() {
  "$PG_BIN/pg_ctl" -D "$PGDATA" -m fast stop >/dev/null 2>&1 || true
  wait "$server_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM
until "$PG_BIN/pg_isready" -h "$PGSOCKET" -p "$PGPORT" -U advisor >/dev/null 2>&1; do
  kill -0 "$server_pid" 2>/dev/null || { echo 'patched advisor PostgreSQL stopped during startup' >&2; exit 1; }
  sleep 0.2
done
"$PG_BIN/psql" -h "$PGSOCKET" -p "$PGPORT" -U advisor -d postgres \
  -v ON_ERROR_STOP=0 -c 'CREATE DATABASE advisor' >/dev/null 2>&1 || true
export PGEXT_ADVISOR_DSN="host=$PGSOCKET port=$PGPORT user=advisor dbname=postgres"
if [[ "${1:-}" == "advise" ]]; then
  exec /opt/venv/bin/pg-extstats-advisor "$@" --advisor-dsn "$PGEXT_ADVISOR_DSN"
fi
exec /opt/venv/bin/pg-extstats-advisor "$@"
