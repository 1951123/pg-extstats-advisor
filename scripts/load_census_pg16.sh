#!/usr/bin/env bash
set -euo pipefail

readonly REPO_ROOT=/root/projects/pg-extstats-advisor
readonly PG_BIN=${REPO_ROOT}/.build/postgresql-16.14-install/bin
readonly HOST=${REPO_ROOT}/.build/pg16.14-experiment-socket
readonly PORT=55436
readonly DB=pgextadv_exp16_census
readonly CSV=${REPO_ROOT}/.build/datasets/census-climate.csv

[[ "$($PG_BIN/postgres --version)" == "postgres (PostgreSQL) 16.14" ]] || exit 1
[[ -s "$CSV" ]] || { printf 'Missing Census logical CSV: %s\n' "$CSV" >&2; exit 1; }
runuser -u postgres -- "$PG_BIN/dropdb" -h "$HOST" -p "$PORT" --if-exists "$DB"
runuser -u postgres -- "$PG_BIN/createdb" -h "$HOST" -p "$PORT" "$DB"
runuser -u postgres -- "$PG_BIN/psql" -h "$HOST" -p "$PORT" -d "$DB" -v ON_ERROR_STOP=1 <<'SQL'
CREATE TABLE public.climate (
 caseid integer,dage integer,dancstry1 integer,dancstry2 integer,iavail integer,
 icitizen integer,iclass integer,ddepart integer,idisabl1 integer,idisabl2 integer,
 ienglish integer,ifeb55 integer,ifertil integer,dhispanic integer,dhour89 integer,
 dhours integer,iimmigr integer,dincome1 integer,dincome2 integer,dincome3 integer,
 dincome4 integer,dincome5 integer,dincome6 integer,dincome7 integer,dincome8 integer,
 dindustry integer,ikorean integer,ilang1 integer,ilooking integer,imarital integer,
 imay75880 integer,imeans integer,imilitary integer,imobility integer,imobillim integer,
 doccup integer,iothrserv integer,iperscare integer,dpob integer,dpoverty integer,
 dpwgt1 integer,iragechld integer,drearning integer,irelat1 integer,irelat2 integer,
 iremplpar integer,iriders integer,irlabor integer,irownchld integer,drpincome integer,
 irpob integer,irrelchld integer,irspouse integer,irvetserv integer,ischool integer,
 isept80 integer,isex integer,isubfam1 integer,isubfam2 integer,itmpabsnt integer,
 dtravtime integer,ivietnam integer,dweek89 integer,iwork89 integer,iworklwk integer,
 iwwii integer,iyearsch integer,iyearwrk integer,dyrsserv integer
);
SQL
runuser -u postgres -- "$PG_BIN/psql" -h "$HOST" -p "$PORT" -d "$DB" -v ON_ERROR_STOP=1 \
  -c "\copy public.climate FROM '$CSV' WITH (FORMAT csv, HEADER true)"
runuser -u postgres -- "$PG_BIN/psql" -h "$HOST" -p "$PORT" -d "$DB" -v ON_ERROR_STOP=1 \
  -c "ANALYZE public.climate"
actual=$(runuser -u postgres -- "$PG_BIN/psql" -h "$HOST" -p "$PORT" -d "$DB" -Atqc \
  "SELECT count(*)||'|'||(SELECT count(*) FROM pg_attribute WHERE attrelid='public.climate'::regclass AND attnum>0 AND NOT attisdropped)||'|'||(SELECT relpersistence::text FROM pg_class WHERE oid='public.climate'::regclass) FROM public.climate")
[[ "$actual" == "2458285|69|p" ]] || { printf 'Census validation failed: %s\n' "$actual" >&2; exit 1; }
printf 'Census validated: %s\n' "$actual"
