#!/usr/bin/env bash
set -euo pipefail

readonly REPO_ROOT=/root/projects/pg-extstats-advisor
readonly PG_BIN=${REPO_ROOT}/.build/postgresql-16.14-install/bin
readonly HOST=${REPO_ROOT}/.build/pg16.14-experiment-socket
readonly PORT=55436
readonly DB=pgextadv_exp16_dmv
readonly CSV=/root/projects/extended-stats-optim-v2/benchmarks/DMV/data/original.csv

[[ "$($PG_BIN/postgres --version)" == "postgres (PostgreSQL) 16.14" ]] || exit 1
[[ "$(sha256sum "$CSV" | awk '{print $1}')" == "ae310972b7ac08629d135a1da7c580e3fe603bfc2e665e1969005bd481d4605a" ]] || {
  printf 'DMV source checksum mismatch\n' >&2; exit 1;
}
runuser -u postgres -- "$PG_BIN/dropdb" -h "$HOST" -p "$PORT" --if-exists "$DB"
runuser -u postgres -- "$PG_BIN/createdb" -h "$HOST" -p "$PORT" "$DB"
runuser -u postgres -- "$PG_BIN/psql" -h "$HOST" -p "$PORT" -d "$DB" -v ON_ERROR_STOP=1 <<'SQL'
CREATE UNLOGGED TABLE public.dmv (
 record_type text,registration_class text,state text,county text,body_type text,
 fuel_type text,reg_valid_date text,color text,scofflaw_indicator text,
 suspension_indicator text,revocation_indicator text
) WITH (autovacuum_enabled=false);
SQL
runuser -u postgres -- "$PG_BIN/psql" -h "$HOST" -p "$PORT" -d "$DB" -v ON_ERROR_STOP=1 \
  -c "\copy public.dmv FROM '$CSV' WITH (FORMAT csv, HEADER true)"
runuser -u postgres -- "$PG_BIN/psql" -h "$HOST" -p "$PORT" -d "$DB" -v ON_ERROR_STOP=1 <<'SQL'
UPDATE public.dmv SET
 record_type=btrim(record_type),registration_class=btrim(registration_class),
 state=btrim(state),county=btrim(county),body_type=btrim(body_type),
 fuel_type=btrim(fuel_type),color=btrim(color),
 scofflaw_indicator=btrim(scofflaw_indicator),
 suspension_indicator=btrim(suspension_indicator),
 revocation_indicator=btrim(revocation_indicator);
ANALYZE public.dmv;
SQL
actual=$(runuser -u postgres -- "$PG_BIN/psql" -h "$HOST" -p "$PORT" -d "$DB" -Atqc \
  "SELECT count(*)||'|'||(SELECT count(*) FROM pg_attribute WHERE attrelid='public.dmv'::regclass AND attnum>0 AND NOT attisdropped)||'|'||(SELECT bool_and(format_type(atttypid,atttypmod)='text') FROM pg_attribute WHERE attrelid='public.dmv'::regclass AND attnum>0 AND NOT attisdropped)||'|'||(SELECT relpersistence::text FROM pg_class WHERE oid='public.dmv'::regclass) FROM public.dmv")
[[ "$actual" == "11591877|11|true|u" ]] || { printf 'DMV validation failed: %s\n' "$actual" >&2; exit 1; }
printf 'DMV validated: %s\n' "$actual"
