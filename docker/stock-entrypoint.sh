#!/usr/bin/env bash
set -euo pipefail

PG_BIN=${PG_BIN:-/opt/postgresql-16.14-stock/bin}
PGDATA=${PGDATA:-/var/lib/postgresql/data}
PGPORT=${PGPORT:-5432}
PGDATABASE=${POSTGRES_DB:-demo}
POSTGRES_PASSWORD=${POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be supplied at runtime}
CAPTURE_PASSWORD=${CAPTURE_PASSWORD:?CAPTURE_PASSWORD must be supplied at runtime}

mkdir -p "$PGDATA"
if [[ ! -s "$PGDATA/PG_VERSION" ]]; then
  pw_file=$(mktemp)
  trap 'rm -f "$pw_file"' EXIT
  printf '%s\n' "$POSTGRES_PASSWORD" >"$pw_file"
  "$PG_BIN/initdb" --pgdata="$PGDATA" --username=postgres --pwfile="$pw_file" --auth=scram-sha-256 >/dev/null
  rm -f "$pw_file"
  trap - EXIT
  {
    printf "listen_addresses = '0.0.0.0'\n"
    printf "port = %s\n" "$PGPORT"
    printf "default_statistics_target = %s\n" "${DEFAULT_STATISTICS_TARGET:-100}"
  } >>"$PGDATA/postgresql.conf"
  printf 'host all all 0.0.0.0/0 scram-sha-256\n' >>"$PGDATA/pg_hba.conf"
fi

"$PG_BIN/postgres" -D "$PGDATA" -k /tmp -p "$PGPORT" &
server_pid=$!
cleanup() {
  "$PG_BIN/pg_ctl" -D "$PGDATA" -m fast stop >/dev/null 2>&1 || true
  wait "$server_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM
until "$PG_BIN/pg_isready" -h 127.0.0.1 -p "$PGPORT" -U postgres >/dev/null 2>&1; do
  kill -0 "$server_pid" 2>/dev/null || { echo 'stock PostgreSQL stopped during startup' >&2; exit 1; }
  sleep 0.2
done
export PGPASSWORD="$POSTGRES_PASSWORD"
"$PG_BIN/psql" -h 127.0.0.1 -p "$PGPORT" -U postgres -d postgres -v ON_ERROR_STOP=1 \
  -v dbname="$PGDATABASE" -v capture_pw="$CAPTURE_PASSWORD" <<'SQL'
SELECT format('CREATE DATABASE %I', :'dbname') WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'dbname') \gexec
SELECT format('CREATE ROLE capture LOGIN PASSWORD %L', :'capture_pw') WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'capture') \gexec
SQL
"$PG_BIN/psql" -h 127.0.0.1 -p "$PGPORT" -U postgres -d "$PGDATABASE" -v ON_ERROR_STOP=1 \
  -v capture_pw="$CAPTURE_PASSWORD" <<'SQL'
ALTER ROLE capture LOGIN PASSWORD :'capture_pw';
SQL
unset PGPASSWORD
exec tail -f /dev/null &
wait
