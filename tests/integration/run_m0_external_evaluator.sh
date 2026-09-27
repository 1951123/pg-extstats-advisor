#!/usr/bin/env bash
set -euo pipefail

readonly REPO_ROOT=/root/projects/pg-extstats-advisor
readonly PG_INSTALL=${REPO_ROOT}/.build/postgresql-16.14-install
readonly RUNTIME_ROOT=$(mktemp -d /tmp/pg-extstats-advisor-m0b-test.XXXXXX)
readonly PORT=55434

mkdir -p "$RUNTIME_ROOT/install" "$RUNTIME_ROOT/data" "$RUNTIME_ROOT/socket"
cp -a "$PG_INSTALL"/. "$RUNTIME_ROOT/install/"
chown -R postgres:postgres "$RUNTIME_ROOT"
runuser -u postgres -- "$RUNTIME_ROOT/install/bin/initdb" -D "$RUNTIME_ROOT/data" --no-locale --encoding=UTF8 >/dev/null
runuser -u postgres -- "$RUNTIME_ROOT/install/bin/pg_ctl" -D "$RUNTIME_ROOT/data" \
  -o "-k $RUNTIME_ROOT/socket -p $PORT" -l "$RUNTIME_ROOT/server.log" start >/dev/null
trap 'runuser -u postgres -- "$RUNTIME_ROOT/install/bin/pg_ctl" -D "$RUNTIME_ROOT/data" stop -m fast >/dev/null' EXIT
export PG_EXTSTATS_TEST_DSN="host=$RUNTIME_ROOT/socket port=$PORT dbname=postgres user=postgres"
cd "$REPO_ROOT"
.venv/bin/python -m pytest -q tests/integration/test_external_evaluator.py
