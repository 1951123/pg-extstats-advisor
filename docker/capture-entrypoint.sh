#!/bin/sh
set -eu
umask 0007
exec /opt/venv/bin/pg-extstats-advisor "$@"
